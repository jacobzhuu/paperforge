"""SEARCH 阶段：五源并行检索 → 规范化 → 去重 → 排序 → 入库候选。

设计 §4.4.1：`五源并行检索 → 规范化 → 去重 → 相关性排序（确定性分 + LLM top-N 重排）`。
每个 provider 的结果都记 `search_run`（轻量复现留痕，非守恒账本，§3.3）。

Draft-first：任一 provider 失败只记 search_run(status=failed) + 降级标记，
其余源的结果照常入库；全部失败也返回结构化结果而不抛出。
R1：入库候选只来自 provider 真实响应，候选携带 provider_record_id。
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any

from db import record_search_run, upsert_entry, upsert_work
from observability import get_logger, record_scholar_provider
from scholar_gateway import (
    DEFAULT_PROVIDER_ORDER,
    ScholarlyDiscoveryQuery,
    ScholarlyDiscoveryResult,
    ScholarlyWorkCandidate,
    build_adapter,
    dedupe_scholarly_candidates,
)

from paperforge_worker.context import JobContext
from paperforge_worker.pipelines.ranking import (
    RankedCandidate,
    rank_candidates,
    rerank_with_llm,
)
from paperforge_worker.pipelines.scope import scope_filters, search_queries

logger = get_logger(__name__)


@dataclass
class SearchOutcome:
    raw_candidate_count: int = 0
    canonical_candidate_count: int = 0
    duplicates_merged: int = 0
    persisted_entry_count: int = 0
    selected_count: int = 0
    provider_stats: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        return {
            "raw_candidate_count": self.raw_candidate_count,
            "canonical_candidate_count": self.canonical_candidate_count,
            "duplicates_merged": self.duplicates_merged,
            "persisted_entry_count": self.persisted_entry_count,
            "selected_count": self.selected_count,
            "provider_stats": self.provider_stats,
            "warnings": self.warnings,
        }


async def run_search(
    context: JobContext,
    *,
    scope: dict[str, Any],
    providers: list[str] | None = None,
    limit_per_provider: int | None = None,
    auto_select_top_k: int | None = None,
) -> SearchOutcome:
    """执行一轮检索并把候选写入项目文献库。"""
    settings = context.settings
    provider_names = [p for p in (providers or list(DEFAULT_PROVIDER_ORDER)) if p]
    limit = limit_per_provider or settings.search_limit_per_provider
    queries = search_queries(scope)
    filters = scope_filters(scope)
    outcome = SearchOutcome()

    if not queries:
        outcome.warnings.append({"stage": "search", "reason": "empty_scope_query"})
        return outcome

    results = await _discover_all(
        context,
        provider_names=provider_names,
        queries=queries,
        filters=filters,
        limit=limit,
    )

    all_candidates: list[ScholarlyWorkCandidate] = []
    async with context.session() as session:
        for provider_name, query_text, result in results:
            failed = result.status == "failed"
            error_code = result.errors[0].error_code if result.errors else None
            # 前端只认 succeeded/partial/failed 三态。
            run_status = "failed" if failed else (
                "partial" if result.status == "partial" else "succeeded"
            )
            await record_search_run(
                session,
                project_id=context.project_id,
                provider=provider_name,
                query_text=query_text,
                filters=filters or None,
                hit_count=result.hit_count,
                retrieved_count=len(result.candidates),
                status=run_status,
                error=error_code,
            )
            record_scholar_provider(provider_name, result.status)
            outcome.provider_stats.append(
                {
                    "provider": provider_name,
                    "query": query_text,
                    "status": result.status,
                    "hit_count": result.hit_count,
                    "retrieved_count": len(result.candidates),
                    "error": error_code,
                }
            )
            if failed:
                # TODO(draft-first)：provider 限流/失败已留痕，后续可由用户重跑该源。
                outcome.warnings.append(
                    {
                        "stage": "search",
                        "provider": provider_name,
                        "reason": error_code or "provider_failed",
                    }
                )
            all_candidates.extend(result.candidates)

    outcome.raw_candidate_count = len(all_candidates)
    if not all_candidates:
        return outcome

    deduped = dedupe_scholarly_candidates(all_candidates)
    outcome.canonical_candidate_count = len(deduped.canonical_candidates)
    outcome.duplicates_merged = deduped.duplicate_count
    await context.emit(
        "search.deduped",
        {
            "raw": outcome.raw_candidate_count,
            "canonical": outcome.canonical_candidate_count,
            "duplicates_merged": outcome.duplicates_merged,
        },
        stage="search",
    )

    ranked = rank_candidates(list(deduped.canonical_candidates), scope=scope)
    ranked = await rerank_with_llm(
        ranked,
        scope=scope,
        runner=context.llm_runner(),
        top_n=settings.rerank_top_n,
    )

    top_k = settings.search_auto_select_top_k if auto_select_top_k is None else auto_select_top_k
    outcome.persisted_entry_count, outcome.selected_count = await _persist(
        context,
        ranked=ranked,
        auto_select_top_k=top_k,
    )
    return outcome


async def _discover_all(
    context: JobContext,
    *,
    provider_names: list[str],
    queries: list[str],
    filters: dict[str, Any],
    limit: int,
) -> list[tuple[str, str, ScholarlyDiscoveryResult]]:
    """各 provider 并行、同 provider 内串行（尊重共享配额与单飞语义）。"""

    async def _run_provider(provider_name: str) -> list[tuple[str, str, ScholarlyDiscoveryResult]]:
        try:
            adapter = build_adapter(
                provider_name,
                client=context.http_client,
                config=context.settings.provider_config(provider_name),
                cache=context.scholar_cache,
            )
        except ValueError:
            logger.warning("unknown provider skipped", extra={"provider": provider_name})
            return []
        collected: list[tuple[str, str, ScholarlyDiscoveryResult]] = []
        for query_text in queries:
            query = ScholarlyDiscoveryQuery(
                query_text=query_text,
                limit=limit,
                filters=filters,
            )
            # 适配器是同步 httpx 实现：放线程池执行，避免阻塞事件循环。
            result = await asyncio.to_thread(adapter.discover, query)
            collected.append((provider_name, query_text, result))
        return collected

    gathered = await asyncio.gather(
        *(_run_provider(name) for name in provider_names),
        return_exceptions=True,
    )
    results: list[tuple[str, str, ScholarlyDiscoveryResult]] = []
    for provider_name, item in zip(provider_names, gathered, strict=True):
        if isinstance(item, BaseException):
            logger.warning(
                "provider task crashed",
                extra={"provider": provider_name, "error": type(item).__name__},
            )
            continue
        results.extend(item)
    return results


async def _persist(
    context: JobContext,
    *,
    ranked: list[RankedCandidate],
    auto_select_top_k: int,
) -> tuple[int, int]:
    """写 scholarly_work + library_entry。

    R1：这些候选来自 provider 真实响应，因此以 ``verified=True`` 入库；
    是否进入写作白名单还要看 status='selected' 且已分配 bibtex_key。
    全自动模式取 top-K 直接置为 selected（设计 §3.4）。
    """
    persisted = 0
    selected = 0
    async with context.session() as session:
        for index, item in enumerate(ranked):
            work, _created = await upsert_work(session, item.candidate)
            status = "selected" if index < auto_select_top_k else "candidate"
            entry, _entry_created = await upsert_entry(
                session,
                project_id=context.project_id,
                work_id=work.id,
                added_via="search",
                status=status,
                relevance_score=item.score,
                rank_reason={
                    **item.reason,
                    "provider": item.candidate.provider_name,
                    "provider_record_id": item.candidate.provider_record_id,
                },
                verified=True,
            )
            persisted += 1
            if entry.status == "selected":
                selected += 1
    return persisted, selected


async def ensure_bibtex_keys(context: JobContext, project_id: uuid.UUID | None = None) -> int:
    """R3：为已 selected 且已核验的条目一次性生成并持久化 bibtex_key。

    渲染期只消费持久化 key，不再重算（设计 §4.4.3 R3）。
    """
    from db import assign_bibtex_key, list_entries, reference_metadata_payload
    from paper_ir import ReferenceMetadata, make_bibtex_key

    assigned = 0
    async with context.session() as session:
        entries = await list_entries(
            session,
            project_id or context.project_id,
            status="selected",
        )
        for entry, work in entries:
            if entry.bibtex_key or entry.verified_at is None or work.is_retracted:
                continue
            payload = await reference_metadata_payload(session, work)
            await assign_bibtex_key(
                session,
                entry,
                make_bibtex_key,
                reference=ReferenceMetadata(**payload),
            )
            assigned += 1
    return assigned
