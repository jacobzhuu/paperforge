"""一跳引文雪球扩展（前向 cited-by + 后向 references）。

迁移自 DeepSearch literature_review/snowball.py（674 行，设计 §3.1）。
改动：
- 种子从 ORM ``ScholarlyWork`` 解耦为轻量 ``SnowballSeed``（雪球不依赖 db 包）；
- 缓存句柄换为注入的 ``HttpCacheBackend``；
- 去掉 screening 语义：结果只是普通候选，回到 dedupe/排序即可。

Related Work 发现的核心武器：综述管线 CURATE 阶段对种子做一轮扩展。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

import httpx

from scholar_gateway.cache import HttpCacheBackend, NullHttpCache
from scholar_gateway.models import ScholarlyDiscoveryQuery, ScholarlyWorkCandidate
from scholar_gateway.normalize import normalize_openalex_id
from scholar_gateway.providers.base import ProviderConfig
from scholar_gateway.providers.mapping import candidate_from_semantic_scholar_item
from scholar_gateway.providers.openalex import OpenAlexDiscoveryAdapter
from scholar_gateway.runtime import (
    SEMANTIC_SCHOLAR_CIRCUIT_BREAKER_COOLDOWN_SECONDS,
    SEMANTIC_SCHOLAR_CIRCUIT_BREAKER_THRESHOLD,
    provider_circuit_breaker,
    provider_pacer,
    semantic_scholar_min_request_interval,
)

SnowballDirection = Literal["both", "forward", "backward"]
_OPENALEX_ID_RE = re.compile(r"(W\d+)", re.IGNORECASE)


class CitationRankable(Protocol):
    """引文影响力排序只需要这几个属性（ORM 行与 dataclass 都满足）。"""

    citation_count: int | None
    influential_citation_count: int | None
    publication_year: int | None


@dataclass(frozen=True)
class SnowballSeed:
    """雪球种子：只带扩展所需标识符，避免 scholar_gateway 依赖 db 包。"""

    work_id: str
    openalex_id: str | None = None
    semantic_scholar_id: str | None = None
    citation_count: int | None = None
    influential_citation_count: int | None = None
    publication_year: int | None = None


@dataclass(frozen=True)
class SnowballExpansionResult:
    seed_work_ids: tuple[str, ...]
    discovered_candidates: tuple[ScholarlyWorkCandidate, ...]
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def to_event_payload(self) -> dict[str, Any]:
        return {
            "seed_work_count": len(self.seed_work_ids),
            "discovered_candidate_count": len(self.discovered_candidates),
            "diagnostics": self.diagnostics,
        }


def expand_citation_snowball(
    *,
    seeds: list[SnowballSeed],
    client: httpx.Client | None,
    user_agent: str = "PaperForge/0.1",
    timeout_seconds: float = 10.0,
    max_seeds: int = 8,
    max_neighbors_per_seed: int = 20,
    direction: SnowballDirection = "both",
    cache: HttpCacheBackend | None = None,
    cache_ttl_hours: float = 72.0,
    enable_semantic_scholar: bool = False,
    semantic_scholar_api_key: str | None = None,
    openalex_api_key: str | None = None,
    semantic_scholar_min_interval_seconds: float | None = None,
) -> SnowballExpansionResult:
    """对有界种子集做一跳 references / cited-by 扩展。

    ``direction="both"`` 时每个方向取 ``max_neighbors_per_seed`` 的一半，
    使邻居总量与仅前向时相当。任何 provider 失败都只记诊断，不抛出（draft-first）。
    Semantic Scholar 仅保留兼容实现，默认关闭；生产流程只使用 OpenAlex 扩展。
    """
    if direction not in {"both", "forward", "backward"}:
        raise ValueError(f"unsupported snowball direction: {direction}")

    cache = cache or NullHttpCache()
    selected = seeds[: max(0, max_seeds)]
    per_direction_limit = max(1, int(max_neighbors_per_seed))
    if direction == "both":
        per_direction_limit = max(1, int(max_neighbors_per_seed) // 2)

    run_forward = direction in {"both", "forward"}
    run_backward = direction in {"both", "backward"}
    diagnostics: dict[str, Any] = {
        "status": "skipped" if client is None or not selected else "attempted",
        "seed_count": len(selected),
        "direction": direction,
        "per_direction_limit": per_direction_limit,
        "errors": [],
        "provider_calls": [],
        "forward_candidate_count": 0,
        "backward_candidate_count": 0,
    }
    if client is None or not selected:
        return SnowballExpansionResult((), (), diagnostics)

    discovered: list[ScholarlyWorkCandidate] = []
    seen_keys: set[str] = set()
    headers = {"User-Agent": user_agent} if user_agent else {}
    s2_headers = dict(headers)
    if semantic_scholar_api_key:
        s2_headers["x-api-key"] = semantic_scholar_api_key
    # 显式 0 用于关闭 pacing（测试/自建代理）；None 表示按凭据档位取默认。
    s2_min_interval = (
        max(0.0, semantic_scholar_min_interval_seconds)
        if semantic_scholar_min_interval_seconds is not None
        else semantic_scholar_min_request_interval(api_key=semantic_scholar_api_key)
    )
    forward_count = 0
    backward_count = 0

    for seed in selected:
        openalex_id = (seed.openalex_id or "").strip()
        s2_id = (seed.semantic_scholar_id or "").strip() if enable_semantic_scholar else ""

        if run_forward and openalex_id:
            neighbors, call_diag = _openalex_neighbors(
                client=client,
                openalex_id=openalex_id,
                headers=headers,
                timeout_seconds=timeout_seconds,
                limit=per_direction_limit,
                cache=cache,
                cache_ttl_hours=cache_ttl_hours,
                api_key=openalex_api_key,
            )
            diagnostics["provider_calls"].append(call_diag)
            forward_count += _append_unique_candidates(
                discovered, seen_keys, neighbors, prefer="openalex"
            )

        if run_backward and openalex_id:
            neighbors, call_diag = _openalex_references(
                client=client,
                openalex_id=openalex_id,
                headers=headers,
                timeout_seconds=timeout_seconds,
                limit=per_direction_limit,
                cache=cache,
                cache_ttl_hours=cache_ttl_hours,
                api_key=openalex_api_key,
            )
            diagnostics["provider_calls"].append(call_diag)
            backward_count += _append_unique_candidates(
                discovered, seen_keys, neighbors, prefer="openalex"
            )

        if run_forward and s2_id:
            neighbors, call_diag = _semantic_scholar_edge_neighbors(
                client=client,
                paper_id=s2_id,
                headers=s2_headers,
                timeout_seconds=timeout_seconds,
                limit=per_direction_limit,
                edge="citations",
                paper_field="citingPaper",
                direction="forward",
                cache=cache,
                cache_ttl_hours=cache_ttl_hours,
                min_request_interval_seconds=s2_min_interval,
            )
            diagnostics["provider_calls"].append(call_diag)
            forward_count += _append_unique_candidates(
                discovered, seen_keys, neighbors, prefer="semantic_scholar"
            )

        if run_backward and s2_id:
            neighbors, call_diag = _semantic_scholar_edge_neighbors(
                client=client,
                paper_id=s2_id,
                headers=s2_headers,
                timeout_seconds=timeout_seconds,
                limit=per_direction_limit,
                edge="references",
                paper_field="citedPaper",
                direction="backward",
                cache=cache,
                cache_ttl_hours=cache_ttl_hours,
                min_request_interval_seconds=s2_min_interval,
            )
            diagnostics["provider_calls"].append(call_diag)
            backward_count += _append_unique_candidates(
                discovered, seen_keys, neighbors, prefer="semantic_scholar"
            )

    diagnostics["forward_candidate_count"] = forward_count
    diagnostics["backward_candidate_count"] = backward_count
    diagnostics["discovered_candidate_count"] = len(discovered)
    diagnostics["errors"] = [call for call in diagnostics["provider_calls"] if call.get("error")]
    return SnowballExpansionResult(
        seed_work_ids=tuple(seed.work_id for seed in selected),
        discovered_candidates=tuple(discovered),
        diagnostics=diagnostics,
    )


def _append_unique_candidates(
    discovered: list[ScholarlyWorkCandidate],
    seen_keys: set[str],
    neighbors: list[ScholarlyWorkCandidate],
    *,
    prefer: Literal["openalex", "semantic_scholar"],
) -> int:
    added = 0
    for candidate in neighbors:
        if prefer == "openalex":
            key = candidate.doi or candidate.openalex_id or candidate.normalized_title_hash
        else:
            key = candidate.doi or candidate.semantic_scholar_id or candidate.normalized_title_hash
        if key and key not in seen_keys:
            seen_keys.add(key)
            discovered.append(candidate)
            added += 1
    return added


def _openalex_adapter(
    *,
    client: httpx.Client,
    timeout_seconds: float,
    api_key: str | None,
    user_agent: str | None,
    cache: HttpCacheBackend,
    cache_ttl_hours: float,
) -> OpenAlexDiscoveryAdapter:
    return OpenAlexDiscoveryAdapter(
        client=client,
        config=ProviderConfig(
            api_key=api_key,
            user_agent=user_agent,
            timeout_seconds=timeout_seconds,
            rate_limit_delay=0.2,
            max_retries=1,
            cache_ttl_hours=cache_ttl_hours,
        ),
        cache=cache,
    )


def _openalex_neighbors(
    *,
    client: httpx.Client,
    openalex_id: str,
    headers: dict[str, str],
    timeout_seconds: float,
    limit: int,
    cache: HttpCacheBackend,
    cache_ttl_hours: float,
    api_key: str | None = None,
) -> tuple[list[ScholarlyWorkCandidate], dict[str, Any]]:
    """前向：谁引用了种子（``filter=cites:``）。"""
    adapter = _openalex_adapter(
        client=client,
        timeout_seconds=timeout_seconds,
        api_key=api_key,
        user_agent=headers.get("User-Agent"),
        cache=cache,
        cache_ttl_hours=cache_ttl_hours,
    )
    query = ScholarlyDiscoveryQuery(
        query_text="*",
        limit=limit,
        filters={"cites": openalex_id},
    )
    url = f"{adapter.base_url}/works"
    params = {"filter": f"cites:{openalex_id}", "per-page": limit}
    try:
        payload, cache_hit = _cached_json_get(
            client=client,
            url=url,
            params=params,
            headers=headers,
            timeout_seconds=timeout_seconds,
            cache=cache,
            cache_ttl_hours=cache_ttl_hours,
            provider_name="openalex",
            auth_params={"api_key": api_key} if api_key else None,
        )
        result = adapter.map_payload(
            query=query,
            payload=payload,
            request_metadata={"cache_hit": cache_hit},
        )
        return list(result.candidates), {
            "provider": "openalex",
            "mode": "cites",
            "direction": "forward",
            "seed": openalex_id,
            "count": len(result.candidates),
            "cache_hit": cache_hit,
        }
    except Exception as error:  # noqa: BLE001 - 雪球是尽力而为，失败只记诊断
        return [], {
            "provider": "openalex",
            "mode": "cites",
            "direction": "forward",
            "seed": openalex_id,
            "error": type(error).__name__,
            "cache_hit": False,
        }


def _openalex_references(
    *,
    client: httpx.Client,
    openalex_id: str,
    headers: dict[str, str],
    timeout_seconds: float,
    limit: int,
    cache: HttpCacheBackend,
    cache_ttl_hours: float,
    api_key: str | None = None,
) -> tuple[list[ScholarlyWorkCandidate], dict[str, Any]]:
    """后向：种子引用了谁（``referenced_works`` → 批量取详情）。"""
    adapter = _openalex_adapter(
        client=client,
        timeout_seconds=timeout_seconds,
        api_key=api_key,
        user_agent=headers.get("User-Agent"),
        cache=cache,
        cache_ttl_hours=cache_ttl_hours,
    )
    seed_key = normalize_openalex_id(openalex_id) or openalex_id.strip()
    detail_url = f"{adapter.base_url}/works/{seed_key}"
    auth_params = {"api_key": api_key} if api_key else None
    try:
        detail_payload, detail_cache_hit = _cached_json_get(
            client=client,
            url=detail_url,
            params=None,
            headers=headers,
            timeout_seconds=timeout_seconds,
            cache=cache,
            cache_ttl_hours=cache_ttl_hours,
            provider_name="openalex",
            auth_params=auth_params,
        )
        referenced_ids = _openalex_referenced_work_ids(detail_payload)[: max(0, limit)]
        if not referenced_ids:
            return [], {
                "provider": "openalex",
                "mode": "references",
                "direction": "backward",
                "seed": seed_key,
                "count": 0,
                "referenced_id_count": 0,
                "cache_hit": detail_cache_hit,
            }

        works_params = {
            "filter": "openalex_id:" + "|".join(referenced_ids),
            "per-page": min(limit, len(referenced_ids)),
        }
        works_payload, works_cache_hit = _cached_json_get(
            client=client,
            url=f"{adapter.base_url}/works",
            params=works_params,
            headers=headers,
            timeout_seconds=timeout_seconds,
            cache=cache,
            cache_ttl_hours=cache_ttl_hours,
            provider_name="openalex",
            auth_params=auth_params,
        )
        query = ScholarlyDiscoveryQuery(
            query_text="*",
            limit=limit,
            filters={"openalex_id": "|".join(referenced_ids)},
        )
        result = adapter.map_payload(
            query=query,
            payload=works_payload,
            request_metadata={"cache_hit": works_cache_hit},
        )
        return list(result.candidates)[:limit], {
            "provider": "openalex",
            "mode": "references",
            "direction": "backward",
            "seed": seed_key,
            "count": min(len(result.candidates), limit),
            "referenced_id_count": len(referenced_ids),
            "cache_hit": detail_cache_hit or works_cache_hit,
        }
    except Exception as error:  # noqa: BLE001 - 雪球是尽力而为
        return [], {
            "provider": "openalex",
            "mode": "references",
            "direction": "backward",
            "seed": seed_key,
            "error": type(error).__name__,
            "cache_hit": False,
        }


def _semantic_scholar_edge_neighbors(
    *,
    client: httpx.Client,
    paper_id: str,
    headers: dict[str, str],
    timeout_seconds: float,
    limit: int,
    edge: Literal["citations", "references"],
    paper_field: Literal["citingPaper", "citedPaper"],
    direction: Literal["forward", "backward"],
    cache: HttpCacheBackend,
    cache_ttl_hours: float,
    min_request_interval_seconds: float = 0.0,
) -> tuple[list[ScholarlyWorkCandidate], dict[str, Any]]:
    candidates: list[ScholarlyWorkCandidate] = []
    url = f"https://api.semanticscholar.org/graph/v1/paper/{paper_id}/{edge}"
    params = {
        "limit": limit,
        "fields": (
            f"{paper_field}.paperId,{paper_field}.title,{paper_field}.abstract,"
            f"{paper_field}.year,{paper_field}.externalIds,{paper_field}.url,"
            f"{paper_field}.influentialCitationCount"
        ),
    }
    try:
        payload, cache_hit = _cached_json_get(
            client=client,
            url=url,
            params=params,
            headers=headers,
            timeout_seconds=timeout_seconds,
            cache=cache,
            cache_ttl_hours=cache_ttl_hours,
            provider_name="semantic_scholar",
            min_request_interval_seconds=min_request_interval_seconds,
            circuit_breaker_threshold=SEMANTIC_SCHOLAR_CIRCUIT_BREAKER_THRESHOLD,
            circuit_breaker_cooldown_seconds=SEMANTIC_SCHOLAR_CIRCUIT_BREAKER_COOLDOWN_SECONDS,
        )
        for item in payload.get("data") or []:
            if not isinstance(item, dict):
                continue
            paper = item.get(paper_field)
            if not isinstance(paper, dict):
                continue
            mapped = candidate_from_semantic_scholar_item(
                ScholarlyDiscoveryQuery(query_text="snowball", limit=limit),
                paper,
                datetime.now(UTC),
            )
            if mapped is not None:
                candidates.append(mapped)
        return candidates, {
            "provider": "semantic_scholar",
            "mode": edge,
            "direction": direction,
            "seed": paper_id,
            "count": len(candidates),
            "cache_hit": cache_hit,
        }
    except Exception as error:  # noqa: BLE001 - 雪球是尽力而为
        return [], {
            "provider": "semantic_scholar",
            "mode": edge,
            "direction": direction,
            "seed": paper_id,
            "error": type(error).__name__,
            "cache_hit": False,
        }


def _cached_json_get(
    *,
    client: httpx.Client,
    url: str,
    params: dict[str, Any] | None,
    headers: dict[str, str],
    timeout_seconds: float,
    cache: HttpCacheBackend,
    cache_ttl_hours: float,
    provider_name: str,
    min_request_interval_seconds: float = 0.0,
    circuit_breaker_threshold: int = 0,
    circuit_breaker_cooldown_seconds: float = 0.0,
    auth_params: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], bool]:
    cached = cache.get(url=url, params=params, ttl_hours=cache_ttl_hours)
    if cached is not None:
        payload = json.loads(cached.text)
        if not isinstance(payload, dict):
            raise ValueError(f"{provider_name} cached payload was not an object")
        return payload, True

    breaker = provider_circuit_breaker(provider_name) if circuit_breaker_threshold > 0 else None
    if breaker is not None:
        # 抛 ProviderCircuitOpenError；雪球调用方按 call 记录诊断。
        breaker.allow(provider_name)
    if min_request_interval_seconds > 0:
        provider_pacer(provider_name).wait(min_request_interval_seconds, sleep_fn=time.sleep)
    # 凭据只在发请求时合并，绝不进缓存键与任何持久化诊断。
    request_params = {**(params or {}), **auth_params} if auth_params else params
    response = client.get(url, params=request_params, headers=headers, timeout=timeout_seconds)
    if breaker is not None and response.status_code == 429:
        breaker.record_429(
            threshold=circuit_breaker_threshold,
            cooldown_seconds=circuit_breaker_cooldown_seconds,
        )
    response.raise_for_status()
    if breaker is not None:
        breaker.record_success()
    if response.status_code == 200:
        cache.put(
            url=url,
            params=params,
            status_code=response.status_code,
            content_type=response.headers.get("content-type"),
            body=response.content,
            provider_name=provider_name,
        )
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError(f"{provider_name} response JSON was not an object")
    return payload, False


def _openalex_referenced_work_ids(payload: dict[str, Any]) -> list[str]:
    raw = payload.get("referenced_works")
    if not isinstance(raw, list):
        return []
    ids: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            continue
        normalized = normalize_openalex_id(item.strip())
        if normalized is None:
            match = _OPENALEX_ID_RE.search(item)
            normalized = match.group(1).upper() if match else None
        if normalized and normalized not in seen:
            seen.add(normalized)
            ids.append(normalized)
    return ids


def rank_by_citation_influence(works: list[Any]) -> list[Any]:
    """按引文影响力排序，供雪球种子与 OA 全文预算使用。

    主键为 influential → total 引用数（降序），同分按发表年降序、再按 work id 稳定。
    当所有条目都没有引用信号时保持输入顺序（不引入随机性）。
    """
    if not any(
        getattr(work, "influential_citation_count", None) is not None
        or getattr(work, "citation_count", None) is not None
        for work in works
    ):
        return list(works)
    return sorted(
        works,
        key=lambda work: (
            -(getattr(work, "influential_citation_count", None) or 0),
            -(getattr(work, "citation_count", None) or 0),
            -(getattr(work, "publication_year", None) or 0),
            str(getattr(work, "work_id", None) or getattr(work, "id", "")),
        ),
    )


def select_core_works_by_influence(works: list[Any], *, top_k: int = 5) -> list[Any]:
    """返回引文影响力最高的 top_k 条，保持排名顺序。"""
    return rank_by_citation_influence(works)[: max(0, top_k)]
