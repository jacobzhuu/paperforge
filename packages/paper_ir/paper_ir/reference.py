from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 简化自 DeepSearch evidence_matrix.ReviewEvidenceCard（方案 §3.2：改造为 literature_card /
# 参考文献元数据）。去掉 screening/findings/evidence_pointers/provenance 等溯源审计字段，
# 仅保留确定性参考文献生成（R3）与引用格式化所需的字段。这是 R3 的库内元数据来源。


@dataclass(frozen=True)
class ReferenceMetadata:
    """一条真实文献的参考文献级元数据（引用格式化与 BibTeX 生成的输入）。

    ``citation_metadata`` 携带 ``authors``（list[dict{author_name, author_order?}]）
    与可选 URL 键（landing_page_url / url / provider_record_url）。
    """

    work_key: str
    # R3 single source of truth: assigned once when a verified library entry is created.
    bibtex_key: str | None = None
    title: str | None = None
    normalized_title: str | None = None
    publication_year: int | None = None
    venue_name: str | None = None
    publisher: str | None = None
    doi: str | None = None
    pmid: str | None = None
    pmcid: str | None = None
    arxiv_id: str | None = None
    openalex_id: str | None = None
    semantic_scholar_id: str | None = None
    corpus_id: str | None = None
    work_type: str | None = None
    language: str | None = None
    citation_metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def authors(self) -> list[dict[str, Any]]:
        value = self.citation_metadata.get("authors")
        return value if isinstance(value, list) else []
