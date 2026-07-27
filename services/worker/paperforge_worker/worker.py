"""ARQ worker 入口与任务编排（设计 §4.1 / §4.4）。

论文生成是分钟级长任务：异步 + 进度流 + 断点续跑。
阶段 checkpoint 写 `generation_job.checkpoint_json`，事件写 `job_event` 供 SSE。

Draft-first：每个阶段都包在 `_run_stage` 里——阶段失败写降级标记并继续，
只有「一件可交付产物都没有」时才把任务标成 failed。
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from arq import func
from arq.connections import RedisSettings
from db import get_project, update_job, update_project_scope
from db.session import make_engine, make_session_factory
from observability import configure_logging, get_logger, record_job_stage

from paperforge_worker.config import WorkerSettings as Settings
from paperforge_worker.config import get_settings
from paperforge_worker.context import JobContext, build_scholar_cache, job_context
from paperforge_worker.pipelines.cards import generate_cards
from paperforge_worker.pipelines.document import write_document
from paperforge_worker.pipelines.export import export_document
from paperforge_worker.pipelines.fulltext import acquire_fulltexts
from paperforge_worker.pipelines.importing import (
    import_references,
    requests_from_bibtex,
    requests_from_dois,
)
from paperforge_worker.pipelines.outline import CardBrief, generate_outline
from paperforge_worker.pipelines.quality import (
    apply_readiness_gate,
    build_claim_evidence,
    build_quality_report,
    soft_check_citations,
)
from paperforge_worker.pipelines.scope import generate_scope
from paperforge_worker.pipelines.search import ensure_bibtex_keys, run_search
from paperforge_worker.pipelines.visuals import generate_visual, suggest_visuals

logger = get_logger(__name__)

# 一键生成的单任务超时（秒）。默认 job_timeout 管的是单阶段任务。
FULL_PIPELINE_TIMEOUT_SECONDS = 7200

# 综述管线阶段权重（用于进度条）。
#
# 这是**一键生成那条长管线**的绝对刻度，只对 run_full_pipeline / run_library_pipeline
# 有意义。单阶段任务（视觉建议、单张生图）不能复用它：`visual_generate` 此前在这里
# 挂着 0.75，而它根本不是全文管线的阶段——独立的生图任务因此长期停在 75%，
# 完成时才直接跳到 100%。单阶段任务改为由调用方显式传 `progress`。
_STAGE_PROGRESS = {
    "scope": 0.10,
    "search": 0.40,
    "curate": 0.45,
    "ingest": 0.50,
    "snowball": 0.52,
    "cards": 0.55,
    "quality": 0.96,
    "outline": 0.65,
    "write": 0.90,
    "citecheck": 0.93,
    "visual_plan": 0.95,
    "render": 0.98,
    "done": 1.0,
}


async def _run_stage(
    context: JobContext,
    stage: str,
    runner: Callable[[], Awaitable[Any]],
    *,
    kind: str = "library",
    progress: float | None = None,
) -> Any:
    """执行一个阶段：成功发事件，失败留降级标记并继续（gate-free）。"""
    await context.emit(f"{stage}.started", {}, stage=stage)
    try:
        result = await runner()
    except Exception as error:  # noqa: BLE001 - 单阶段失败不阻断整条管线
        logger.warning("stage failed", extra={"stage": stage, "error": type(error).__name__})
        context.warn(stage, type(error).__name__, {"message": str(error)[:300]})
        record_job_stage(kind, stage, "failed")
        await context.emit(
            f"{stage}.failed",
            {"error": type(error).__name__, "message": str(error)[:300]},
            stage=stage,
            checkpoint={f"{stage}_failed": True},
        )
        return None
    record_job_stage(kind, stage, "succeeded")
    # 阶段可以返回 (outcome, extra)：checkpoint 取第一个带 to_payload 的元素。
    reportable = result[0] if isinstance(result, tuple) and result else result
    if hasattr(reportable, "to_payload"):
        payload = reportable.to_payload()
    elif isinstance(reportable, dict):
        # 阶段直接返回 dict 时原样作为 checkpoint（例如雪球统计）。
        payload = reportable
    else:
        payload = {}
    await context.emit(
        f"{stage}.completed",
        payload,
        stage=stage,
        progress=progress if progress is not None else _STAGE_PROGRESS.get(stage),
        checkpoint={stage: payload or True},
    )
    return result


def scope_needs_regeneration(scope: dict[str, Any]) -> bool:
    """已存的 scope 是否需要在检索前重生成。

    确定性回退是降级品——LLM 当时不可用或输出不合法才会留下它。若不重生成，项目会被
    永久钉死在这份 scope 上：前端默认只在项目没有 topic 时才请求重生成，于是变成
    「关键词很差 → 检索结果跑题 → 再点一次还是同样的关键词」。中文主题尤其致命，
    回退切出来的中文关键词打不中任何只索引英文的检索源。
    用户手改过的 scope 带 generator='user'，永远豁免。
    """
    if not scope.get("keyword_groups"):
        return True
    return str(scope.get("generator") or "").startswith("deterministic")


async def run_library_pipeline(
    ctx: dict,
    project_id: str,
    job_id: str | None = None,
    *,
    providers: list[str] | None = None,
    regenerate_scope: bool = True,
    finalize: bool = True,
) -> dict[str, Any]:
    """M1 主管线：SCOPE → SEARCH → CURATE(bibtex keys) → CARDS。

    ``finalize=False`` 供 ``run_full_pipeline`` 复用：全管线还要继续跑
    outline/write/render，此处提前把任务标成 succeeded 会让前端以为已经完成。
    """
    settings: Settings = ctx.get("settings") or get_settings()
    project_uuid = uuid.UUID(project_id)
    job_uuid = uuid.UUID(job_id) if job_id else None

    async with job_context(
        project_id=project_uuid,
        job_id=job_uuid,
        settings=settings,
        session_factory=ctx.get("session_factory"),
        scholar_cache=ctx.get("scholar_cache"),
    ) as context:
        async with context.session() as session:
            project = await get_project(session, project_uuid)
            if project is None:
                raise ValueError(f"project not found: {project_id}")
            scope = dict(project.scope_json or {})
            topic = scope.get("topic") or project.title
            language = project.language
            paper_type = project.paper_type

        await _mark_running(context)

        if regenerate_scope or scope_needs_regeneration(scope):
            generated = await _run_stage(
                context,
                "scope",
                lambda: generate_scope(
                    topic,
                    language=language,
                    paper_type=paper_type,
                    runner=context.llm_runner(),
                ),
            )
            if generated:
                scope = {**scope, **generated}
                async with context.session() as session:
                    project = await get_project(session, project_uuid)
                    if project is not None:
                        await update_project_scope(session, project, scope)

        search_outcome = await _run_stage(
            context,
            "search",
            lambda: run_search(context, scope=scope, providers=providers),
        )
        await _run_stage(context, "curate", lambda: _assign_keys(context))
        cards_outcome = await _run_stage(
            context,
            "cards",
            lambda: generate_cards(context, language=language),
        )

        delivered = bool(search_outcome and search_outcome.persisted_entry_count)
        if finalize:
            await _finish(context, delivered=delivered)
        return {
            "project_id": project_id,
            "scope_generator": scope.get("generator"),
            "search": search_outcome.to_payload() if search_outcome else None,
            "cards": cards_outcome.to_payload() if cards_outcome else None,
            "warnings": context.warnings,
        }


async def run_import_pipeline(
    ctx: dict,
    project_id: str,
    job_id: str | None = None,
    *,
    dois: list[str] | None = None,
    bibtex: str | None = None,
) -> dict[str, Any]:
    """DOI / BibTeX 导入任务：逐条 R1 核验后入库。"""
    settings: Settings = ctx.get("settings") or get_settings()
    project_uuid = uuid.UUID(project_id)
    job_uuid = uuid.UUID(job_id) if job_id else None

    async with job_context(
        project_id=project_uuid,
        job_id=job_uuid,
        settings=settings,
        session_factory=ctx.get("session_factory"),
        scholar_cache=ctx.get("scholar_cache"),
    ) as context:
        await _mark_running(context)
        payloads: list[dict[str, Any]] = []
        if dois:
            outcome = await _run_stage(
                context,
                # 阶段名区分两种来源，checkpoint 才不会互相覆盖。
                "import_doi",
                lambda: import_references(
                    context, requests_from_dois(dois), added_via="doi_import"
                ),
                kind="import",
            )
            if outcome:
                payloads.append({"source": "doi", **outcome.to_payload()})
        if bibtex and bibtex.strip():
            outcome = await _run_stage(
                context,
                "import_bibtex",
                lambda: import_references(
                    context, requests_from_bibtex(bibtex), added_via="bibtex_import"
                ),
                kind="import",
            )
            if outcome:
                payloads.append({"source": "bibtex", **outcome.to_payload()})

        delivered = any(item.get("verified") for item in payloads)
        await _finish(context, delivered=delivered)
        return {"project_id": project_id, "imports": payloads, "warnings": context.warnings}


async def run_cards_pipeline(
    ctx: dict,
    project_id: str,
    job_id: str | None = None,
) -> dict[str, Any]:
    """仅重跑卡片抽取（前端「重新生成卡片」）。"""
    settings: Settings = ctx.get("settings") or get_settings()
    project_uuid = uuid.UUID(project_id)
    job_uuid = uuid.UUID(job_id) if job_id else None
    async with job_context(
        project_id=project_uuid,
        job_id=job_uuid,
        settings=settings,
        session_factory=ctx.get("session_factory"),
        scholar_cache=ctx.get("scholar_cache"),
    ) as context:
        async with context.session() as session:
            project = await get_project(session, project_uuid)
            language = project.language if project else "en"
        await _mark_running(context)
        outcome = await _run_stage(
            context,
            "cards",
            lambda: generate_cards(context, language=language),
        )
        await _finish(context, delivered=bool(outcome and outcome.requested))
        return {"project_id": project_id, "cards": outcome.to_payload() if outcome else None}


async def run_outline_pipeline(
    ctx: dict,
    project_id: str,
    job_id: str | None = None,
) -> dict[str, Any]:
    """OUTLINE 任务：卡片聚类 → 章节树（大纲可编辑、可重生成）。"""
    settings: Settings = ctx.get("settings") or get_settings()
    project_uuid = uuid.UUID(project_id)
    async with job_context(
        project_id=project_uuid,
        job_id=uuid.UUID(job_id) if job_id else None,
        settings=settings,
        session_factory=ctx.get("session_factory"),
        scholar_cache=ctx.get("scholar_cache"),
    ) as context:
        await _mark_running(context)
        outcome = await _run_stage(context, "outline", lambda: _outline(context))
        await _finish(context, delivered=bool(outcome and outcome.section_count))
        return {"project_id": project_id, "outline": outcome.to_payload() if outcome else None}


async def run_write_pipeline(
    ctx: dict,
    project_id: str,
    job_id: str | None = None,
    *,
    coherence: bool = True,
) -> dict[str, Any]:
    """WRITE 任务：分节写作 + 连贯性 pass + R2 收口 + 落库。"""
    settings: Settings = ctx.get("settings") or get_settings()
    project_uuid = uuid.UUID(project_id)
    async with job_context(
        project_id=project_uuid,
        job_id=uuid.UUID(job_id) if job_id else None,
        settings=settings,
        session_factory=ctx.get("session_factory"),
        scholar_cache=ctx.get("scholar_cache"),
    ) as context:
        async with context.session() as session:
            project = await get_project(session, project_uuid)
            if project is None:
                raise ValueError(f"project not found: {project_id}")
            language, title, paper_type = project.language, project.title, project.paper_type
        await _mark_running(context)
        outcome = await _run_stage(
            context,
            "write",
            lambda: write_document(
                context,
                language=language,
                title=title,
                coherence=coherence,
                paper_type=paper_type,
            ),
        )
        await _finish(context, delivered=bool(outcome and outcome.section_count))
        return {"project_id": project_id, "write": outcome.to_payload() if outcome else None}


async def run_ingest_pipeline(
    ctx: dict,
    project_id: str,
    job_id: str | None = None,
    *,
    max_works: int = 12,
) -> dict[str, Any]:
    """INGEST：OA 全文获取 → 解析 → 全文级卡片升级（M5）。"""
    settings: Settings = ctx.get("settings") or get_settings()
    project_uuid = uuid.UUID(project_id)
    async with job_context(
        project_id=project_uuid,
        job_id=uuid.UUID(job_id) if job_id else None,
        settings=settings,
        session_factory=ctx.get("session_factory"),
        scholar_cache=ctx.get("scholar_cache"),
    ) as context:
        async with context.session() as session:
            project = await get_project(session, project_uuid)
            language = project.language if project else "en"
        await _mark_running(context)
        fulltext_result = await _run_stage(
            context,
            "ingest",
            lambda: acquire_fulltexts(context, max_works=max_works),
            kind="ingest",
        )
        texts = fulltext_result[1] if fulltext_result else {}
        outcome = fulltext_result[0] if fulltext_result else None
        cards = await _run_stage(
            context,
            "cards",
            lambda: generate_cards(context, language=language, fulltexts=texts),
            kind="ingest",
        )
        await _finish(context, delivered=bool(cards and cards.requested))
        return {
            "project_id": project_id,
            "fulltext": outcome.to_payload() if outcome else None,
            "cards": cards.to_payload() if cards else None,
        }


async def run_snowball_pipeline(
    ctx: dict,
    project_id: str,
    job_id: str | None = None,
    *,
    direction: str = "both",
    max_seeds: int = 8,
) -> dict[str, Any]:
    """CURATE：对核心种子做一轮引文雪球扩展（设计 §4.4.1）。"""
    settings: Settings = ctx.get("settings") or get_settings()
    project_uuid = uuid.UUID(project_id)
    async with job_context(
        project_id=project_uuid,
        job_id=uuid.UUID(job_id) if job_id else None,
        settings=settings,
        session_factory=ctx.get("session_factory"),
        scholar_cache=ctx.get("scholar_cache"),
    ) as context:
        await _mark_running(context)
        outcome = await _run_stage(
            context,
            "snowball",
            lambda: _snowball(context, direction=direction, max_seeds=max_seeds),
            kind="search",
        )
        await _finish(context, delivered=bool(outcome and outcome.get("persisted")))
        return {"project_id": project_id, "snowball": outcome}


async def _snowball(context: JobContext, *, direction: str, max_seeds: int) -> dict[str, Any]:
    """雪球扩展：核心种子 → 邻居候选 → 去重 → 入库为 candidate。

    R1：邻居来自 provider 真实响应，因此 verified=True；但状态是 candidate，
    要用户圈选后才进写作白名单。
    """
    import asyncio as _asyncio

    from db import list_entries, upsert_entry, upsert_work
    from scholar_gateway import (
        SnowballSeed,
        dedupe_scholarly_candidates,
        expand_citation_snowball,
        select_core_works_by_influence,
    )

    async with context.session() as session:
        entries = await list_entries(session, context.project_id, status="selected")
        seeds = [
            SnowballSeed(
                work_id=str(work.id),
                openalex_id=work.openalex_id,
                semantic_scholar_id=work.semantic_scholar_id,
                citation_count=work.citation_count,
                influential_citation_count=work.influential_citation_count,
                publication_year=work.publication_year,
            )
            for _entry, work in entries
        ]
    core = select_core_works_by_influence(seeds, top_k=max_seeds)
    if not core:
        return {"seed_count": 0, "discovered": 0, "persisted": 0}

    result = await _asyncio.to_thread(
        expand_citation_snowball,
        seeds=core,
        client=context.http_client,
        user_agent=context.settings.user_agent(),
        cache=context.scholar_cache,
        direction=direction,  # type: ignore[arg-type]
        semantic_scholar_api_key=context.settings.semantic_scholar_api_key or None,
        openalex_api_key=context.settings.openalex_api_key or None,
    )
    deduped = dedupe_scholarly_candidates(list(result.discovered_candidates))
    persisted = 0
    async with context.session() as session:
        for candidate in deduped.canonical_candidates:
            work, _created = await upsert_work(session, candidate)
            await upsert_entry(
                session,
                project_id=context.project_id,
                work_id=work.id,
                added_via="snowball",
                status="candidate",
                rank_reason={"method": "snowball", "provider": candidate.provider_name},
                verified=True,
            )
            persisted += 1
    return {
        "seed_count": len(core),
        "discovered": len(result.discovered_candidates),
        "canonical": len(deduped.canonical_candidates),
        "persisted": persisted,
        "diagnostics": result.diagnostics,
    }


async def run_quality_pipeline(
    ctx: dict,
    project_id: str,
    job_id: str | None = None,
    *,
    quality_profile: str = "draft",
    review_style: str = "narrative",
    layout_checks: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """质量报告：语义软校验 + 覆盖建议 + 评分（只提示，不阻断）。"""
    settings: Settings = ctx.get("settings") or get_settings()
    project_uuid = uuid.UUID(project_id)
    async with job_context(
        project_id=project_uuid,
        job_id=uuid.UUID(job_id) if job_id else None,
        settings=settings,
        session_factory=ctx.get("session_factory"),
        scholar_cache=ctx.get("scholar_cache"),
    ) as context:
        await _mark_running(context)
        report = await _run_stage(
            context,
            "quality",
            lambda: _quality(
                context,
                quality_profile=quality_profile,
                review_style=review_style,
                layout_checks=layout_checks,
            ),
            kind="write",
        )
        await _finish(context, delivered=report is not None)
        return {"project_id": project_id, "quality": report.to_payload() if report else None}


async def _quality(
    context: JobContext,
    *,
    quality_profile: str = "draft",
    review_style: str = "narrative",
    layout_checks: dict[str, Any] | None = None,
):
    from db import (
        create_quality_report,
        document_snapshot_hash,
        get_project,
        get_writing_whitelist,
        latest_document,
        latest_quality_report,
        list_citation_usage,
        list_claim_evidence,
        list_entries,
        list_search_runs,
        list_sections,
        replace_claim_evidence,
    )

    async with context.session() as session:
        project = await get_project(session, context.project_id)
        whitelist = await get_writing_whitelist(session, context.project_id)
        document = await latest_document(session, context.project_id)
        rows = await list_sections(session, document.id) if document else []
        usage_rows = await list_citation_usage(session, context.project_id)
        entries = await list_entries(session, context.project_id, status="selected")
        cards = await _cards(session, context.project_id)
        search_runs = await list_search_runs(session, context.project_id)
        previous_report = await latest_quality_report(
            session,
            context.project_id,
            quality_profile=quality_profile,
        )
        previous_anchors = (
            await list_claim_evidence(
                session,
                context.project_id,
                quality_report_id=previous_report.id,
            )
            if previous_report is not None
            else []
        )
        abstracts = {
            entry.bibtex_key: work.abstract
            for entry, work in entries
            if entry.bibtex_key and work.abstract
        }
        years = [work.publication_year for _e, work in entries if work.publication_year]
        fulltext_used = sum(1 for card in cards if card.fulltext_used)
        scope = dict((project.scope_json or {}) if project else {})
        evidence_sources = {
            entry.bibtex_key: {
                "work_id": work.id,
                "fulltext_used": bool(card and card.fulltext_used),
                "quotable_points": (card.quotable_points_json if card else None) or [],
            }
            for entry, work in entries
            if entry.bibtex_key
            for card in [next((item for item in cards if item.work_id == work.id), None)]
        }

    frame_keys = {"abstract", "introduction", "conclusion"}
    sections = [
        {
            "section_key": row.section_key,
            "title": row.title,
            "word_count": _section_words(row.body_ir_json or {}),
            "cite_keys": row.cite_keys_json or [],
            "kind": "frame" if row.section_key in frame_keys else "body",
        }
        for row in rows
    ]
    usages = [
        {
            "cite_key": item.cite_key,
            "section_key": "",
            "context_snippet": item.context_snippet,
        }
        for item in usage_rows
    ]
    findings = await soft_check_citations(
        usages=usages,
        abstracts=abstracts,
        runner=context.llm_runner(),
    )
    report = build_quality_report(
        sections=sections,
        whitelist_size=len(whitelist),
        publication_years=years,
        fulltext_coverage=(fulltext_used / len(entries)) if entries else 0.0,
        soft_check=findings,
        scope=scope,
    )
    report.document_version = document.version if document else None
    report.paper_snapshot_hash = document_snapshot_hash(rows) if document else None
    report.quality_profile = quality_profile
    report.review_style = review_style
    report.layout_checks = layout_checks or {"status": "not_run", "passed": None}
    report.claim_evidence = build_claim_evidence(rows=rows, evidence_sources=evidence_sources)
    manual_statuses = {
        (anchor.claim_hash, anchor.cite_key, anchor.evidence_hash): anchor.manual_status
        for anchor in previous_anchors
        if anchor.manual_status != "unreviewed"
    }
    for anchor in report.claim_evidence:
        anchor["manual_status"] = manual_statuses.get(
            (anchor["claim_hash"], anchor["cite_key"], anchor["evidence_hash"]),
            "unreviewed",
        )
    if project is not None:
        apply_readiness_gate(
            report,
            rows=rows,
            project=project,
            whitelist=set(whitelist),
            search_runs=search_runs,
        )
    if document is not None and report.paper_snapshot_hash:
        async with context.session() as session:
            record = await create_quality_report(
                session,
                project_id=context.project_id,
                document_id=document.id,
                document_version=document.version,
                paper_snapshot_hash=report.paper_snapshot_hash,
                quality_profile=quality_profile,
                review_style=review_style,
                readiness_status=report.readiness_status,
                blockers=report.blockers,
                warnings=report.warnings,
                scores=report.scores,
                metrics=report.to_payload(),
                layout_checks=report.layout_checks,
            )
            await replace_claim_evidence(
                session,
                quality_report_id=record.id,
                project_id=context.project_id,
                document_id=document.id,
                anchors=report.claim_evidence,
            )
            report.report_id = str(record.id)
    await context.emit(
        "quality.metrics",
        {
            "quality_profile": quality_profile,
            "review_style": review_style,
            "readiness_status": report.readiness_status,
            "core_claim_fulltext_coverage": report.core_claim_fulltext_coverage,
            "blocker_codes": [item.get("code") for item in report.blockers],
            "warning_codes": [item.get("code") or item.get("kind") for item in report.warnings],
        },
        stage="quality",
    )
    return report


async def _cards(session, project_id):
    from db import get_cards

    return list((await get_cards(session, project_id)).values())


def _section_words(body: dict[str, Any]) -> int:
    from paperforge_worker.pipelines.quality import count_words

    return count_words(
        " ".join(
            run.get("v", "")
            for block in body.get("blocks", [])
            for run in block.get("runs", [])
            if run.get("t") == "text"
        )
    )


async def run_export_pipeline(
    ctx: dict,
    project_id: str,
    job_id: str | None = None,
    *,
    formats: list[str] | None = None,
    quality_profile: str = "draft",
    quality_report_id: str | None = None,
    readiness_status: str = "unassessed",
    paper_snapshot_hash: str | None = None,
) -> dict[str, Any]:
    """RENDER 任务：PaperIR → LaTeX 工程 → texd 编译 → 产物落对象存储。"""
    settings: Settings = ctx.get("settings") or get_settings()
    project_uuid = uuid.UUID(project_id)
    async with job_context(
        project_id=project_uuid,
        job_id=uuid.UUID(job_id) if job_id else None,
        settings=settings,
        session_factory=ctx.get("session_factory"),
        scholar_cache=ctx.get("scholar_cache"),
    ) as context:
        await _mark_running(context)
        store = _object_store(settings)
        outcome = await _run_stage(
            context,
            "render",
            lambda: export_document(
                context,
                formats=formats,
                store=store,
                quality_profile=quality_profile,
                quality_report_id=quality_report_id,
                readiness_status=readiness_status,
                paper_snapshot_hash=paper_snapshot_hash,
            ),
            kind="export",
        )
        # Draft-first：只要有任一产物（哪怕只是 LaTeX 工程 + 日志）就算交付。
        delivered = bool(outcome and outcome.formats)
        await _finish(context, delivered=delivered)
        return {"project_id": project_id, "export": outcome.to_payload() if outcome else None}


async def run_visual_suggest_pipeline(
    ctx: dict,
    project_id: str,
    job_id: str | None = None,
) -> dict[str, Any]:
    """全文完成后的视觉建议；不会调用付费图片服务或改动 PaperIR。"""
    settings: Settings = ctx.get("settings") or get_settings()
    project_uuid = uuid.UUID(project_id)
    async with job_context(
        project_id=project_uuid,
        job_id=uuid.UUID(job_id) if job_id else None,
        settings=settings,
        session_factory=ctx.get("session_factory"),
        scholar_cache=ctx.get("scholar_cache"),
    ) as context:
        await _mark_running(context)
        # 单阶段任务：这一阶段跑完任务就结束了，进度就是 1.0。
        outcome = await _run_stage(
            context,
            "visual_plan",
            lambda: suggest_visuals(context),
            kind="visual",
            progress=1.0,
        )
        await _finish(context, delivered=outcome is not None)
        return {"project_id": project_id, "visual_plan": outcome.to_payload() if outcome else None}


async def run_visual_generate_pipeline(
    ctx: dict,
    project_id: str,
    job_id: str | None = None,
    *,
    visual_id: str,
) -> dict[str, Any]:
    """渲染一个已确认规格的视觉资产。"""
    settings: Settings = ctx.get("settings") or get_settings()
    project_uuid = uuid.UUID(project_id)
    async with job_context(
        project_id=project_uuid,
        job_id=uuid.UUID(job_id) if job_id else None,
        settings=settings,
        session_factory=ctx.get("session_factory"),
        scholar_cache=ctx.get("scholar_cache"),
    ) as context:
        await _mark_running(context)
        outcome = await _run_stage(
            context,
            "visual_generate",
            lambda: generate_visual(context, uuid.UUID(visual_id)),
            kind="visual",
            progress=1.0,
        )
        await _finish(context, delivered=bool(outcome and outcome.status == "ready"))
        return {"project_id": project_id, "visual": outcome.to_payload() if outcome else None}


def _object_store(settings: Settings):
    from storage import make_object_store

    return make_object_store(settings)


async def run_full_pipeline(
    ctx: dict,
    project_id: str,
    job_id: str | None = None,
    *,
    quality_profile: str = "draft",
    review_style: str = "narrative",
) -> dict:
    """kind=full 一键生成入口。

    综述管线：scope→search→curate→ingest→cards→outline→write→quality→visual→render。
    投稿模式的质量失败保留任务技术成功，但不会进入视觉或导出。
    """
    settings: Settings = ctx.get("settings") or get_settings()
    # finalize=False：文献阶段结束不收尾，任务状态要覆盖到 render 为止。
    library = await run_library_pipeline(ctx, project_id, job_id, finalize=False)
    project_uuid = uuid.UUID(project_id)
    async with job_context(
        project_id=project_uuid,
        job_id=uuid.UUID(job_id) if job_id else None,
        settings=settings,
        session_factory=ctx.get("session_factory"),
        scholar_cache=ctx.get("scholar_cache"),
    ) as context:
        async with context.session() as session:
            project = await get_project(session, project_uuid)
            language = project.language if project else "en"
            title = (project.publication_title or project.title) if project else ""
            paper_type = project.paper_type if project else "review"
        outline_outcome = await _run_stage(
            context,
            "outline",
            lambda: _outline(context, review_style=review_style),
        )
        write_outcome = await _run_stage(
            context,
            "write",
            lambda: write_document(
                context,
                language=language,
                title=title,
                paper_type=paper_type,
            ),
        )
        quality_outcome = None
        export_outcome = None
        if write_outcome and write_outcome.section_count:
            quality_outcome = await _run_stage(
                context,
                "quality",
                lambda: _quality(
                    context,
                    quality_profile=quality_profile,
                    review_style=review_style,
                ),
                kind="write",
            )
            can_render = quality_profile == "draft" or (
                quality_outcome is not None
                and quality_outcome.readiness_status in {"preflight_ready", "submission_ready"}
            )
            if can_render:
                await _run_stage(
                    context,
                    "visual_plan",
                    lambda: suggest_visuals(context),
                    kind="visual",
                )
                export_outcome = await _run_stage(
                    context,
                    "render",
                    lambda: export_document(
                        context,
                        store=_object_store(settings),
                        quality_profile=quality_profile,
                        quality_report_id=(quality_outcome.report_id if quality_outcome else None),
                        readiness_status=(
                            quality_outcome.readiness_status if quality_outcome else "unassessed"
                        ),
                        paper_snapshot_hash=(
                            quality_outcome.paper_snapshot_hash if quality_outcome else None
                        ),
                    ),
                )
            elif quality_outcome is not None:
                await context.emit(
                    "quality.blocked",
                    {
                        "readiness_status": quality_outcome.readiness_status,
                        "blockers": quality_outcome.blockers,
                    },
                    stage="quality",
                )
        # Draft-first：正文写不出来也交付已入库的文献与大纲。
        await _finish(context, delivered=bool(library.get("search")))
        return {
            **library,
            "outline": outline_outcome.to_payload() if outline_outcome else None,
            "write": write_outcome.to_payload() if write_outcome else None,
            "quality": quality_outcome.to_payload() if quality_outcome else None,
            "export": export_outcome.to_payload() if export_outcome else None,
        }


async def _outline(context: JobContext, *, review_style: str = "narrative"):
    """从库内卡片构造大纲输入并生成章节树。"""
    from db import (
        create_outline,
        get_cards,
        get_writing_whitelist,
        list_entries,
        list_search_runs,
    )

    async with context.session() as session:
        project = await get_project(session, context.project_id)
        whitelist = await get_writing_whitelist(session, context.project_id)
        cards_by_work = await get_cards(session, context.project_id)
        entries = await list_entries(session, context.project_id, status="selected")
        search_runs = await list_search_runs(session, context.project_id)
        briefs = [
            CardBrief(
                cite_key=entry.bibtex_key,
                title=work.canonical_title,
                year=work.publication_year,
                summary=(
                    cards_by_work[work.id].summary if work.id in cards_by_work else work.abstract
                ),
                contributions=tuple(
                    cards_by_work[work.id].contributions_json or []
                    if work.id in cards_by_work
                    else []
                ),
                methods=tuple(
                    cards_by_work[work.id].methods_json or [] if work.id in cards_by_work else []
                ),
                results=tuple(
                    cards_by_work[work.id].results_json or [] if work.id in cards_by_work else []
                ),
                limitations=tuple(
                    cards_by_work[work.id].limitations_json or []
                    if work.id in cards_by_work
                    else []
                ),
                fulltext_used=bool(
                    work.id in cards_by_work and cards_by_work[work.id].fulltext_used
                ),
            )
            for entry, work in entries
            if entry.bibtex_key
        ]
        scope = dict((project.scope_json or {}) if project else {})
        language = project.language if project else "en"
        paper_type = project.paper_type if project else "review"

    completed_search = bool(search_runs) and all(
        run.status == "succeeded" and not run.error for run in search_runs
    )
    criteria = list(scope.get("inclusion_criteria") or [])
    if not criteria:
        criteria = [
            "direct relevance to the research question",
            "verifiable scholarly metadata",
            "eligible publication type and language",
        ]
    search_method = (
        {
            "databases": sorted({run.provider for run in search_runs}),
            "queries": [run.query_text for run in search_runs],
            "dates": sorted({run.executed_at.date().isoformat() for run in search_runs}),
            "hit_count": sum(int(run.hit_count or 0) for run in search_runs),
            "retrieved_count": sum(int(run.retrieved_count or 0) for run in search_runs),
            "selected_count": len(entries),
            "inclusion_criteria": criteria,
        }
        if review_style == "systematic" and completed_search
        else None
    )

    outcome = await generate_outline(
        topic=str(scope.get("topic") or (project.title if project else "")),
        research_question=str(scope.get("research_question") or ""),
        cards=briefs,
        whitelist=set(whitelist),
        language=language,
        paper_type=paper_type,
        runner=context.llm_runner(),
        review_style=review_style,
        search_method=search_method,
    )
    async with context.session() as session:
        await create_outline(session, project_id=context.project_id, tree=outcome.tree)
    return outcome


async def _assign_keys(context: JobContext) -> dict[str, Any]:
    assigned = await ensure_bibtex_keys(context)
    return {"bibtex_keys_assigned": assigned}


async def _mark_running(context: JobContext) -> None:
    if context.job_id is None:
        return
    from db.models.paper import GenerationJob

    async with context.session() as session:
        job = await session.get(GenerationJob, context.job_id)
        if job is not None:
            await update_job(session, job, status="running", progress=0.02)


async def _finish(context: JobContext, *, delivered: bool) -> None:
    """Draft-first 收尾：有产物即 succeeded（哪怕部分阶段降级）。"""
    status = "succeeded" if delivered else "failed"
    await context.emit(
        "job.finished",
        {"status": status, "warnings": context.warnings},
        stage="done",
        progress=1.0 if delivered else None,
    )
    if context.job_id is None:
        return
    from db.models.paper import GenerationJob

    async with context.session() as session:
        job = await session.get(GenerationJob, context.job_id)
        if job is not None:
            await update_job(
                session,
                job,
                status=status,
                error={"warnings": context.warnings} if context.warnings else None,
            )


async def startup(ctx: dict) -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    engine = make_engine(settings.database_url)
    ctx["settings"] = settings
    ctx["engine"] = engine
    ctx["session_factory"] = make_session_factory(engine)
    ctx["scholar_cache"] = build_scholar_cache(settings)
    logger.info("worker started")


async def shutdown(ctx: dict) -> None:
    engine = ctx.get("engine")
    if engine is not None:
        await engine.dispose()


class WorkerSettings:
    functions = [
        run_library_pipeline,
        run_import_pipeline,
        run_cards_pipeline,
        run_outline_pipeline,
        run_write_pipeline,
        run_export_pipeline,
        run_ingest_pipeline,
        run_snowball_pipeline,
        run_quality_pipeline,
        run_visual_suggest_pipeline,
        run_visual_generate_pipeline,
        # 一键生成串起 scope→…→render 十个阶段，是唯一会跑到小时级的任务：
        # 实测 7 节 / 46 篇约 22 分钟，章节与文献翻倍就顶到默认超时上。
        # 超时不是重试而是直接判失败（arq 用 asyncio.wait_for，抛的是 TimeoutError），
        # 所以宁可给宽，真挂住了还有 LLM 侧的单请求超时兜底。
        func(run_full_pipeline, timeout=FULL_PIPELINE_TIMEOUT_SECONDS),
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    # 论文生成是分钟级任务：给足超时，并限制并发以尊重 provider 配额。
    job_timeout = 3600
    max_jobs = 4
