"""学术检索适配器（设计 §3.1 / §9）。

DeepSearch 的 `literature_review/adapters.py`（1961 行，五源合一）在此按 provider 拆分迁移：
- `crossref.py` / `openalex.py` / `semantic_scholar.py` / `arxiv.py` / `europepmc.py`
- `base.py`：共享 GET（缓存优先 → 限速 → 重试/退避 → 配额诊断）
- `mapping.py`：provider 响应 → 候选实体的确定性映射
- `query_syntax.py`：各源查询净化与时间窗过滤

限速/熔断在 `../runtime.py`，HTTP 缓存在 `../cache.py`，雪球在 `../snowball.py`，
OA 全文在 `../fulltext.py`，合规抓取客户端在 `../http.py`。
"""

from __future__ import annotations

import httpx

from scholar_gateway.cache import HttpCacheBackend
from scholar_gateway.providers.arxiv import ArxivDiscoveryAdapter
from scholar_gateway.providers.base import (
    FetchedHttpResponse,
    HttpScholarlyDiscoveryAdapter,
    ProviderConfig,
)
from scholar_gateway.providers.crossref import CrossrefDiscoveryAdapter
from scholar_gateway.providers.europepmc import EuropePmcDiscoveryAdapter
from scholar_gateway.providers.openalex import OpenAlexDiscoveryAdapter
from scholar_gateway.providers.semantic_scholar import SemanticScholarDiscoveryAdapter

ADAPTER_REGISTRY: dict[str, type[HttpScholarlyDiscoveryAdapter]] = {
    "openalex": OpenAlexDiscoveryAdapter,
    "crossref": CrossrefDiscoveryAdapter,
    "arxiv": ArxivDiscoveryAdapter,
    "europe_pmc": EuropePmcDiscoveryAdapter,
}

# 默认检索顺序：配额宽松者优先（设计 §7 provider 限流对策）。
DEFAULT_PROVIDER_ORDER: tuple[str, ...] = (
    "openalex",
    "europe_pmc",
    "crossref",
    "arxiv",
)

ALL_PROVIDERS: tuple[str, ...] = tuple(ADAPTER_REGISTRY)


def build_adapter(
    provider_name: str,
    *,
    client: httpx.Client | None = None,
    config: ProviderConfig | None = None,
    cache: HttpCacheBackend | None = None,
) -> HttpScholarlyDiscoveryAdapter:
    """按名字构造适配器。未知 provider 抛 ValueError（调用方按 draft-first 记 TODO 跳过）。"""
    key = provider_name.strip().lower()
    adapter_cls = ADAPTER_REGISTRY.get(key)
    if adapter_cls is None:
        raise ValueError(f"unknown scholarly provider: {provider_name}")
    return adapter_cls(client=client, config=config, cache=cache)


__all__ = [
    "ADAPTER_REGISTRY",
    "ALL_PROVIDERS",
    "DEFAULT_PROVIDER_ORDER",
    "ArxivDiscoveryAdapter",
    "CrossrefDiscoveryAdapter",
    "EuropePmcDiscoveryAdapter",
    "FetchedHttpResponse",
    "HttpScholarlyDiscoveryAdapter",
    "OpenAlexDiscoveryAdapter",
    "ProviderConfig",
    "SemanticScholarDiscoveryAdapter",
    "build_adapter",
]
