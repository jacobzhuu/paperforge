"""文档抽取器测试（M0 验收：PDF 解析测试全绿）。

PDF/DOCX/XLSX 夹具在测试内确定性构造，不依赖外部样本文件。
"""

from __future__ import annotations

import io
import zlib
from zipfile import ZipFile

import pytest
from ingest import (
    DocumentParseError,
    UnsupportedMimeTypeError,
    assess_chunk_quality,
    extract_and_chunk,
    extract_content,
    extract_html_content,
    normalize_mime_type,
    try_extract_and_chunk,
)

_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>'
)

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _minimal_pdf(lines: list[str]) -> bytes:
    """构造一个含单个文本流的极简 PDF（Tj 操作符路径）。"""
    text_ops = "\n".join(f"({line}) Tj" for line in lines)
    stream = f"BT\n/F1 12 Tf\n{text_ops}\nET".encode()
    compressed = zlib.compress(stream)
    return (
        b"%PDF-1.4\n"
        b"1 0 obj\n<< /Type /Catalog >>\nendobj\n"
        b"2 0 obj\n<< /Length "
        + str(len(compressed)).encode()
        + b" /Filter /FlateDecode >>\nstream\n"
        + compressed
        + b"\nendstream\nendobj\n"
        b"trailer\n<< /Root 1 0 R >>\n%%EOF\n"
    )


