"""PDF / DOCX / PPTX / XLSX 确定性抽取器（标准库实现，不执行宏）。

迁移自 DeepSearch parsing/document_extractors.py（设计 §3.1「直接复用」）。
改动：仅把 ParsedContent 的导入指向 ingest.types，其余零改动。
服务两条管线：OA 全文解析（综述管线 INGEST）与用户素材解析（研究型管线 INPUT）。

安全语义：Office 文件按 zip + XML 解析，不执行宏、不加载外部资源、不展开嵌入对象；
mime_policy_metadata() 把这些保证写进产物元数据。
"""

from __future__ import annotations

import re
import zlib
from dataclasses import dataclass
from io import BytesIO
from typing import Any
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from ingest.types import ParsedContent

_MAX_PDF_STREAM_BYTES = 8 * 1024 * 1024

SUPPORTED_DOCUMENT_MIME_TYPES = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
}

SUPPORTED_TEXT_MIME_TYPES = {
    "application/x-env",
    "application/x-yaml",
    "application/yaml",
    "text/html",
    "text/markdown",
    "text/plain",
    "text/x-yaml",
    "text/yaml",
}
SUPPORTED_MIME_TYPES = SUPPORTED_TEXT_MIME_TYPES | set(SUPPORTED_DOCUMENT_MIME_TYPES)

PDF_PAGE_LOCATOR_FALLBACK_REASON = "pdf_page_stream_mapping_unreliable"
PDF_TEXT_FALLBACK_REASON = "pdf_text_operator_extraction_only"
OFFICE_VISUAL_LAYOUT_FALLBACK_REASON = "office_visual_layout_not_interpreted"


class DocumentParseError(ValueError):
    pass


@dataclass(frozen=True)
class _TextSegment:
    text: str
    locator: dict[str, Any]


def extract_document_content(*, mime_type: str, content: bytes) -> ParsedContent:
    normalized_mime_type = normalize_mime_type(mime_type)
    parser_kind = SUPPORTED_DOCUMENT_MIME_TYPES.get(normalized_mime_type)
    if parser_kind == "pdf":
        return _extract_pdf_content(content=content, mime_type=normalized_mime_type)
    if parser_kind == "docx":
        return _extract_docx_content(content=content, mime_type=normalized_mime_type)
    if parser_kind == "pptx":
        return _extract_pptx_content(content=content, mime_type=normalized_mime_type)
    if parser_kind == "xlsx":
        return _extract_xlsx_content(content=content, mime_type=normalized_mime_type)
    raise DocumentParseError(f"unsupported document mime type: {normalized_mime_type}")


def normalize_mime_type(mime_type: str) -> str:
    return mime_type.split(";", 1)[0].strip().lower() or "application/octet-stream"


def mime_policy_metadata(mime_type: str) -> dict[str, Any]:
    normalized_mime_type = normalize_mime_type(mime_type)
    return {
        "mime_type": normalized_mime_type,
        "mime_policy": {
            "supported": normalized_mime_type in SUPPORTED_MIME_TYPES,
            "supported_mime_types": sorted(SUPPORTED_MIME_TYPES),
            "office_macros_executed": False,
            "external_resources_loaded": False,
            "embedded_objects_executed": False,
        },
    }


