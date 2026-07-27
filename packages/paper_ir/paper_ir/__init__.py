"""PaperForge PaperIR：结构化论文 IR、引用样式、确定性 BibTeX、i18n。

迁移自 DeepSearch（citation_format / report_i18n / evidence_matrix 简化）
+ 新写（PaperIR schema、bibtex）。
"""

from paper_ir.bibtex import (
    BibtexImportEntry,
    make_bibtex_key,
    parse_bibtex_entries,
    render_bibtex,
)
from paper_ir.citation_style import (
    CitationStyle,
    format_scholarly_reference,
    normalize_citation_style,
)
from paper_ir.i18n import (
    normalize_lr_report_language,
    resolve_report_language,
    t,
)
from paper_ir.markdown import render_markdown
from paper_ir.reference import ReferenceMetadata
from paper_ir.schema import (
    Bibliography,
    Block,
    CitationWarning,
    CiteRun,
    FigureBlock,
    PaperIR,
    PaperIRCiteKeyViolation,
    PaperMeta,
    ParagraphBlock,
    Section,
    TableBlock,
    TableSource,
    TextRun,
    XRefRun,
)

__all__ = [
    "Bibliography",
    "BibtexImportEntry",
    "Block",
    "CitationStyle",
    "CitationWarning",
    "CiteRun",
    "FigureBlock",
    "PaperIR",
    "PaperIRCiteKeyViolation",
    "PaperMeta",
    "ParagraphBlock",
    "ReferenceMetadata",
    "Section",
    "TableBlock",
    "TableSource",
    "TextRun",
    "XRefRun",
    "format_scholarly_reference",
    "make_bibtex_key",
    "parse_bibtex_entries",
    "normalize_citation_style",
    "normalize_lr_report_language",
    "render_bibtex",
    "render_markdown",
    "resolve_report_language",
    "t",
]
