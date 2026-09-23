"""SEARCH 阶段：多源并行检索 → 规范化 → 去重 → 排序 → 入库候选。

设计 §4.4.1：`多源并行检索 → 规范化 → 去重 → 相关性排序（确定性分 + LLM top-N 重排）`。
每个 provider 的结果都记 `search_run`（轻量复现留痕，非守恒账本，§3.3）。

Draft-first：任一 provider 失败只记 search_run(status=failed) + 降级标记，
其余源的结果照常入库；全部失败也返回结构化结果而不抛出。
R1：入库候选只来自 provider 真实响应，候选携带 provider_record_id。
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from db import list_research_questions, record_search_run, upsert_entry, upsert_work
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
    AUTO_SELECT_MIN_TOPIC_EVIDENCE,
    RankedCandidate,
    balanced_selection_indices,
    rank_candidates,
    rerank_with_llm,
    topic_evidence,
)
from paperforge_worker.pipelines.scope import scope_filters, search_queries

logger = get_logger(__name__)


@dataclass
class SearchOutcome:
    raw_candidate_count: int = 0
    canonical_candidate_count: int = 0
    duplicates_merged: int = 0
    persisted_entry_count: int = 0
    new_work_count: int = 0
    new_entry_count: int = 0
    selected_count: int = 0
    provider_stats: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        return {
            "raw_candidate_count": self.raw_candidate_count,
            "canonical_candidate_count": self.canonical_candidate_count,
            "duplicates_merged": self.duplicates_merged,
            "persisted_entry_count": self.persisted_entry_count,
            "new_work_count": self.new_work_count,
            "new_entry_count": self.new_entry_count,
            "selected_count": self.selected_count,
            "provider_stats": self.provider_stats,
            "warnings": self.warnings,
        }


async def _sub_question_queries(context: JobContext) -> list[str] | None:
    """子问题检索面的单一真源是 ``research_question`` 表，不是 ``scope_json``。

    返回 ``None`` 表示这个项目根本没有问题树（original 论文不跑 QDECOMP），
    此时由 :func:`search_queries` 回落到 scope 里的副本。
    """
    async with context.session() as session:
        questions = await list_research_questions(session, context.project_id, kind="sub")
    if not questions:
        return None
    return [question.search_query or "" for question in questions]


async def run_search(
    context: JobContext,
    *,
    scope: dict[str, Any],
    providers: list[str] | None = None,
    limit_per_provider: int | None = None,
    auto_select_top_k: int | None = None,
    query_texts: list[str] | None = None,
) -> SearchOutcome:
    """执行一轮检索并把候选写入项目文献库。"""
    settings = context.settings
    provider_names = [p for p in (providers or list(DEFAULT_PROVIDER_ORDER)) if p]
    limit = limit_per_provider or settings.search_limit_per_provider
    queries = list(dict.fromkeys(query.strip() for query in (query_texts or []) if query.strip()))
    if query_texts is None:
        queries = search_queries(
            scope,
            sub_question_queries=await _sub_question_queries(context),
        )
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
            run_status = (
                "failed" if failed else ("partial" if result.status == "partial" else "succeeded")
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
        concurrency=settings.rerank_concurrency,
    )

    top_k = settings.search_auto_select_top_k if auto_select_top_k is None else auto_select_top_k
    (
        outcome.persisted_entry_count,
        outcome.selected_count,
        outcome.new_work_count,
        outcome.new_entry_count,
    ) = await _persist(
        context,
        ranked=ranked,
        auto_select_top_k=top_k,
    )
    if ranked and not outcome.selected_count:
        # 有候选却一篇都不够格自动入库：检索式跟主题对不上（中文主题 + 英文检索源
        # 是最常见的成因）。这必须让用户看见，否则前端只会显示一个空文献库。
        outcome.warnings.append(
            {
                "stage": "search",
                "reason": "no_topically_relevant_results",
                "queries": queries,
                "candidates_reviewed": len(ranked),
            }
        )
    elif ranked and outcome.selected_count < _corpus_floor(context):
        # 「零篇」不是唯一的失败模式。一次真实运行 764 篇候选只选中 9 篇，前端
        # 不报任何异常，而写作阶段把这个由筛选造成的空档当成了领域的真实空白，
        # 花了近四成篇幅去描述它。库容偏小必须和库容为零一样显式。
        outcome.warnings.append(
            {
                "stage": "search",
                "reason": "library_undersized",
                "selected": outcome.selected_count,
                "floor": _corpus_floor(context),
                "candidates_reviewed": len(ranked),
            }
        )
    return outcome


def _corpus_floor(context: JobContext) -> int:
    return max(
        0,
        int(getattr(getattr(context, "settings", None), "library_backfill_floor", 36) or 0),
    )


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
) -> tuple[int, int, int, int]:
    """写 scholarly_work + library_entry。

    R1：这些候选来自 provider 真实响应，因此以 ``verified=True`` 入库；
    是否进入写作白名单还要看 status='selected' 且已分配 bibtex_key。
    全自动模式取 top-K 直接置为 selected（设计 §3.4），但 top-K 只是**名额上限**：
    还要过主题证据下限才真的入库。检索源返回的东西未必和主题有关（中文主题打到
    只索引英文的检索源时尤其如此），而 selected 会直接进入引用白名单——名次靠前
    不等于切题，这里必须再拦一道，否则无关文献会被当成可引用的文献基础。
    低于门槛的候选仍以 candidate 留档，用户可以自行圈选，不丢数据。
    """
    persisted = 0
    selected = 0
    new_works = 0
    new_entries = 0
    selected_indices = balanced_selection_indices(ranked, limit=auto_select_top_k)
    async with context.session() as session:
        for index, item in enumerate(ranked):
            work, work_created = await upsert_work(session, item.candidate)
            relevant = topic_evidence(item) >= AUTO_SELECT_MIN_TOPIC_EVIDENCE
            status = "selected" if index in selected_indices and relevant else "candidate"
            entry, entry_created = await upsert_entry(
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
                    "selection_stratum": (
                        "foundational"
                        if (item.candidate.publication_year or 9999) <= datetime.now(UTC).year - 6
                        else "recent"
                    ),
                    "fulltext_available": bool(
                        item.candidate.pmcid
                        or item.candidate.arxiv_id
                        or item.candidate.oa_status not in {None, "closed"}
                        or any(link.is_oa for link in item.candidate.links)
                    ),
                },
                verified=True,
            )
            persisted += 1
            new_works += int(work_created)
            new_entries += int(entry_created)
            if entry.status == "selected":
                selected += 1
    return persisted, selected, new_works, new_entries


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
