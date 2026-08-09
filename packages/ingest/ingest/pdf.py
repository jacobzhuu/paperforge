"""PDF 文本抽取：pypdf 优先，标准库操作符扫描兜底。

背景：迁移自 DeepSearch 的 `document_extractors._extract_pdf_content` 只做
「解压流 + 扫 Tj/TJ 操作符」，对字体子集化/CID 编码的真实学术 PDF 几乎取不到正文
（实测 613 KB 的 Europe PMC 渲染件只出 1 KB 文本）。文献卡片抽取依赖全文质量，
因此这里以 pypdf 为主路径，保留原实现作为 pypdf 不可用或抽取为空时的兜底。

安全语义不变：纯解析，不执行 JS、不加载外部资源、不跟随嵌入链接。
"""

from __future__ import annotations

import re
from io import BytesIO
from typing import Any

from ingest.document_extractors import (
    DocumentParseError,
    _extract_pdf_content,
    mime_policy_metadata,
)
from ingest.types import ParsedContent

# 低于该字符数视为「基本没取到正文」，触发另一条路径。
_MIN_USEFUL_TEXT_CHARS = 400


def extract_pdf_content(*, content: bytes, mime_type: str = "application/pdf") -> ParsedContent:
    """抽取 PDF 文本，页码定位尽力而为。"""
    if not content.startswith(b"%PDF-"):
        raise DocumentParseError("pdf_signature_mismatch")

    parsed = _extract_with_pypdf(content=content, mime_type=mime_type)
    structured = _extract_with_pdfplumber(content=content, mime_type=mime_type)
    if structured is not None and (
        parsed is None
        or len(structured.text) >= int(len(parsed.text) * 0.8)
        or bool(structured.metadata.get("structured_objects"))
    ):
        parsed = structured
    if parsed is not None and len(parsed.text) >= _MIN_USEFUL_TEXT_CHARS:
        return parsed

    fallback = _extract_pdf_content(content=content, mime_type=mime_type)
    if parsed is None:
        return fallback
    # 两条路径都跑过时取正文更长的一条，并记录另一条的产出量便于诊断。
    if len(fallback.text) > len(parsed.text):
        return ParsedContent(
            text=fallback.text,
            title=fallback.title,
            source_type=fallback.source_type,
            metadata={**fallback.metadata, "pypdf_text_length": len(parsed.text)},
        )
    return parsed


def _extract_with_pypdf(*, content: bytes, mime_type: str) -> ParsedContent | None:
    try:
        from pypdf import PdfReader
    except ImportError:
        return None

    try:
        reader = PdfReader(BytesIO(content))
        if reader.is_encrypted:
            # 加密 PDF 一律不尝试解密（合规红线：不绕任何访问控制）。
            return None
        pages = [(page.extract_text() or "").strip() for page in reader.pages]
    except Exception:  # noqa: BLE001 - 损坏 PDF 交给兜底路径
        return None

    page_texts = [text for text in pages if text]
    if not page_texts:
        return None

    segments: list[dict[str, Any]] = []
    parts: list[str] = []
    offset = 0
    for index, text in enumerate(pages):
        if not text:
            continue
        normalized = _normalize_pdf_text(text)
        if not normalized:
            continue
        parts.append(normalized)
        segments.extend(
            _page_structure_segments(
                normalized,
                page_number=index + 1,
                document_offset=offset,
            )
        )
        offset += len(normalized) + 2

    full_text = "\n\n".join(parts)
    return ParsedContent(
        text=full_text,
        title=_derive_title(full_text),
        source_type="pdf_document",
        metadata={
            **mime_policy_metadata(mime_type),
            "extractor": "pypdf_v1",
            "parser_status": "success",
            "parser_kind": "pdf",
            "content_type": mime_type,
            "text_length": len(full_text),
            "page_count": len(reader.pages),
            "page_locator_reliable": True,
            "parser_warnings": [],
            "structure_segments": segments,
        },
    )


def _normalize_pdf_text(text: str) -> str:
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _derive_title(text: str) -> str | None:
    for line in text.split("\n"):
        candidate = line.strip()
        if len(candidate) >= 8:
            return candidate[:300]
    return None


