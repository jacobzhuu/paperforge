"""Crossref 检索适配器（迁移自 DeepSearch adapters.py Crossref 部分）。

改动：去掉 occurrence 落账；polite pool 的联系邮箱走 ProviderConfig 注入。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from scholar_gateway.models import ScholarlyDiscoveryQuery, ScholarlyDiscoveryResult
from scholar_gateway.providers.base import HttpScholarlyDiscoveryAdapter
from scholar_gateway.providers.mapping import (
    candidate_from_crossref_item,
    int_or_none,
)
from scholar_gateway.providers.query_syntax import crossref_date_filter


class CrossrefDiscoveryAdapter(HttpScholarlyDiscoveryAdapter):
    name = "crossref"
    provider_name = "crossref"
    base_url = "https://api.crossref.org"

    def _request(self, query: ScholarlyDiscoveryQuery) -> tuple[str, dict[str, Any]]:
        params: dict[str, Any] = {
            # bibliographic 是 Crossref 的学术记录检索面；通用 ``query`` 会同时
            # 搜噪声元数据字段，并把宽查询的命中数吹大。
            "query.bibliographic": query.query_text,
            "rows": max(0, query.limit),
            "offset": max(0, query.offset),
        }
        date_filter = crossref_date_filter(query.filters)
        if date_filter:
            params["filter"] = date_filter
        contact_email = (self._config.contact_email or "").strip()
        if contact_email:
            # polite pool：带 mailto 的请求走更宽松的配额池。
            params["mailto"] = contact_email
        return f"{self.base_url}/works", params

    def map_payload(
        self,
        *,
        query: ScholarlyDiscoveryQuery,
        payload: dict[str, Any],
        retrieved_at: datetime | None = None,
        request_metadata: dict[str, Any] | None = None,
    ) -> ScholarlyDiscoveryResult:
        retrieved = retrieved_at or datetime.now(UTC)
        message = payload.get("message")
        items = message.get("items") if isinstance(message, dict) else None
        candidates = tuple(
            mapped
            for item in items or []
            if isinstance(item, dict)
            if (mapped := candidate_from_crossref_item(query, item, retrieved)) is not None
        )
        hit_count = int_or_none(message.get("total-results")) if isinstance(message, dict) else None
        return ScholarlyDiscoveryResult(
            provider_name=self.provider_name,
            query=query,
            retrieved_at=retrieved,
            candidates=candidates,
            hit_count=hit_count,
            metadata={
                "provider_status": payload.get("status"),
                "provider_returned_count": len(items or []),
                **(request_metadata or {}),
            },
        )