def _extract_pdf_content(*, content: bytes, mime_type: str) -> ParsedContent:
    if not content.startswith(b"%PDF-"):
        raise DocumentParseError("pdf_signature_mismatch")

    page_count = len(re.findall(rb"/Type\s*/Page\b", content))
    raw_streams = _extract_pdf_streams(content)
    page_texts: list[str] = []
    warnings = [PDF_TEXT_FALLBACK_REASON]
    for stream in raw_streams:
        extracted = _extract_pdf_text_from_stream(stream)
        if extracted:
            page_texts.append(extracted)

    if not page_texts:
        fallback_text = _extract_pdf_literal_text(content)
        if fallback_text:
            page_texts.append(fallback_text)
            warnings.append("pdf_literal_text_fallback")

    locator_reliable = bool(page_count and page_count == len(page_texts))
    if not locator_reliable:
        warnings.append(PDF_PAGE_LOCATOR_FALLBACK_REASON)

    segments = [
        _TextSegment(
            text=text,
            locator={
                "format": "pdf",
                "page_number": index + 1 if locator_reliable else None,
                "page_range": ([index + 1, index + 1] if locator_reliable else None),
                "page_locator_reliable": locator_reliable,
                "locator_fallback_reason": (
                    None if locator_reliable else PDF_PAGE_LOCATOR_FALLBACK_REASON
                ),
            },
        )
        for index, text in enumerate(page_texts)
    ]
    text, segment_payloads = _join_segments(segments)
    return ParsedContent(
        text=text,
        title=_derive_title(text),
        source_type="pdf_document",
        metadata={
            **mime_policy_metadata(mime_type),
            "extractor": "pdf_text_stream_v1",
            "parser_status": "success",
            "parser_kind": "pdf",
            "content_type": mime_type,
            "text_length": len(text),
            "page_count": page_count if page_count else None,
            "page_locator_reliable": locator_reliable,
            "locator_fallback_reason": (
                None if locator_reliable else PDF_PAGE_LOCATOR_FALLBACK_REASON
            ),
            "parser_warnings": warnings,
            "structure_segments": segment_payloads,
        },
    )


def _extract_docx_content(*, content: bytes, mime_type: str) -> ParsedContent:
    with _open_office_zip(content, expected_member="word/document.xml") as archive:
        document_xml = archive.read("word/document.xml")
        root = ElementTree.fromstring(document_xml)
        segments: list[_TextSegment] = []
        paragraph_no = 0
        table_no = 0
        body = next((element for element in _iter_elements(root, "body")), root)
        for child in list(body):
            child_kind = _local_name(child.tag)
            if child_kind == "p":
                text = _docx_paragraph_text(child)
                if not text:
                    continue
                paragraph_no += 1
                segments.append(
                    _TextSegment(
                        text=text,
                        locator={
                            "format": "docx",
                            "paragraph_no": paragraph_no,
                            "section": f"paragraph:{paragraph_no}",
                            "structure_kind": "paragraph",
                            "page_locator_reliable": False,
                            "locator_fallback_reason": "docx_has_no_stable_page_numbers",
                        },
                    )
                )
            elif child_kind == "tbl":
                rows = _docx_table_rows(child)
                if not rows:
                    continue
                table_no += 1
                segments.append(
                    _TextSegment(
                        text=f"Table {table_no}\n" + "\n".join(rows),
                        locator={
                            "format": "docx",
                            "structure_kind": "table",
                            "table_index": table_no,
                            "table_row_count": len(rows),
                            "table_column_count": max(row.count(" | ") + 1 for row in rows),
                            "page_locator_reliable": False,
                            "locator_fallback_reason": "docx_has_no_stable_page_numbers",
                        },
                    )
                )
    text, segment_payloads = _join_segments(segments)
    return ParsedContent(
        text=text,
            title=_derive_title(text),
            source_type="office_document",
            metadata={
                **mime_policy_metadata(mime_type),
                "extractor": "docx_xml_text_v2",
                "parser_status": "success",
                "parser_kind": "docx",
                "content_type": mime_type,
                "text_length": len(text),
                "paragraph_count": paragraph_no,
                "table_count": table_no,
                "locator_reliability": "paragraph_and_table_xml_order",
                "citation_offsets_exact_in_extracted_text": True,
                "content_structure_hints": {
                    "paragraph_count": paragraph_no,
                    "table_count": table_no,
                    "structure_segment_count": len(segment_payloads),
                },
                "parser_warnings": [OFFICE_VISUAL_LAYOUT_FALLBACK_REASON],
                "structure_segments": segment_payloads,
            },
        )


