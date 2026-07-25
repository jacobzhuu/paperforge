"""arXiv Atom 检索适配器（迁移自 DeepSearch adapters.py arXiv 部分）。

arXiv 返回 Atom XML 而非 JSON，因此覆盖 discover() 走 XML 解析路径，
但复用基类的缓存/限速/重试 GET。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from scholar_gateway.models import (
    ScholarlyDiscoveryError,
    ScholarlyDiscoveryQuery,
    ScholarlyDiscoveryResult,
)
from scholar_gateway.providers.base import HttpScholarlyDiscoveryAdapter
from scholar_gateway.providers.mapping import (
    arxiv_atom_entry_count,
    arxiv_atom_total_results,
    candidates_from_arxiv_atom,
)
from scholar_gateway.providers.query_syntax import (
    arxiv_search_query,
    arxiv_submitted_date_clause,
)
from scholar_gateway.runtime import ProviderCircuitOpenError


class ArxivDiscoveryAdapter(HttpScholarlyDiscoveryAdapter):
    """arXiv Atom API：CS/物理方向时效性预印本发现。"""

    name = "arxiv"
    provider_name = "arxiv"
    base_url = "https://export.arxiv.org/api"

    def _request(self, query: ScholarlyDiscoveryQuery) -> tuple[str, dict[str, Any]]:
        search_query = arxiv_search_query(query.query_text)
        date_clause = arxiv_submitted_date_clause(query.filters)
        if date_clause:
            search_query = f"({search_query}) AND {date_clause}"
        return f"{self.base_url}/query", {
            "search_query": search_query,
            "start": max(0, query.offset),
            "max_results": max(0, query.limit),
            "sortBy": "relevance",
            "sortOrder": "descending",
        }

    def discover(self, query: ScholarlyDiscoveryQuery) -> ScholarlyDiscoveryResult:
        retrieved_at = datetime.now(UTC)
        if self._client is None:
            return self._not_configured(query, retrieved_at)
        request_url, params = self._request(query)
        headers = self._request_headers()
        fetched, fetch_error = self._fetch_http(request_url, params, headers)
        if fetched is None:
            circuit_open = isinstance(fetch_error, ProviderCircuitOpenError)
            return ScholarlyDiscoveryResult(
                provider_name=self.provider_name,
                query=query,
                retrieved_at=retrieved_at,
                errors=(
                    ScholarlyDiscoveryError(
                        provider_name=self.provider_name,
                        error_code="circuit_open" if circuit_open else "arxiv_http_error",
                        message=str(fetch_error),
                        status_code=429 if circuit_open else None,
                        retryable=True,
                        metadata=(
                            {"rate_limited": True, "fallback_reason": "circuit_open"}
                            if circuit_open
                            else {}
                        ),
                    ),
                ),
                metadata={"cache_hit": False},
            )
        if fetched.status_code >= 400:
            return ScholarlyDiscoveryResult(
                provider_name=self.provider_name,
                query=query,
                retrieved_at=retrieved_at,
                errors=(
                    ScholarlyDiscoveryError(
                        provider_name=self.provider_name,
                        error_code="arxiv_http_error",
                        message=f"arxiv returned HTTP {fetched.status_code}.",
                        status_code=fetched.status_code,
                        retryable=fetched.status_code in {408, 429}
                        or fetched.status_code >= 500,
                    ),
                ),
                metadata={"cache_hit": fetched.cache_hit},
            )
        candidates = candidates_from_arxiv_atom(query, fetched.text, retrieved_at)
        total_results = arxiv_atom_total_results(fetched.text)
        return ScholarlyDiscoveryResult(
            provider_name=self.provider_name,
            query=query,
            retrieved_at=retrieved_at,
            candidates=candidates,
            hit_count=total_results,
            metadata={
                "format": "atom",
                "request_params": params,
                "cache_hit": fetched.cache_hit,
                "provider_returned_count": arxiv_atom_entry_count(fetched.text),
                "total_results_reported": total_results,
            },
        )

    def map_payload(
        self,
        *,
        query: ScholarlyDiscoveryQuery,
        payload: dict[str, Any],
        retrieved_at: datetime | None = None,
        request_metadata: dict[str, Any] | None = None,
    ) -> ScholarlyDiscoveryResult:
        # Atom 路径不使用；保留以满足适配器协议。
        del payload, request_metadata
        return ScholarlyDiscoveryResult(
            provider_name=self.provider_name,
            query=query,
            retrieved_at=retrieved_at or datetime.now(UTC),
            candidates=(),
            hit_count=0,
            metadata={"format": "atom_unsupported_json_map"},
        )
