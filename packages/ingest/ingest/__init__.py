"""PaperForge 文档摄取：PDF/DOCX/PPTX/XLSX/HTML 抽取、稳定切块、section 感知选块、
切块质量评分。

迁移自 DeepSearch parsing/ 与 literature_review/section_chunks.py（设计 §3.1 / §9）。
"""

from ingest.assets import (
    ASSET_KINDS,
    ParsedAsset,
    infer_asset_kind,
    parse_asset,
)
from ingest.bibliography import (
    PdfBibliographicMetadata,
    extract_pdf_bibliographic_metadata,
    infer_pdf_bibliographic_metadata,
)
from ingest.chunking import DEFAULT_MAX_CHARS_PER_CHUNK, ParsedChunk, chunk_text
from ingest.document_extractors import (
    SUPPORTED_DOCUMENT_MIME_TYPES,
    DocumentParseError,
    extract_document_content,
    mime_policy_metadata,
    normalize_mime_type,
)
from ingest.extract import (
    JATS_MIME_TYPES,
    LATEX_SOURCE_MIME_TYPES,
    SUPPORTED_MIME_TYPES,
    extract_and_chunk,
    extract_content,
    try_extract_and_chunk,
)
from ingest.jats import extract_jats_content
from ingest.latex_source import extract_latex_source_content
from ingest.numlint import (
    NumberFinding,
    NumLintReport,
    build_asset_index,
    lint_sections,
    lint_text,
)
from ingest.pdf import extract_pdf_content
from ingest.quality import ChunkQuality, assess_chunk_quality
from ingest.section_chunks import (
    ChunkText,
    ContentRole,
    ContentRoleDecision,
    StructuredTableEvidence,
    classify_content_role,
    parse_markdown_table,
    score_chunk_for_section_group,
    select_section_aware_chunks,
)
from ingest.text_extract import extract_html_content, extract_plain_text_content
from ingest.types import ParsedContent, UnsupportedMimeTypeError

__all__ = [
    "ParsedAsset",
    "PdfBibliographicMetadata",
    "NumberFinding",
    "NumLintReport",
    "ASSET_KINDS",
    "DEFAULT_MAX_CHARS_PER_CHUNK",
    "SUPPORTED_DOCUMENT_MIME_TYPES",
    "SUPPORTED_MIME_TYPES",
    "JATS_MIME_TYPES",
    "LATEX_SOURCE_MIME_TYPES",
    "ChunkQuality",
    "ChunkText",
    "ContentRole",
    "ContentRoleDecision",
    "DocumentParseError",
    "ParsedChunk",
    "ParsedContent",
    "StructuredTableEvidence",
    "UnsupportedMimeTypeError",
    "assess_chunk_quality",
    "build_asset_index",
    "infer_asset_kind",
    "infer_pdf_bibliographic_metadata",
    "lint_sections",
    "lint_text",
    "parse_asset",
    "chunk_text",
    "classify_content_role",
    "extract_and_chunk",
    "extract_content",
    "extract_document_content",
    "extract_html_content",
    "extract_jats_content",
    "extract_latex_source_content",
    "extract_pdf_content",
    "extract_pdf_bibliographic_metadata",
    "extract_plain_text_content",
    "mime_policy_metadata",
    "normalize_mime_type",
    "parse_markdown_table",
    "score_chunk_for_section_group",
    "select_section_aware_chunks",
    "try_extract_and_chunk",
]
