from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

# 迁移自 DeepSearch literature_review/discovery.py 的候选实体族与检索契约。
# 改动：去掉 ledger/occurrence 相关字段（research_task_id / lane 语义不落账）；
# 字段名与 db.models.library 对齐（url_type / raw_affiliation），
# 作为 scholar_gateway 内部与去重/雪球/入库共享的纯数据模型。


@dataclass(frozen=True)
class ScholarlyIdentifier:
    id_type: str
    id_value: str
    is_primary: bool = False


@dataclass(frozen=True)
class ScholarlyAuthorCandidate:
    author_name: str
    author_order: int
    raw_affiliation: str | None = None


@dataclass(frozen=True)
class ScholarlyLinkCandidate:
    url: str
    url_type: str | None = None
    source_name: str | None = None
    is_oa: bool = False


@dataclass(frozen=True)
class ScholarlyWorkCandidate:
    title: str
    normalized_title: str
    normalized_title_hash: str
    provider_name: str
    provider_record_id: str | None
    provider_record_url: str | None
    query_text: str
    retrieved_at: datetime
    source_strategy_id: str | None = None
    abstract: str | None = None
    publication_year: int | None = None
    publication_date: str | None = None
    work_type: str | None = None
    venue_name: str | None = None
    publisher: str | None = None
    language: str | None = None
    doi: str | None = None
    pmid: str | None = None
    pmcid: str | None = None
    arxiv_id: str | None = None
    openalex_id: str | None = None
    semantic_scholar_id: str | None = None
    corpus_id: str | None = None
    oa_status: str | None = None
    license: str | None = None
    is_retracted: bool = False
    citation_count: int | None = None
    influential_citation_count: int | None = None
    identifiers: tuple[ScholarlyIdentifier, ...] = ()
    links: tuple[ScholarlyLinkCandidate, ...] = ()
    authors: tuple[ScholarlyAuthorCandidate, ...] = ()
    raw_provider_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ScholarlyDiscoveryQuery:
    """一次 provider 检索请求。filters 支持 ``time_range`` 子字典（年/日精度）。"""

    query_text: str
    limit: int = 20
    filters: dict[str, Any] = field(default_factory=dict)
    source_strategy_id: str | None = None
    language: str | None = None
    cursor: str | None = None
    offset: int = 0
    page: int = 1


@dataclass(frozen=True)
class ScholarlyDiscoveryError:
    provider_name: str
    error_code: str
    message: str
    status_code: int | None = None
    retryable: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "provider": self.provider_name,
            "error_code": self.error_code,
            "message": self.message,
            "status_code": self.status_code,
            "retryable": self.retryable,
        }
        if self.metadata:
            payload["metadata"] = self.metadata
        return payload


@dataclass(frozen=True)
class ScholarlyDiscoveryResult:
    provider_name: str
    query: ScholarlyDiscoveryQuery
    retrieved_at: datetime
    candidates: tuple[ScholarlyWorkCandidate, ...] = ()
    errors: tuple[ScholarlyDiscoveryError, ...] = ()
    hit_count: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def status(self) -> str:
        """Draft-first：部分失败仍交付已拿到的候选，不阻断上层管线。"""
        if self.errors and self.candidates:
            return "partial"
        if self.errors:
            return "failed"
        return "completed"

    def diagnostics(self) -> dict[str, Any]:
        return {
            "provider": self.provider_name,
            "status": self.status,
            "query_text": self.query.query_text,
            "candidate_count": len(self.candidates),
            "hit_count": self.hit_count,
            "errors": [error.to_payload() for error in self.errors],
            "metadata": self.metadata,
        }


class ScholarlyDiscoveryAdapter(Protocol):
    name: str
    provider_name: str

    def discover(self, query: ScholarlyDiscoveryQuery) -> ScholarlyDiscoveryResult: ...
