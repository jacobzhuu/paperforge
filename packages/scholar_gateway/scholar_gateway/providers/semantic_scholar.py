"""Semantic Scholar 检索适配器（迁移自 DeepSearch adapters.py S2 部分）。

改动：API key 走 ProviderConfig 注入；去 occurrence 落账。
匿名共享池限流极严，因此默认按凭据档位 pacing，并对匿名访问严格单飞。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from scholar_gateway.models import ScholarlyDiscoveryQuery, ScholarlyDiscoveryResult
from scholar_gateway.providers.base import (
    FetchedHttpResponse,
    HttpScholarlyDiscoveryAdapter,
    ProviderConfig,
)
from scholar_gateway.providers.mapping import (
    candidate_from_semantic_scholar_item,
    dict_without,
    int_or_none,
)
from scholar_gateway.providers.query_syntax import semantic_scholar_time_params
from scholar_gateway.runtime import (
    SEMANTIC_SCHOLAR_CIRCUIT_BREAKER_COOLDOWN_SECONDS,
    SEMANTIC_SCHOLAR_CIRCUIT_BREAKER_THRESHOLD,
    semantic_scholar_min_request_interval,
)

SEARCH_FIELDS = (
    "paperId,corpusId,externalIds,url,title,abstract,year,publicationDate,"
    "venue,publicationTypes,authors,openAccessPdf,influentialCitationCount,"
    "citationCount,referenceCount"
)


class SemanticScholarDiscoveryAdapter(HttpScholarlyDiscoveryAdapter):
    name = "semantic_scholar"
    provider_name = "semantic_scholar"
    base_url = "https://api.semanticscholar.org"

    def default_min_request_interval(self, config: ProviderConfig) -> float:
        return semantic_scholar_min_request_interval(api_key=config.api_key)

    def default_circuit_breaker_threshold(self) -> int:
        return SEMANTIC_SCHOLAR_CIRCUIT_BREAKER_THRESHOLD

    def default_circuit_breaker_cooldown_seconds(self) -> float:
        return SEMANTIC_SCHOLAR_CIRCUIT_BREAKER_COOLDOWN_SECONDS

    def default_serialize_requests(self, config: ProviderConfig) -> bool:
        # 匿名共享池严格单飞（并发=1）。
        return not (config.api_key or "").strip()

    def _request_headers(self) -> dict[str, str]:
        headers = super()._request_headers()
        if self._api_key:
            headers["x-api-key"] = self._api_key
        return headers

    def _request(self, query: ScholarlyDiscoveryQuery) -> tuple[str, dict[str, Any]]:
        # Cap length — S2 silently 400s on extremely long boolean strings.
        text = (query.query_text or "").strip()[:300]
        params: dict[str, Any] = {
            "query": text,
            "limit": max(0, min(query.limit, 100)),
            "offset": max(0, query.offset),
            "fields": SEARCH_FIELDS,
        }
        params.update(semantic_scholar_time_params(query.filters))
        return f"{self.base_url}/graph/v1/paper/search", params

    def discover(self, query: ScholarlyDiscoveryQuery) -> ScholarlyDiscoveryResult:
        # Empty queries burn the anonymous shared pool and open the circuit (N3).
        if not (query.query_text or "").strip():
            retrieved_at = datetime.now(UTC)
            return self._error_result(
                query=query,
                retrieved_at=retrieved_at,
                error_code="empty_query",
                message="semantic_scholar query must be non-empty",
                retryable=False,
            )
        return super().discover(query)

    def rate_limit_hint(self, fetched: FetchedHttpResponse) -> str | None:
        if self._api_key:
            return None
        return (
            "semantic_scholar unauthenticated shared pool is heavily rate limited; "
            "set SEMANTIC_SCHOLAR_API_KEY to use a dedicated quota."
        )

    def map_payload(
        self,
        *,
        query: ScholarlyDiscoveryQuery,
        payload: dict[str, Any],
        retrieved_at: datetime | None = None,
        request_metadata: dict[str, Any] | None = None,
    ) -> ScholarlyDiscoveryResult:
        retrieved = retrieved_at or datetime.now(UTC)
        raw_results = payload.get("data")
        candidates = tuple(
            mapped
            for item in raw_results or []
            if isinstance(item, dict)
            if (mapped := candidate_from_semantic_scholar_item(query, item, retrieved)) is not None
        )
        return ScholarlyDiscoveryResult(
            provider_name=self.provider_name,
            query=query,
            retrieved_at=retrieved,
            candidates=candidates,
            hit_count=int_or_none(payload.get("total")),
            metadata={
                **dict_without(payload, "data"),
                **(request_metadata or {}),
                "provider_returned_count": len(raw_results or []),
            },
        )