def _extract_pptx_content(*, content: bytes, mime_type: str) -> ParsedContent:
    with _open_office_zip(content, expected_member="[Content_Types].xml") as archive:
        slide_names = sorted(
            (name for name in archive.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", name)),
            key=_natural_sort_key,
        )
        segments = []
        for slide_no, slide_name in enumerate(slide_names, start=1):
            root = ElementTree.fromstring(archive.read(slide_name))
            text = _normalize_line(
                "\n".join(
                    _normalize_line(node.text or "")
                    for node in _iter_elements(root, "t")
                    if _normalize_line(node.text or "")
                )
            )
            if not text:
                continue
            segments.append(
                _TextSegment(
                    text=text,
                        locator={
                            "format": "pptx",
                            "slide_number": slide_no,
                            "slide_range": [slide_no, slide_no],
                            "structure_kind": "slide_text",
                            "text_block_count": len(
                                [
                                    node
                                    for node in _iter_elements(root, "t")
                                    if _normalize_line(node.text or "")
                                ]
                            ),
                        },
                    )
                )
    text, segment_payloads = _join_segments(segments)
    return ParsedContent(
        text=text,
        title=_derive_title(text),
        source_type="office_document",
        metadata={
            **mime_policy_metadata(mime_type),
                "extractor": "pptx_slide_xml_text_v2",
                "parser_status": "success",
                "parser_kind": "pptx",
                "content_type": mime_type,
                "text_length": len(text),
                "slide_count": len(slide_names),
                "locator_reliability": "slide_xml_order",
                "citation_offsets_exact_in_extracted_text": True,
                "content_structure_hints": {
                    "slide_count": len(slide_names),
                    "text_block_count": sum(
                        int(segment.get("text_block_count") or 0) for segment in segment_payloads
                    ),
                    "structure_segment_count": len(segment_payloads),
                },
                "parser_warnings": [OFFICE_VISUAL_LAYOUT_FALLBACK_REASON],
                "structure_segments": segment_payloads,
            },
        )


def _extract_xlsx_content(*, content: bytes, mime_type: str) -> ParsedContent:
    with _open_office_zip(content, expected_member="[Content_Types].xml") as archive:
        shared_strings = _xlsx_shared_strings(archive)
        sheet_names = _xlsx_sheet_names(archive)
        worksheet_names = sorted(
            (
                name
                for name in archive.namelist()
                if re.match(r"xl/worksheets/sheet\d+\.xml$", name)
            ),
            key=_natural_sort_key,
        )
        segments = []
        for sheet_index, worksheet_name in enumerate(worksheet_names):
            sheet_name = (
                sheet_names[sheet_index]
                if sheet_index < len(sheet_names)
                else f"Sheet{sheet_index + 1}"
            )
            rows, cell_refs = _xlsx_rows(
                archive.read(worksheet_name),
                shared_strings=shared_strings,
            )
            if not rows:
                continue
            text = _normalize_line("\n".join(rows))
            column_count = _xlsx_column_count(cell_refs)
            segments.append(
                _TextSegment(
                    text=text,
                    locator={
                        "format": "xlsx",
                        "sheet_name": sheet_name,
                        "cell_range": _cell_range(cell_refs),
                        "structure_kind": "sheet_table",
                        "table_block": "used_range",
                        "table_row_count": len(rows),
                        "table_column_count": column_count,
                    },
                )
            )
    text, segment_payloads = _join_segments(segments)
    return ParsedContent(
        text=text,
        title=_derive_title(text),
        source_type="office_document",
        metadata={
            **mime_policy_metadata(mime_type),
                "extractor": "xlsx_sheet_xml_text_v2",
                "parser_status": "success",
                "parser_kind": "xlsx",
                "content_type": mime_type,
                "text_length": len(text),
                "sheet_count": len(worksheet_names),
                "table_count": len(segment_payloads),
                "locator_reliability": "sheet_cell_reference",
                "citation_offsets_exact_in_extracted_text": True,
                "content_structure_hints": {
                    "sheet_count": len(worksheet_names),
                    "table_count": len(segment_payloads),
                    "table_row_count": sum(
                        int(segment.get("table_row_count") or 0) for segment in segment_payloads
                    ),
                    "table_column_count_max": max(
                        (
                            int(segment.get("table_column_count") or 0)
                            for segment in segment_payloads
                        ),
                        default=0,
                    ),
                    "structure_segment_count": len(segment_payloads),
                },
                "parser_warnings": [OFFICE_VISUAL_LAYOUT_FALLBACK_REASON],
                "structure_segments": segment_payloads,
            },
        )


def _open_office_zip(content: bytes, *, expected_member: str) -> ZipFile:
    try:
        archive = ZipFile(BytesIO(content))
    except BadZipFile as error:
        raise DocumentParseError("office_zip_signature_mismatch") from error
    if expected_member not in archive.namelist():
        archive.close()
        raise DocumentParseError(f"office_missing_member:{expected_member}")
    return archive


def _extract_pdf_streams(content: bytes) -> list[bytes]:
    """Extract raw PDF streams with a bounded linear scan.

    A whole-document DOTALL regular expression can exhibit catastrophic backtracking on
    image-heavy or malformed PDFs. Locate markers directly and cap dictionary look-behind.
    """
    streams: list[bytes] = []
    cursor = 0
    while True:
        stream_start = content.find(b"stream", cursor)
        if stream_start < 0:
            break
        dictionary_end = content.rfind(b">>", max(0, stream_start - 65_536), stream_start)
        dictionary_start = content.rfind(b"<<", max(0, dictionary_end - 65_536), dictionary_end)
        body_start = stream_start + len(b"stream")
        if content[body_start : body_start + 2] == b"\r\n":
            body_start += 2
        elif content[body_start : body_start + 1] in {b"\r", b"\n"}:
            body_start += 1
        stream_end = content.find(b"endstream", body_start)
        if dictionary_start < 0 or dictionary_end < dictionary_start or stream_end < 0:
            cursor = body_start
            continue
        stream_dict = content[dictionary_start + 2 : dictionary_end]
        body = content[body_start:stream_end].strip(b"\r\n")
        if b"/FlateDecode" in stream_dict:
            try:
                decompressor = zlib.decompressobj()
                body = decompressor.decompress(body, _MAX_PDF_STREAM_BYTES + 1)
            except zlib.error:
                cursor = stream_end + len(b"endstream")
                continue
            if len(body) > _MAX_PDF_STREAM_BYTES or decompressor.unconsumed_tail:
                cursor = stream_end + len(b"endstream")
                continue
        elif len(body) > _MAX_PDF_STREAM_BYTES:
            cursor = stream_end + len(b"endstream")
            continue
        streams.append(body)
        cursor = stream_end + len(b"endstream")
    return streams


def _extract_pdf_text_from_stream(stream: bytes) -> str:
    parts: list[str] = []
    for raw_text in re.findall(rb"\((?:\\.|[^\\()])*\)\s*Tj", stream):
        parts.append(_decode_pdf_string(raw_text[:-2].strip()))
    for raw_array in re.findall(rb"\[(.*?)\]\s*TJ", stream, flags=re.S):
        strings = re.findall(rb"\((?:\\.|[^\\()])*\)", raw_array)
        if strings:
            parts.append("".join(_decode_pdf_string(value) for value in strings))
    return _normalize_text("\n".join(part for part in parts if part.strip()))


def _extract_pdf_literal_text(content: bytes) -> str:
    parts = [_decode_pdf_string(value) for value in re.findall(rb"\((?:\\.|[^\\()])*\)", content)]
    return _normalize_text("\n".join(part for part in parts if len(part.strip()) >= 3))


def _decode_pdf_string(value: bytes) -> str:
    raw = value.strip()
    if raw.startswith(b"(") and raw.endswith(b")"):
        raw = raw[1:-1]
    raw = re.sub(rb"\\([nrtbf()\\])", _pdf_escape_replacement, raw)
    raw = re.sub(rb"\\([0-7]{1,3})", lambda match: bytes([int(match.group(1), 8)]), raw)
    return (
        raw.decode("utf-8", errors="replace")
        .encode("latin-1", errors="ignore")
        .decode("latin-1", errors="replace")
    )


def _pdf_escape_replacement(match: re.Match[bytes]) -> bytes:
    value = match.group(1)
    replacements = {
        b"n": b"\n",
        b"r": b"\r",
        b"t": b"\t",
        b"b": b"\b",
        b"f": b"\f",
        b"(": b"(",
        b")": b")",
        b"\\": b"\\",
    }
    return replacements.get(value, value)


def _iter_elements(root: ElementTree.Element, local_name: str) -> list[ElementTree.Element]:
    return [element for element in root.iter() if _local_name(element.tag) == local_name]


def _docx_paragraph_text(paragraph: ElementTree.Element) -> str:
    return _normalize_line("".join(node.text or "" for node in _iter_elements(paragraph, "t")))


def _docx_table_rows(table: ElementTree.Element) -> list[str]:
    rows: list[str] = []
    for row in (child for child in table if _local_name(child.tag) == "tr"):
        cells: list[str] = []
        for cell in (child for child in row if _local_name(child.tag) == "tc"):
            cell_text = _normalize_line(
                " ".join(
                    _normalize_line(node.text or "")
                    for node in _iter_elements(cell, "t")
                    if _normalize_line(node.text or "")
                )
            )
            if cell_text:
                cells.append(cell_text)
        if cells:
            rows.append(" | ".join(cells))
    return rows


def _xlsx_shared_strings(archive: ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
    strings: list[str] = []
    for item in _iter_elements(root, "si"):
        strings.append(
            _normalize_line("".join(node.text or "" for node in _iter_elements(item, "t")))
        )
    return strings


def _xlsx_sheet_names(archive: ZipFile) -> list[str]:
    if "xl/workbook.xml" not in archive.namelist():
        return []
    root = ElementTree.fromstring(archive.read("xl/workbook.xml"))
    return [
        str(sheet.attrib.get("name"))
        for sheet in _iter_elements(root, "sheet")
        if sheet.attrib.get("name")
    ]


def _xlsx_rows(worksheet_xml: bytes, *, shared_strings: list[str]) -> tuple[list[str], list[str]]:
    root = ElementTree.fromstring(worksheet_xml)
    rows: list[str] = []
    cell_refs: list[str] = []
    for row in _iter_elements(root, "row"):
        cells: list[str] = []
        for cell in _iter_elements(row, "c"):
            cell_ref = str(cell.attrib.get("r") or "")
            value = _xlsx_cell_text(cell, shared_strings=shared_strings)
            if not value:
                continue
            if cell_ref:
                cell_refs.append(cell_ref)
                cells.append(f"{cell_ref}: {value}")
            else:
                cells.append(value)
        if cells:
            rows.append(" | ".join(cells))
    return rows, cell_refs


def _xlsx_cell_text(cell: ElementTree.Element, *, shared_strings: list[str]) -> str:
    cell_type = cell.attrib.get("t")
    value_node = next((child for child in cell if _local_name(child.tag) == "v"), None)
    if value_node is not None and value_node.text is not None:
        raw_value = value_node.text.strip()
        if cell_type == "s":
            try:
                return shared_strings[int(raw_value)]
            except (ValueError, IndexError):
                return raw_value
        return raw_value
    inline = next((child for child in cell if _local_name(child.tag) == "is"), None)
    if inline is not None:
        return _normalize_line("".join(node.text or "" for node in _iter_elements(inline, "t")))
    return ""


def _cell_range(cell_refs: list[str]) -> str | None:
    if not cell_refs:
        return None
    return f"{cell_refs[0]}:{cell_refs[-1]}"


def _xlsx_column_count(cell_refs: list[str]) -> int:
    columns = {
        match.group(1).upper()
        for cell_ref in cell_refs
        if (match := re.match(r"([A-Z]+)\d+", cell_ref, flags=re.IGNORECASE))
    }
    return len(columns)


def _join_segments(segments: list[_TextSegment]) -> tuple[str, list[dict[str, Any]]]:
    parts: list[str] = []
    payloads: list[dict[str, Any]] = []
    cursor = 0
    for segment in segments:
        text = _normalize_text(segment.text)
        if not text:
            continue
        if parts:
            parts.append("")
            cursor += 2
        start = cursor
        parts.append(text)
        cursor += len(text)
        payloads.append({"start_offset": start, "end_offset": cursor, **segment.locator})
    return "\n\n".join(parts).strip(), payloads


def _derive_title(text: str) -> str | None:
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    return first_line[:200] if first_line else None


def _normalize_text(text: str) -> str:
    lines = [
        _normalize_line(line) for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    ]
    return "\n".join(line for line in lines if line).strip()


def _normalize_line(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _natural_sort_key(value: str) -> tuple[Any, ...]:
    return tuple(int(part) if part.isdigit() else part for part in re.split(r"(\d+)", value))
