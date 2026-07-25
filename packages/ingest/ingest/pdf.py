"""PDF 文本抽取：pypdf 优先，标准库操作符扫描兜底。

背景：迁移自 DeepSearch 的 `document_extractors._extract_pdf_content` 只做
「解压流 + 扫 Tj/TJ 操作符」，对字体子集化/CID 编码的真实学术 PDF 几乎取不到正文
（实测 613 KB 的 Europe PMC 渲染件只出 1 KB 文本）。文献卡片抽取依赖全文质量，
因此这里以 pypdf 为主路径，保留原实现作为 pypdf 不可用或抽取为空时的兜底。

安全语义不变：纯解析，不执行 JS、不加载外部资源、不跟随嵌入链接。
"""

from __future__ import annotations

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
        segments.append(
            {
                "format": "pdf",
                "page_number": index + 1,
                "page_range": [index + 1, index + 1],
                "page_locator_reliable": True,
                "char_start": offset,
                "char_end": offset + len(normalized),
            }
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