def _extract_with_pdfplumber(*, content: bytes, mime_type: str) -> ParsedContent | None:
    """MIT 许可的 pdfplumber 增强路径：标题线索、表格、图注与公式引用。"""
    try:
        import pdfplumber
    except ImportError:
        return None
    try:
        pdf = pdfplumber.open(BytesIO(content))
    except Exception:  # noqa: BLE001 - 损坏/加密 PDF 交给其他路径
        return None
    try:
        parts: list[str] = []
        segments: list[dict[str, Any]] = []
        objects: list[dict[str, Any]] = []
        offset = 0
        table_number = 0
        for page_number, page in enumerate(pdf.pages, start=1):
            page_text = _normalize_pdf_text(page.extract_text() or "")
            if not page_text:
                continue
            page_parts = [page_text]
            page_start = offset
            segments.extend(
                _page_structure_segments(
                    page_text,
                    page_number=page_number,
                    document_offset=page_start,
                )
            )
            for match in re.finditer(
                r"^(?P<label>(?:Figure|Fig\.|Table|图|表)\s*[A-Za-z]?\d+"
                r"(?:[.-]\d+)?[.:]?[^\n]{0,500})$",
                page_text,
                re.IGNORECASE | re.MULTILINE,
            ):
                label = match.group("label")
                kind = "table" if re.match(r"^(?:Table|表)", label, re.IGNORECASE) else "figure"
                number = re.search(r"[A-Za-z]?\d+(?:[.-]\d+)?", label)
                prefix = "table" if kind == "table" else "fig"
                object_ref = f"{prefix}:{number.group() if number else '?'}"
                object_segment = {
                    "format": "pdf",
                    "page_number": page_number,
                    "page_range": [page_number, page_number],
                    "page_locator_reliable": True,
                    "char_start": page_start + match.start(),
                    "char_end": page_start + match.end(),
                    "object_ref": object_ref,
                    "object_kind": kind,
                }
                segments.append(object_segment)
                objects.append({**object_segment, "text": label})
            try:
                tables = page.extract_tables()
            except Exception:  # noqa: BLE001 - 单页表格失败不影响正文
                tables = []
            for table in tables:
                markdown = _pdf_table_to_markdown(table)
                if not markdown:
                    continue
                table_number += 1
                separator = 2 if page_parts else 0
                object_start = (
                    page_start
                    + sum(len(part) for part in page_parts)
                    + (2 * (len(page_parts) - 1))
                    + separator
                )
                page_parts.append(markdown)
                object_ref = f"table:{table_number}"
                object_segment = {
                    "format": "pdf",
                    "page_number": page_number,
                    "page_range": [page_number, page_number],
                    "page_locator_reliable": True,
                    "char_start": object_start,
                    "char_end": object_start + len(markdown),
                    "object_ref": object_ref,
                    "object_kind": "table",
                }
                segments.append(object_segment)
                objects.append({**object_segment, "text": markdown})
            combined = "\n\n".join(page_parts)
            parts.append(combined)
            offset += len(combined) + 2
        full_text = "\n\n".join(parts)
        if not full_text:
            return None
        return ParsedContent(
            text=full_text,
            title=_derive_title(full_text),
            source_type="pdf_document",
            metadata={
                **mime_policy_metadata(mime_type),
                "extractor": "pdfplumber_v1",
                "parser_status": "success",
                "parser_kind": "pdf",
                "content_type": mime_type,
                "text_length": len(full_text),
                "page_count": len(pdf.pages),
                "page_locator_reliable": True,
                "parser_warnings": [],
                "structure_segments": segments,
                "structured_objects": objects,
            },
        )
    finally:
        pdf.close()


def _pdf_table_to_markdown(table: list[list[str | None]] | None) -> str:
    if not table:
        return ""
    rows = [
        [" ".join((cell or "").split()).replace("|", r"\|") for cell in row] for row in table if row
    ]
    rows = [row for row in rows if any(row)]
    if len(rows) < 2:
        return ""
    width = max(len(row) for row in rows)
    normalized = [row + [""] * (width - len(row)) for row in rows]
    header, *data = normalized
    if not any(header):
        header = [f"column_{index + 1}" for index in range(width)]
    return "\n".join(
        [
            "| " + " | ".join(header) + " |",
            "| " + " | ".join("---" for _ in header) + " |",
            *["| " + " | ".join(row) + " |" for row in data],
        ]
    )


_KNOWN_HEADINGS = re.compile(
    r"^(?:abstract|introduction|background|related work|literature review|"
    r"materials? and methods?|methodology|methods?|experimental setup|experiments?|"
    r"evaluation|results?|findings?|discussion|limitations?|conclusions?|future work|"
    r"acknowledg(?:e)?ments?|references|摘要|引言|背景|相关工作|文献综述|"
    r"材料与方法|方法|实验设置|实验|评估|结果|讨论|局限|结论|未来工作|致谢|参考文献)$",
    re.IGNORECASE,
)
_NUMBERED_HEADING = re.compile(
    r"^(?:(?:\d+(?:\.\d+){0,3})|(?:[IVXLC]+))[\s.、]+(.{2,120})$",
    re.IGNORECASE,
)


def _heading_title(line: str) -> str | None:
    """保守识别 PDF 文本行标题；宁可漏掉，也不把正文误标成章节。"""
    candidate = " ".join(line.split()).strip()
    if not candidate or len(candidate) > 160:
        return None
    numbered = _NUMBERED_HEADING.match(candidate)
    if numbered:
        return candidate
    if _KNOWN_HEADINGS.fullmatch(candidate):
        return candidate
    words = candidate.split()
    if 1 <= len(words) <= 10 and len(candidate) >= 4 and candidate.isupper():
        return candidate
    return None


def _page_structure_segments(
    text: str,
    *,
    page_number: int,
    document_offset: int,
) -> list[dict[str, Any]]:
    """把页内标题提升为带 section_title 的可靠字符区间。"""
    headings: list[tuple[int, str]] = []
    cursor = 0
    for line in text.splitlines(keepends=True):
        title = _heading_title(line.rstrip("\r\n"))
        if title:
            headings.append((cursor, title))
        cursor += len(line)

    boundaries: list[tuple[int, str | None]] = [(0, None)]
    for position, title in headings:
        if position == 0:
            boundaries[0] = (0, title)
        else:
            boundaries.append((position, title))

    segments: list[dict[str, Any]] = []
    for index, (start, title) in enumerate(boundaries):
        end = boundaries[index + 1][0] if index + 1 < len(boundaries) else len(text)
        if end <= start:
            continue
        segment: dict[str, Any] = {
            "format": "pdf",
            "page_number": page_number,
            "page_range": [page_number, page_number],
            "page_locator_reliable": True,
            "char_start": document_offset + start,
            "char_end": document_offset + end,
        }
        if title:
            segment["section_title"] = title
        segments.append(segment)
    return segments