def _minimal_docx(paragraphs: list[str]) -> bytes:
    body = "".join(
        f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in paragraphs
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    buffer = io.BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        archive.writestr("word/document.xml", document)
    return buffer.getvalue()


def _minimal_xlsx(rows: list[list[str]]) -> bytes:
    def cell(ref: str, value: str) -> str:
        return f'<c r="{ref}" t="inlineStr"><is><t>{value}</t></is></c>'

    columns = "ABCDEFGH"
    row_xml = "".join(
        f'<row r="{index}">'
        + "".join(cell(f"{columns[col]}{index}", value) for col, value in enumerate(row))
        + "</row>"
        for index, row in enumerate(rows, start=1)
    )
    sheet = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{row_xml}</sheetData></worksheet>"
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="Results" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    buffer = io.BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
    return buffer.getvalue()


def test_pdf_text_is_extracted_from_content_stream() -> None:
    content = _minimal_pdf(["Retrieval augmented generation", "achieves strong results."])
    parsed = extract_content(mime_type="application/pdf", content=content)
    assert "Retrieval augmented generation" in parsed.text
    assert parsed.source_type == "pdf_document"
    # 安全语义写进元数据：不执行宏、不加载外部资源。
    assert parsed.metadata["mime_policy"]["office_macros_executed"] is False


def test_pdf_extraction_and_chunking_produces_chunks() -> None:
    lines = [f"Sentence number {index} about scaling laws." for index in range(60)]
    parsed, chunks = extract_and_chunk(
        mime_type="application/pdf",
        content=_minimal_pdf(lines),
        max_chars_per_chunk=200,
    )
    assert parsed.text
    assert len(chunks) > 1
    assert all(chunk.token_count > 0 for chunk in chunks)
    assert [chunk.chunk_no for chunk in chunks] == list(range(len(chunks)))


def test_docx_paragraphs_are_extracted() -> None:
    parsed = extract_content(
        mime_type=DOCX_MIME,
        content=_minimal_docx(["Method overview", "We trained on 3 datasets."]),
    )
    assert "Method overview" in parsed.text
    assert "We trained on 3 datasets." in parsed.text
    assert parsed.source_type == "office_document"


def test_xlsx_rows_are_extracted_for_result_tables() -> None:
    parsed = extract_content(
        mime_type=XLSX_MIME,
        content=_minimal_xlsx([["model", "accuracy"], ["ours", "0.913"]]),
    )
    # 研究型管线的数字硬规则依赖表格解析：数值必须原样出现。
    assert "0.913" in parsed.text
    assert "accuracy" in parsed.text
    assert parsed.source_type == "office_document"


def test_html_extraction_drops_script_and_nav_noise() -> None:
    html = """
    <html><head><title>A Paper</title></head>
    <body>
      <script>window.tracker = 1;</script>
      <p>We introduce a new method.</p>
      <p>Results show improvement.</p>
    </body></html>
    """
    parsed = extract_html_content(content=html)
    assert parsed.title == "A Paper"
    assert "window.tracker" not in parsed.text
    assert "We introduce a new method." in parsed.text


def test_markdown_is_extracted_as_plain_text() -> None:
    parsed = extract_content(mime_type="text/markdown", content=b"# Title\n\nBody text.")
    assert parsed.title == "Title"
    assert "Body text." in parsed.text


def test_unsupported_mime_raises() -> None:
    with pytest.raises(UnsupportedMimeTypeError):
        extract_content(mime_type="application/zip", content=b"PK\x03\x04")


def test_corrupt_docx_raises_document_parse_error() -> None:
    with pytest.raises(DocumentParseError):
        extract_content(mime_type=DOCX_MIME, content=b"not a zip file")


def test_try_extract_degrades_instead_of_raising() -> None:
    parsed, chunks, error = try_extract_and_chunk(
        mime_type="application/zip",
        content=b"PK\x03\x04",
    )
    # Draft-first：解析失败降级为「无全文」，不阻断 INGEST 阶段。
    assert parsed is None
    assert chunks == []
    assert error is not None and error["error"] == "UnsupportedMimeTypeError"


def test_normalize_mime_type_strips_parameters() -> None:
    assert normalize_mime_type("text/html; charset=utf-8") == "text/html"


def test_reference_section_chunk_is_excluded_from_cards() -> None:
    quality = assess_chunk_quality(
        text=(
            "References. Smith J. et al. Deep learning. In Proceedings of NeurIPS, "
            "vol. 33, pp. 1-12, 2020. doi:10.1000/x"
        )
    )
    assert quality.is_reference_section is True
    assert quality.usable_for_cards is False


def test_substantive_paragraph_is_usable_for_cards() -> None:
    quality = assess_chunk_quality(
        text=(
            "We propose a retrieval augmented architecture that separates evidence "
            "selection from generation, and evaluate it on three public benchmarks "
            "covering open-domain question answering and summarization."
        ),
        query="retrieval augmented generation",
    )
    assert quality.usable_for_cards is True
    assert quality.query_relevance_score > 0
    assert quality.is_reference_section is False


def test_navigation_noise_is_flagged() -> None:
    quality = assess_chunk_quality(
        text="Skip to main content. Sign in. Cookie policy. Privacy policy. Back to top."
    )
    assert quality.is_navigation_noise is True
    assert quality.usable_for_cards is False


def test_pdf_falls_back_to_operator_scan_when_pypdf_yields_nothing() -> None:
    """极简 PDF 无字体资源，pypdf 取不到文本；兜底的操作符扫描仍能出正文。"""
    parsed = extract_content(
        mime_type="application/pdf",
        content=_minimal_pdf(["Fallback path still extracts text"]),
    )
    assert "Fallback path still extracts text" in parsed.text
    assert parsed.metadata["extractor"] == "pdf_text_stream_v1"


def test_non_pdf_bytes_rejected_by_signature_check() -> None:
    with pytest.raises(DocumentParseError, match="pdf_signature_mismatch"):
        extract_content(mime_type="application/pdf", content=b"not a pdf")


def test_encrypted_pdf_is_not_decrypted() -> None:
    """合规红线：不绕任何访问控制，加密 PDF 直接走兜底而非尝试解密。"""
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.encrypt("owner-password")
    buffer = io.BytesIO()
    writer.write(buffer)

    parsed = extract_content(mime_type="application/pdf", content=buffer.getvalue())
    # 兜底路径拿不到正文，但不抛出、不解密（draft-first 降级为「无全文」）。
    assert parsed.metadata["extractor"] == "pdf_text_stream_v1"
