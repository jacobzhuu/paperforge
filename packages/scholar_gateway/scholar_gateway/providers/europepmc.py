"""Europe PMC 检索适配器（迁移自 DeepSearch adapters.py EuropePMC 部分）。

配额宽松，设计 §7 建议优先使用。改动：去 occurrence 落账；
时间窗以 FIRST_PDATE 子句进查询串（Europe PMC 无独立 filter 参数）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from scholar_gateway.models import ScholarlyDiscoveryQuery, ScholarlyDiscoveryResult
from scholar_gateway.providers.base import HttpScholarlyDiscoveryAdapter
from scholar_gateway.providers.mapping import candidate_from_europe_pmc_item, int_or_none
from scholar_gateway.providers.query_syntax import europe_pmc_date_clause


class EuropePmcDiscoveryAdapter(HttpScholarlyDiscoveryAdapter):
    """Europe PMC REST search：生物医学文献。"""

    name = "europe_pmc"
    provider_name = "europe_pmc"
    base_url = "https://www.ebi.ac.uk/europepmc/webservices/rest"

    def _request(self, query: ScholarlyDiscoveryQuery) -> tuple[str, dict[str, Any]]:
        query_text = query.query_text
        date_clause = europe_pmc_date_clause(query.filters)
        if date_clause:
            query_text = f"({query_text}) AND {date_clause}"
        return f"{self.base_url}/search", {
            "query": query_text,
            "format": "json",
            "pageSize": max(1, min(query.limit, 100)),
            # Europe PMC 分页基于游标，``*`` 起链。
            "cursorMark": query.cursor or "*",
            "resultType": "core",
        }

    def map_payload(
        self,
        *,
        query: ScholarlyDiscoveryQuery,
        payload: dict[str, Any],
        retrieved_at: datetime | None = None,
        request_metadata: dict[str, Any] | None = None,
    ) -> ScholarlyDiscoveryResult:
        retrieved = retrieved_at or datetime.now(UTC)
        result_list = payload.get("resultList")
        raw_results = result_list.get("result") if isinstance(result_list, dict) else None
        candidates = tuple(
            mapped
            for item in raw_results or []
            if isinstance(item, dict)
            if (mapped := candidate_from_europe_pmc_item(query, item, retrieved)) is not None
        )
        next_cursor = payload.get("nextCursorMark")
        return ScholarlyDiscoveryResult(
            provider_name=self.provider_name,
            query=query,
            retrieved_at=retrieved,
            candidates=candidates,
            hit_count=int_or_none(payload.get("hitCount")),
            metadata={
                **(request_metadata or {}),
                "next_cursor": next_cursor,
                "nextCursorMark": next_cursor,
                "nextPageUrl": payload.get("nextPageUrl"),
                "provider_returned_count": len(raw_results or []),
            },
        )
