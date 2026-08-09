"""统一抽取入口：mime → ParsedContent → 切块。

服务综述管线 INGEST（OA 全文）与研究型管线 INPUT（用户素材）。
"""

from __future__ import annotations

from typing import Any

from ingest.chunking import DEFAULT_MAX_CHARS_PER_CHUNK, ParsedChunk, chunk_text
from ingest.document_extractors import (
    SUPPORTED_DOCUMENT_MIME_TYPES,
    DocumentParseError,
    extract_document_content,
    mime_policy_metadata,
    normalize_mime_type,
)
from ingest.jats import extract_jats_content
from ingest.latex_source import extract_latex_source_content
from ingest.pdf import extract_pdf_content
from ingest.text_extract import (
    HTML_MIME_TYPES,
    PLAIN_TEXT_MIME_TYPES,
    extract_html_content,
    extract_plain_text_content,
)
from ingest.types import ParsedContent, UnsupportedMimeTypeError

JATS_MIME_TYPES = frozenset(
    {"application/xml", "text/xml", "application/jats+xml", "application/x-jats+xml"}
)
LATEX_SOURCE_MIME_TYPES = frozenset(
    {"application/x-arxiv-source", "application/x-tex", "text/x-tex"}
)
SUPPORTED_MIME_TYPES = (
    frozenset(SUPPORTED_DOCUMENT_MIME_TYPES)
    | HTML_MIME_TYPES
    | PLAIN_TEXT_MIME_TYPES
    | JATS_MIME_TYPES
    | LATEX_SOURCE_MIME_TYPES
)


def extract_content(*, mime_type: str, content: bytes) -> ParsedContent:
    """按 mime 分派到确定性抽取器。不支持的类型抛 UnsupportedMimeTypeError。"""
    normalized = normalize_mime_type(mime_type)
    if normalized == "application/pdf":
        # PDF 走 pypdf 优先路径（见 ingest/pdf.py：标准库操作符扫描对真实学术
        # PDF 取不全正文，而文献卡片质量直接依赖全文）。
        parsed = extract_pdf_content(content=content, mime_type=normalized)
    elif normalized in JATS_MIME_TYPES:
        parsed = extract_jats_content(content=content, mime_type=normalized)
    elif normalized in LATEX_SOURCE_MIME_TYPES:
        parsed = extract_latex_source_content(content=content, mime_type=normalized)
    elif normalized in SUPPORTED_DOCUMENT_MIME_TYPES:
        parsed = extract_document_content(mime_type=normalized, content=content)
    elif normalized in HTML_MIME_TYPES:
        parsed = extract_html_content(content=content, mime_type=normalized)
    elif normalized in PLAIN_TEXT_MIME_TYPES:
        parsed = extract_plain_text_content(content=content, mime_type=normalized)
    else:
        raise UnsupportedMimeTypeError(normalized)
    return ParsedContent(
        text=parsed.text,
        title=parsed.title,
        source_type=parsed.source_type,
        metadata={**parsed.metadata, **mime_policy_metadata(normalized)},
    )


def extract_and_chunk(
    *,
    mime_type: str,
    content: bytes,
    max_chars_per_chunk: int = DEFAULT_MAX_CHARS_PER_CHUNK,
) -> tuple[ParsedContent, list[ParsedChunk]]:
    parsed = extract_content(mime_type=mime_type, content=content)
    return parsed, chunk_text(parsed.text, max_chars_per_chunk=max_chars_per_chunk)


def try_extract_and_chunk(
    *,
    mime_type: str,
    content: bytes,
    max_chars_per_chunk: int = DEFAULT_MAX_CHARS_PER_CHUNK,
) -> tuple[ParsedContent | None, list[ParsedChunk], dict[str, Any] | None]:
    """Draft-first 包装：解析失败返回 (None, [], error)，绝不抛出。"""
    try:
        parsed, chunks = extract_and_chunk(
            mime_type=mime_type,
            content=content,
            max_chars_per_chunk=max_chars_per_chunk,
        )
    except (UnsupportedMimeTypeError, DocumentParseError) as error:
        return None, [], {"error": type(error).__name__, "message": str(error)[:300]}
    except Exception as error:  # noqa: BLE001 - 解析失败降级为「无全文」，不阻断管线
        return None, [], {"error": type(error).__name__, "message": str(error)[:300]}
    return parsed, chunks, None
