"""OpenAlex 检索适配器（迁移自 DeepSearch adapters.py OpenAlex 部分）。

改动：polite pool 邮箱与 API key 走 ProviderConfig 注入；去 occurrence 落账。
熔断默认开启：OpenAlex 按次计费且有日预算，连续 429 基本等于预算耗尽，
盲目重试只是浪费墙钟时间（详见 runtime.py 注释）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from scholar_gateway.models import ScholarlyDiscoveryQuery, ScholarlyDiscoveryResult
from scholar_gateway.providers.base import (
    FetchedHttpResponse,
    HttpScholarlyDiscoveryAdapter,
)
from scholar_gateway.providers.mapping import candidate_from_openalex_item, int_or_none
from scholar_gateway.providers.query_syntax import (
    merge_filter_parts,
    openalex_date_filter,
    openalex_search_and_filters,
)
from scholar_gateway.runtime import (
    OPENALEX_CIRCUIT_BREAKER_COOLDOWN_SECONDS,
    OPENALEX_CIRCUIT_BREAKER_THRESHOLD,
)


class OpenAlexDiscoveryAdapter(HttpScholarlyDiscoveryAdapter):
    name = "openalex"
    provider_name = "openalex"
    base_url = "https://api.openalex.org"

    def default_circuit_breaker_threshold(self) -> int:
        return OPENALEX_CIRCUIT_BREAKER_THRESHOLD

    def default_circuit_breaker_cooldown_seconds(self) -> float:
        return OPENALEX_CIRCUIT_BREAKER_COOLDOWN_SECONDS

    def _request_headers(self) -> dict[str, str]:
        headers = super()._request_headers()
        contact_email = (self._config.contact_email or "").strip()
        if contact_email and "mailto" not in headers.get("User-Agent", ""):
            headers["User-Agent"] = (
                f"{headers.get('User-Agent', 'PaperForge')} (mailto:{contact_email})"
            )
        return headers

    def _auth_params(self) -> dict[str, str]:
        return {"api_key": self._api_key} if self._api_key else {}

    def _request(self, query: ScholarlyDiscoveryQuery) -> tuple[str, dict[str, Any]]:
        search_text, native_filter_parts = openalex_search_and_filters(query.query_text)
        params: dict[str, Any] = {
            "search": search_text,
            "per-page": max(0, query.limit),
            # OpenAlex 游标翻页必须显式以 ``*`` 初始化。
            "cursor": query.cursor or "*",
        }
        date_filter = openalex_date_filter(query.filters)
        merged_filter = merge_filter_parts(
            date_filter.split(",") if date_filter else None,
            native_filter_parts,
        )
        if merged_filter:
            params["filter"] = merged_filter
        contact_email = (self._config.contact_email or "").strip()
        if contact_email:
            params["mailto"] = contact_email
        return f"{self.base_url}/works", params

    def rate_limit_hint(self, fetched: FetchedHttpResponse) -> str | None:
        if fetched.daily_quota_exhausted and self._api_key:
            return (
                "OpenAlex daily API budget is exhausted; wait until midnight UTC for "
                "reset or add prepaid funds before retrying."
            )
        if not self._api_key:
            return (
                "openalex keyless access is capped at $0.10/day (~100 search calls, "
                "resets midnight UTC); set OPENALEX_API_KEY (free at "
                "openalex.org/settings/api) for 10x the daily budget."
            )
        return None

    def map_payload(
        self,
        *,
        query: ScholarlyDiscoveryQuery,
        payload: dict[str, Any],
        retrieved_at: datetime | None = None,
        request_metadata: dict[str, Any] | None = None,
    ) -> ScholarlyDiscoveryResult:
        retrieved = retrieved_at or datetime.now(UTC)
        raw_results = payload.get("results")
        candidates = tuple(
            mapped
            for item in raw_results or []
            if isinstance(item, dict)
            if (mapped := candidate_from_openalex_item(query, item, retrieved)) is not None
        )
        meta = payload.get("meta")
        hit_count = int_or_none(meta.get("count")) if isinstance(meta, dict) else None
        next_cursor = meta.get("next_cursor") if isinstance(meta, dict) else None
        return ScholarlyDiscoveryResult(
            provider_name=self.provider_name,
            query=query,
            retrieved_at=retrieved,
            candidates=candidates,
            hit_count=hit_count,
            metadata={
                **(meta if isinstance(meta, dict) else {}),
                **(request_metadata or {}),
                "next_cursor": next_cursor,
                "provider_returned_count": len(raw_results or []),
            },
        )
