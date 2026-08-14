"""ARQ worker 入口与任务编排（设计 §4.1 / §4.4）。

论文生成是分钟级长任务：异步 + 进度流 + 断点续跑。
阶段 checkpoint 写 `generation_job.checkpoint_json`，事件写 `job_event` 供 SSE。

Draft-first：每个阶段都包在 `_run_stage` 里——阶段失败写降级标记并继续，
只有「一件可交付产物都没有」时才把任务标成 failed。
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from functools import partial
from typing import Any

from arq import func
from arq.connections import RedisSettings
from db import (
    FINISHED_JOB_STATUSES,
    POLISH_DECISION_KEY,
    QUALITY_FINDING_COUNT_KEY,
    QUALITY_REPAIR_DECISION_KEY,
    WRITE_DOCUMENT_KEY,
    get_project,
    latest_quality_report,
    update_job,
    update_project_scope,
)
from db.session import make_engine, make_session_factory
from observability import configure_logging, get_logger, record_job_stage

from paperforge_worker.config import WorkerSettings as Settings
from paperforge_worker.config import get_settings
from paperforge_worker.context import JobContext, JobStopped, build_scholar_cache, job_context
from paperforge_worker.pipelines.cards import generate_cards
from paperforge_worker.pipelines.document import (
    repair_document_sections,
    restore_document_sections,
    snapshot_document_sections,
    write_document,
)
from paperforge_worker.pipelines.evidence import extract_evidence_units
from paperforge_worker.pipelines.export import export_document
from paperforge_worker.pipelines.fulltext import acquire_fulltexts, parse_document_file
from paperforge_worker.pipelines.importing import (
    import_references,
    requests_from_bibtex,
    requests_from_dois,
)
from paperforge_worker.pipelines.outline import CardBrief, generate_outline
from paperforge_worker.pipelines.pdf_upload import (
    match_uploaded_pdf,
    prepare_uploaded_pdf,
    set_pdf_upload_status,
    wait_for_pdf_upload_visibility,
)
from paperforge_worker.pipelines.qdecomp import persist_question_decomposition
from paperforge_worker.pipelines.qmatrix import build_question_evidence_matrix
from paperforge_worker.pipelines.quality import (
    apply_readiness_gate,
    build_claim_evidence,
    build_depth_metrics,
    build_original_claim_grounding,
    build_quality_report,
    claim_verification_cache_keys,
    repairable_finding_count,
    soft_check_citations,
    verify_claim_evidence,
    zh_language_mismatches,
)
from paperforge_worker.pipelines.readiness import (
    EvidenceReadinessReport,
    evaluate_evidence_readiness,
)
from paperforge_worker.pipelines.scope import generate_scope
from paperforge_worker.pipelines.screen import screen_eligibility
from paperforge_worker.pipelines.search import ensure_bibtex_keys, run_search
from paperforge_worker.pipelines.synthesis import (
    load_synthesis_bundles,
    synthesize_questions,
)
from paperforge_worker.pipelines.visuals import generate_visual, suggest_visuals

logger = get_logger(__name__)

# 多章节写作/重写任务的超时（秒）。检索、导出等有自身有界 I/O 的单阶段任务继续用
# 默认 job_timeout；write/polish/rebuild/quality-repair/full 都会串行调用多次 LLM，必须
# 按整篇文档规模给预算。保留旧名供已有运维探针兼容。
LONG_RUNNING_PIPELINE_TIMEOUT_SECONDS = 7200
FULL_PIPELINE_TIMEOUT_SECONDS = LONG_RUNNING_PIPELINE_TIMEOUT_SECONDS

# 正文写作前的定向补证轮数上限。每一轮都是一次真实检索 + 全文获取 + 证据抽取，
# 所以有界；但一轮是不够的——「缺第二个独立来源」往往要换一套术语才够得到。
MAX_EVIDENCE_SUPPLEMENT_ROUNDS = 2

# 综述管线阶段权重（用于进度条）。
#
# 这是**一键生成那条长管线**的绝对刻度，只对 run_full_pipeline / run_library_pipeline
# 有意义。单阶段任务（视觉建议、单张生图）不能复用它：`visual_generate` 此前在这里
# 挂着 0.75，而它根本不是全文管线的阶段——独立的生图任务因此长期停在 75%，
# 完成时才直接跳到 100%。单阶段任务改为由调用方显式传 `progress`。
_STAGE_PROGRESS = {
    "scope": 0.10,
    "qdecomp": 0.15,
    "search": 0.40,
    "screen": 0.45,
    "curate": 0.47,
    "ingest": 0.50,
    "snowball": 0.52,
    "cards": 0.55,
    "evidence": 0.60,
    "qmatrix": 0.62,
    "synth": 0.64,
    "quality": 0.91,
    "repair_search": 0.92,
    "repair_ingest": 0.93,
    "quality_repair": 0.94,
    "quality_recheck": 0.95,
    "outline": 0.65,
    "write": 0.90,
    "polish": 0.90,
    "citecheck": 0.93,
    "visual_plan": 0.96,
    "render": 0.98,
    "done": 1.0,
}


class CriticalStageFailed(RuntimeError):
    """A dependency required by downstream paid stages did not complete."""


async def _run_stage(
    context: JobContext,
    stage: str,
    runner: Callable[[], Awaitable[Any]],
    *,
    kind: str = "library",
    progress: float | None = None,
    resumable: bool = True,
    critical: bool = False,
) -> Any:
    """执行一个阶段：成功发事件，失败留降级标记并继续（gate-free）。

    ``resumable=True`` 时，此前某一轮已经跑完的阶段直接跳过——续跑 job 的
    checkpoint 被播种了上一轮的阶段产物，重跑它们等于把用户已经付过的钱再付一遍。
    """
    if resumable and context.stage_completed(stage):
        payload = context.stage_payload(stage)
        await context.emit(
            f"{stage}.skipped",
            {"reason": "already_completed", **payload},
            stage=stage,
            progress=progress if progress is not None else _STAGE_PROGRESS.get(stage),
        )
        record_job_stage(kind, stage, "skipped")
        return None
    # 阶段边界是最廉价的停止点：上一阶段的产物已经落库、下一阶段还没花钱。
    await context.raise_if_stopped()
    await context.emit(f"{stage}.started", {}, stage=stage)
    try:
        result = await runner()
    except JobStopped:
        # 必须先于下面的兜底 except：否则停止信号会被当成「阶段降级」吞掉，
        # 管线接着跑下一阶段，用户点的停止毫无效果。
        raise
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
        if critical:
            raise CriticalStageFailed(f"critical stage failed: {stage}") from error
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


def _stage_scalar(context: JobContext, stage: str, key: str, outcome: Any = None) -> Any:
    """取阶段结果里的某个标量：本轮跑过就用内存对象，跳过了就回落到 checkpoint。

    续跑时被跳过的阶段返回 None，但下游还要靠 `persisted_entry_count` /
    `section_count` / `readiness_status` 决定「有没有可交付产物」「能不能渲染」。
    这些 outcome 对象无法从 checkpoint 重建，好在 `to_payload()` 的键名与属性名
    一一对应，取标量就够了。
    """
    if outcome is not None:
        return getattr(outcome, key, None)
    return context.stage_payload(stage).get(key)


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


#: 质量门放行渲染的 readiness 取值。
RENDERABLE_READINESS = frozenset({"preflight_ready", "submission_ready"})


def can_render_after_quality(quality_profile: str, readiness_status: str) -> bool:
    """质量评估之后还要不要继续做插图与导出。

    投稿模式下，一份没过质量门的稿子不该产出导出件——用户会拿着它去投。草稿模式
    不设门槛：成稿优先，先有东西看。

    这是个具名函数而不是内联表达式，因为它是唯一一处「能不能交付」的判定，
    需要能被直接断言。此前测试只能去 `inspect.getsource(run_full_pipeline)` 里
    匹配字符串，一次等价重构就让它失效了，行为却没变。
    """
    return quality_profile == "draft" or readiness_status in RENDERABLE_READINESS


async def run_library_pipeline(
    ctx: dict,
    project_id: str,
    job_id: str | None = None,
    *,
    providers: list[str] | None = None,
    regenerate_scope: bool = True,
    acquire_fulltext: bool = False,
    max_fulltext_works: int | None = None,
    finalize: bool = True,
) -> dict[str, Any]:
    """文献主管线：SCOPE → SEARCH → CURATE → [INGEST] → CARDS。

    ``finalize=False`` 供 ``run_full_pipeline`` 复用：全管线还要继续跑
    outline/write/render，此处提前把任务标成 succeeded 会让前端以为已经完成。
    一键全流程用 ``acquire_fulltext=True``；普通检索入口保持快速摘要卡片语义。
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
        event_publisher=ctx.get("redis"),
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

        qdecomp_outcome = None
        strict_dependencies = acquire_fulltext and not finalize
        if paper_type == "review":
            qdecomp_outcome = await _run_stage(
                context,
                "qdecomp",
                lambda: persist_question_decomposition(
                    context,
                    scope=scope,
                    topic=str(topic),
                    language=language,
                ),
                critical=strict_dependencies,
            )

        search_outcome = await _run_stage(
            context,
            "search",
            lambda: run_search(context, scope=scope, providers=providers),
            critical=strict_dependencies,
        )
        await _run_stage(
            context,
            "screen",
            lambda: screen_eligibility(context, scope=scope),
            critical=strict_dependencies,
        )
        # SCREEN may promote lower-ranked search candidates.  Assign citation
        # keys afterwards so newly selected evidence is immediately citable by
        # OUTLINE/WRITE rather than merely visible in the matrix.
        await _run_stage(
            context,
            "curate",
            lambda: _assign_keys(context),
            critical=strict_dependencies,
        )
        fulltext_outcome = None
        fulltexts: dict[str, str] = {}
        if acquire_fulltext:
            fulltext_result = await _run_stage(
                context,
                "ingest",
                lambda: acquire_fulltexts(
                    context,
                    max_works=max_fulltext_works or settings.fulltext_max_works,
                ),
                kind="ingest",
                # 解析文本没有写进 checkpoint；若 cards 尚未完成，续跑时必须能重新
                # 读取/解析全文。内容寻址对象与卡片 source_hash 会避免重复 LLM 花费。
                resumable=False,
                critical=strict_dependencies,
            )
            if fulltext_result:
                fulltext_outcome, fulltexts = fulltext_result
        cards_outcome = await _run_stage(
            context,
            "cards",
            lambda: generate_cards(context, language=language, fulltexts=fulltexts),
            critical=strict_dependencies,
        )
        evidence_outcome = await _run_stage(
            context,
            "evidence",
            lambda: extract_evidence_units(context, fulltexts=fulltexts),
            critical=strict_dependencies,
        )
        qmatrix_outcome = None
        synthesis_outcome = None
        if paper_type == "review":
            qmatrix_outcome = await _run_stage(
                context,
                "qmatrix",
                lambda: build_question_evidence_matrix(context, language=language),
                critical=strict_dependencies,
            )
            synthesis_outcome = await _run_stage(
                context,
                "synth",
                lambda: synthesize_questions(context),
                critical=strict_dependencies,
            )

        delivered = bool(_stage_scalar(context, "search", "persisted_entry_count", search_outcome))
        if finalize:
            await _finish(context, delivered=delivered)
        return {
            "project_id": project_id,
            "scope_generator": scope.get("generator"),
            # 跳过的阶段没有 outcome 对象，但产物还在库里：回落到 checkpoint，
            # 否则续跑的 run_full_pipeline 会因为 library["search"] 为空而判定「没交付」。
            "search": (
                search_outcome.to_payload() if search_outcome else context.stage_payload("search")
            )
            or None,
            "cards": (
                cards_outcome.to_payload() if cards_outcome else context.stage_payload("cards")
            )
            or None,
            "fulltext": (
                fulltext_outcome.to_payload()
                if fulltext_outcome
                else context.stage_payload("ingest")
            )
            or None,
            "evidence": (
                evidence_outcome.to_payload()
                if evidence_outcome
                else context.stage_payload("evidence")
            )
            or None,
            "question_decomposition": (
                qdecomp_outcome.to_payload()
                if qdecomp_outcome
                else context.stage_payload("qdecomp")
            )
            or None,
            "question_evidence_matrix": (
                qmatrix_outcome.to_payload()
                if qmatrix_outcome
                else context.stage_payload("qmatrix")
            )
            or None,
            "synthesis": (
                synthesis_outcome.to_payload()
                if synthesis_outcome
                else context.stage_payload("synth")
            )
            or None,
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
        event_publisher=ctx.get("redis"),
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
        event_publisher=ctx.get("redis"),
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


async def run_qdecomp_pipeline(
    ctx: dict,
    project_id: str,
    job_id: str | None = None,
) -> dict[str, Any]:
    """独立重建研究问题树，供研究问题工作台重试。"""
    settings: Settings = ctx.get("settings") or get_settings()
    project_uuid = uuid.UUID(project_id)
    async with job_context(
        project_id=project_uuid,
        job_id=uuid.UUID(job_id) if job_id else None,
        settings=settings,
        session_factory=ctx.get("session_factory"),
        scholar_cache=ctx.get("scholar_cache"),
        event_publisher=ctx.get("redis"),
    ) as context:
        async with context.session() as session:
            project = await get_project(session, project_uuid)
            if project is None:
                raise ValueError(f"project not found: {project_id}")
            scope = dict(project.scope_json or {})
        await _mark_running(context)
        outcome = await _run_stage(
            context,
            "qdecomp",
            lambda: persist_question_decomposition(
                context,
                scope=scope,
                topic=project.title,
                language=project.language,
            ),
            kind="qdecomp",
        )
        await _finish(context, delivered=bool(outcome and outcome.sub_question_count))
        return {"project_id": project_id, "questions": outcome.to_payload() if outcome else None}


async def run_evidence_pipeline(
    ctx: dict,
    project_id: str,
    job_id: str | None = None,
) -> dict[str, Any]:
    """独立获取全文并重建 EvidenceUnit，供证据矩阵工作台重试。"""
    settings: Settings = ctx.get("settings") or get_settings()
    project_uuid = uuid.UUID(project_id)
    async with job_context(
        project_id=project_uuid,
        job_id=uuid.UUID(job_id) if job_id else None,
        settings=settings,
        session_factory=ctx.get("session_factory"),
        scholar_cache=ctx.get("scholar_cache"),
        event_publisher=ctx.get("redis"),
    ) as context:
        await _mark_running(context)
        fulltext_result = await _run_stage(
            context,
            "ingest",
            lambda: acquire_fulltexts(context, max_works=settings.fulltext_max_works),
            kind="evidence",
            resumable=False,
        )
        fulltexts = fulltext_result[1] if fulltext_result else {}
        outcome = await _run_stage(
            context,
            "evidence",
            lambda: extract_evidence_units(context, fulltexts=fulltexts),
            kind="evidence",
        )
        await _finish(context, delivered=bool(outcome and outcome.units))
        return {"project_id": project_id, "evidence": outcome.to_payload() if outcome else None}


async def run_qmatrix_pipeline(
    ctx: dict,
    project_id: str,
    job_id: str | None = None,
) -> dict[str, Any]:
    """独立重建自动矩阵；人工覆盖的单元由仓储层保留。"""
    settings: Settings = ctx.get("settings") or get_settings()
    project_uuid = uuid.UUID(project_id)
    async with job_context(
        project_id=project_uuid,
        job_id=uuid.UUID(job_id) if job_id else None,
        settings=settings,
        session_factory=ctx.get("session_factory"),
        scholar_cache=ctx.get("scholar_cache"),
        event_publisher=ctx.get("redis"),
    ) as context:
        async with context.session() as session:
            project = await get_project(session, project_uuid)
            language = project.language if project else "en"
        await _mark_running(context)
        outcome = await _run_stage(
            context,
            "qmatrix",
            lambda: build_question_evidence_matrix(context, language=language),
            kind="qmatrix",
        )
        await _finish(context, delivered=bool(outcome and outcome.links))
        return {"project_id": project_id, "matrix": outcome.to_payload() if outcome else None}


async def run_alignment_pipeline(
    ctx: dict,
    project_id: str,
    job_id: str | None = None,
) -> dict[str, Any]:
    """Rebuild automatic links and immediately refresh synthesis statuses."""
    settings: Settings = ctx.get("settings") or get_settings()
    project_uuid = uuid.UUID(project_id)
    async with job_context(
        project_id=project_uuid,
        job_id=uuid.UUID(job_id) if job_id else None,
        settings=settings,
        session_factory=ctx.get("session_factory"),
        scholar_cache=ctx.get("scholar_cache"),
        event_publisher=ctx.get("redis"),
    ) as context:
        async with context.session() as session:
            project = await get_project(session, project_uuid)
            language = project.language if project else "en"
        await _mark_running(context)
        matrix = await _run_stage(
            context,
            "qmatrix",
            lambda: build_question_evidence_matrix(context, language=language),
            kind="qmatrix",
            critical=True,
        )
        synthesis = await _run_stage(
            context,
            "synth",
            lambda: synthesize_questions(context),
            kind="synth",
            critical=True,
        )
        readiness = await _evaluate_prewrite_evidence(
            context,
            quality_profile="scholarly",
            review_style="narrative",
            matrix_payload=matrix.to_payload() if matrix else None,
        )
        if readiness.ready:
            await _finish(context, delivered=bool(synthesis and synthesis.bundles))
        else:
            await _finish_needs_input(context, readiness)
        return {
            "project_id": project_id,
            "matrix": matrix.to_payload() if matrix else None,
            "synthesis": synthesis.to_payload() if synthesis else None,
            "evidence_readiness": readiness.to_payload(),
        }


async def run_synthesis_pipeline(
    ctx: dict,
    project_id: str,
    job_id: str | None = None,
) -> dict[str, Any]:
    """独立重算一致、冲突、不可比与缺口判定。"""
    settings: Settings = ctx.get("settings") or get_settings()
    async with job_context(
        project_id=uuid.UUID(project_id),
        job_id=uuid.UUID(job_id) if job_id else None,
        settings=settings,
        session_factory=ctx.get("session_factory"),
        scholar_cache=ctx.get("scholar_cache"),
        event_publisher=ctx.get("redis"),
    ) as context:
        await _mark_running(context)
        outcome = await _run_stage(
            context,
            "synth",
            lambda: synthesize_questions(context),
            kind="synth",
            critical=True,
        )
        readiness = await _evaluate_prewrite_evidence(
            context,
            quality_profile="scholarly",
            review_style="narrative",
        )
        if readiness.ready:
            await _finish(context, delivered=bool(outcome and outcome.bundles))
        else:
            await _finish_needs_input(context, readiness)
        return {
            "project_id": project_id,
            "synthesis": outcome.to_payload() if outcome else None,
            "evidence_readiness": readiness.to_payload(),
        }


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
        event_publisher=ctx.get("redis"),
    ) as context:
        async with context.session() as session:
            project = await get_project(session, project_uuid)
        await _mark_running(context)
        if project is not None and project.paper_type == "review":
            readiness, _enrichments = await _prewrite_evidence_gate(
                context,
                quality_profile="scholarly",
                review_style="narrative",
                language=project.language,
            )
            if not readiness.ready:
                await _finish_needs_input(context, readiness)
                return {
                    "project_id": project_id,
                    "outline": None,
                    "evidence_readiness": readiness.to_payload(),
                }
        outcome = await _run_stage(
            context,
            "outline",
            lambda: _outline(context),
            critical=True,
        )
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
        event_publisher=ctx.get("redis"),
    ) as context:
        async with context.session() as session:
            project = await get_project(session, project_uuid)
            if project is None:
                raise ValueError(f"project not found: {project_id}")
            language, title, paper_type = project.language, project.title, project.paper_type
        await _mark_running(context)
        if paper_type == "review":
            readiness, _enrichments = await _prewrite_evidence_gate(
                context,
                quality_profile="scholarly",
                review_style="narrative",
                language=language,
            )
            if not readiness.ready:
                await _finish_needs_input(context, readiness)
                return {
                    "project_id": project_id,
                    "write": None,
                    "evidence_readiness": readiness.to_payload(),
                }
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
            critical=True,
        )
        await _finish(context, delivered=bool(outcome and outcome.section_count))
        return {"project_id": project_id, "write": outcome.to_payload() if outcome else None}


async def run_draft_rebuild_pipeline(
    ctx: dict,
    project_id: str,
    job_id: str | None = None,
    *,
    quality_profile: str = "scholarly",
    review_style: str = "narrative",
) -> dict[str, Any]:
    """Create a new outline/document version from the latest synthesis."""
    settings: Settings = ctx.get("settings") or get_settings()
    project_uuid = uuid.UUID(project_id)
    async with job_context(
        project_id=project_uuid,
        job_id=uuid.UUID(job_id) if job_id else None,
        settings=settings,
        session_factory=ctx.get("session_factory"),
        scholar_cache=ctx.get("scholar_cache"),
        event_publisher=ctx.get("redis"),
    ) as context:
        async with context.session() as session:
            project = await get_project(session, project_uuid)
            if project is None:
                raise ValueError(f"project not found: {project_id}")
            language = project.language
            title = project.publication_title or project.title
            paper_type = project.paper_type
        await _mark_running(context)
        if paper_type == "review":
            readiness, _enrichments = await _prewrite_evidence_gate(
                context,
                quality_profile=quality_profile,
                review_style=review_style,
                language=language,
            )
            if not readiness.ready:
                await _finish_needs_input(context, readiness)
                return {
                    "project_id": project_id,
                    "outline": None,
                    "write": None,
                    "quality": None,
                    "evidence_readiness": readiness.to_payload(),
                }
        outline = await _run_stage(
            context,
            "outline",
            lambda: _outline(context, review_style=review_style),
            critical=True,
        )
        write = await _run_stage(
            context,
            "write",
            lambda: write_document(
                context,
                language=language,
                title=title,
                coherence=False,
                paper_type=paper_type,
                outline_id=_stage_scalar(context, "outline", "outline_id", outline),
            ),
            kind="write",
            critical=True,
        )
        quality = None
        if write and write.section_count:
            quality = await _run_stage(
                context,
                "quality",
                lambda: _quality(
                    context,
                    quality_profile=quality_profile,
                    review_style=review_style,
                ),
                kind="write",
            )
        if (
            quality_profile != "draft"
            and quality is not None
            and quality.readiness_status not in RENDERABLE_READINESS
        ):
            await _finish_needs_input(context, quality)
        else:
            await _finish(context, delivered=bool(write and write.section_count))
        return {
            "project_id": project_id,
            "outline": outline.to_payload() if outline else None,
            "write": write.to_payload() if write else None,
            "quality": quality.to_payload() if quality else None,
        }


async def run_polish_pipeline(
    ctx: dict,
    project_id: str,
    job_id: str | None = None,
    *,
    source_job_id: str,
    quality_profile: str = "scholarly",
    review_style: str = "narrative",
) -> dict[str, Any]:
    """用户确认后的独立润色任务，并刷新质量报告与导出件。

    新任务的 checkpoint 由 API 播种 ``WRITE_DOCUMENT_KEY``，所以
    ``write_document`` 会恢复首稿各节、跳过重新写作，只执行连贯性润色。
    """
    settings: Settings = ctx.get("settings") or get_settings()
    project_uuid = uuid.UUID(project_id)
    async with job_context(
        project_id=project_uuid,
        job_id=uuid.UUID(job_id) if job_id else None,
        settings=settings,
        session_factory=ctx.get("session_factory"),
        scholar_cache=ctx.get("scholar_cache"),
        event_publisher=ctx.get("redis"),
    ) as context:
        async with context.session() as session:
            project = await get_project(session, project_uuid)
            if project is None:
                raise ValueError(f"project not found: {project_id}")
            language = project.language
            title = project.publication_title or project.title
            paper_type = project.paper_type
        await _mark_running(context)
        write_outcome = await _run_stage(
            context,
            "polish",
            lambda: write_document(
                context,
                language=language,
                title=title,
                coherence=True,
                paper_type=paper_type,
            ),
            kind="write",
        )
        quality_outcome = None
        repair_history: list[dict[str, Any]] = []
        export_outcome = None
        if _stage_scalar(context, "polish", "section_count", write_outcome):
            quality_outcome = await _run_stage(
                context,
                "quality",
                lambda: _quality(
                    context,
                    quality_profile=("draft" if quality_profile == "draft" else "scholarly"),
                    review_style=review_style,
                ),
                kind="write",
            )
            if (
                quality_profile != "draft"
                and quality_outcome is not None
                and quality_outcome.readiness_status != "preflight_ready"
            ):
                quality_outcome, repair_history = await _converge_scholarly_quality(
                    context,
                    initial_report=quality_outcome,
                    language=language,
                    paper_type=paper_type,
                    review_style=review_style,
                )
            if (
                quality_profile == "submission"
                and quality_outcome is not None
                and quality_outcome.readiness_status == "preflight_ready"
            ):
                quality_outcome = await _quality(
                    context,
                    quality_profile="submission",
                    review_style=review_style,
                )
            if quality_outcome is not None:
                await context.emit(
                    "quality.final",
                    quality_outcome.to_payload(),
                    stage="quality_recheck" if repair_history else "quality",
                    checkpoint={
                        "quality": quality_outcome.to_payload(),
                        "quality_repair_history": repair_history,
                    },
                )
            readiness = (
                _stage_scalar(context, "quality", "readiness_status", quality_outcome)
                or "unassessed"
            )
            if can_render_after_quality(quality_profile, readiness):
                export_outcome = await _run_stage(
                    context,
                    "render",
                    lambda: export_document(
                        context,
                        store=_object_store(settings),
                        quality_profile=quality_profile,
                        quality_report_id=_stage_scalar(
                            context, "quality", "report_id", quality_outcome
                        ),
                        readiness_status=readiness,
                        paper_snapshot_hash=_stage_scalar(
                            context, "quality", "paper_snapshot_hash", quality_outcome
                        ),
                    ),
                )
        decision = (
            "skipped"
            if write_outcome is not None and write_outcome.polish_skipped
            else "completed"
            if write_outcome is not None and write_outcome.section_count
            else "failed"
        )
        await _set_source_decision(context, source_job_id, POLISH_DECISION_KEY, decision)
        if (
            quality_profile != "draft"
            and quality_outcome is not None
            and quality_outcome.readiness_status not in RENDERABLE_READINESS
        ):
            await _finish_needs_input(context, quality_outcome)
        else:
            await _finish(context, delivered=decision in {"completed", "skipped"})
        return {
            "project_id": project_id,
            "polish": write_outcome.to_payload() if write_outcome else None,
            "quality": quality_outcome.to_payload() if quality_outcome else None,
            "export": export_outcome.to_payload() if export_outcome else None,
        }


async def _set_source_decision(
    context: JobContext,
    source_job_id: str,
    key: str,
    decision: str,
) -> None:
    """把可选收尾任务的结果回写首轮 full job，供刷新后的界面恢复决策状态。

    润色和质量修复共用这一条：两者都是 full job 交付之后的独立付费任务，
    界面靠 full job checkpoint 上的决策字段判断该显示邀请、进行中还是已完成。
    """
    from db.models.paper import GenerationJob

    try:
        source_uuid = uuid.UUID(source_job_id)
    except ValueError:
        return
    async with context.session() as session:
        source = await session.get(GenerationJob, source_uuid)
        if source is not None and source.project_id == context.project_id and source.kind == "full":
            await update_job(session, source, checkpoint={key: decision})


async def run_ingest_pipeline(
    ctx: dict,
    project_id: str,
    job_id: str | None = None,
    *,
    max_works: int | None = None,
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
        event_publisher=ctx.get("redis"),
    ) as context:
        async with context.session() as session:
            project = await get_project(session, project_uuid)
            language = project.language if project else "en"
        await _mark_running(context)
        fulltext_result = await _run_stage(
            context,
            "ingest",
            lambda: acquire_fulltexts(
                context,
                max_works=max_works or settings.fulltext_max_works,
            ),
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


async def run_pdf_match_pipeline(
    ctx: dict,
    project_id: str,
    job_id: str | None = None,
    *,
    upload_id: str,
) -> dict[str, Any]:
    """Identify an uploaded private PDF without admitting it to the library."""
    settings: Settings = ctx.get("settings") or get_settings()
    async with job_context(
        project_id=uuid.UUID(project_id),
        job_id=uuid.UUID(job_id) if job_id else None,
        settings=settings,
        session_factory=ctx.get("session_factory"),
        scholar_cache=ctx.get("scholar_cache"),
        event_publisher=ctx.get("redis"),
    ) as context:
        # PDF dispatch commits before enqueue. Keep a short visibility wait as
        # defence for cross-session scheduling and independently invoked jobs.
        await wait_for_pdf_upload_visibility(context, upload_id)
        await _mark_running(context)
        outcome = await _run_stage(
            context,
            "pdf_match",
            lambda: match_uploaded_pdf(context, upload_id),
            kind="ingest",
            progress=0.85,
            resumable=False,
        )
        delivered = bool(outcome and outcome.status == "needs_confirmation")
        if outcome is not None and outcome.failure_reason:
            context.warn(
                "pdf_match",
                outcome.failure_reason,
                {"upload_id": upload_id},
            )
        await _finish(context, delivered=delivered)
        return {
            "project_id": project_id,
            "upload_id": upload_id,
            "match": outcome.to_payload() if outcome else None,
        }


async def run_uploaded_pdf_pipeline(
    ctx: dict,
    project_id: str,
    job_id: str | None = None,
    *,
    upload_id: str,
) -> dict[str, Any]:
    """Parse a confirmed PDF, then refresh cards, evidence, matrix and synthesis."""
    settings: Settings = ctx.get("settings") or get_settings()
    project_uuid = uuid.UUID(project_id)
    async with job_context(
        project_id=project_uuid,
        job_id=uuid.UUID(job_id) if job_id else None,
        settings=settings,
        session_factory=ctx.get("session_factory"),
        scholar_cache=ctx.get("scholar_cache"),
        event_publisher=ctx.get("redis"),
    ) as context:
        # Confirmation, DocumentFile binding and job creation commit together.
        # This also waits out the previously committed needs_confirmation state.
        await prepare_uploaded_pdf(context, upload_id)
        await _mark_running(context)
        target = await _run_stage(
            context,
            "pdf_prepare",
            lambda: prepare_uploaded_pdf(context, upload_id),
            kind="ingest",
            progress=0.08,
            resumable=False,
        )
        if target is None:
            await _finish(context, delivered=False)
            return {"project_id": project_id, "upload_id": upload_id, "status": "failed"}
        if target.already_ready:
            await _finish(context, delivered=True)
            return {"project_id": project_id, "upload_id": upload_id, "status": "ready"}

        await set_pdf_upload_status(context, target.upload_id, "parsing", error=None)
        parse_result = await _run_stage(
            context,
            "pdf_parse",
            lambda: parse_document_file(context, target.document_file_id),
            kind="ingest",
            progress=0.30,
            resumable=False,
        )
        parse_outcome = parse_result[0] if parse_result else None
        fulltexts = parse_result[1] if parse_result else {}
        if parse_outcome is None or not parse_outcome.parsed:
            context.warn(
                "pdf_parse",
                (
                    parse_outcome.error
                    if parse_outcome is not None and parse_outcome.error
                    else "pdf_parse_stage_failed"
                ),
                {"upload_id": upload_id},
            )
            error = {
                "stage": "pdf_parse",
                "reason": (
                    parse_outcome.error if parse_outcome is not None else "pdf_parse_stage_failed"
                ),
                "warnings": context.warnings,
            }
            await set_pdf_upload_status(
                context,
                target.upload_id,
                "parse_failed",
                error=error,
            )
            await _finish(context, delivered=False)
            return {
                "project_id": project_id,
                "upload_id": upload_id,
                "status": "parse_failed",
                "parse": parse_outcome.to_payload() if parse_outcome else None,
            }

        await set_pdf_upload_status(context, target.upload_id, "extracting", error=None)
        async with context.session() as session:
            project = await get_project(session, project_uuid)
            language = project.language if project else "en"

        cards = await _run_stage(
            context,
            "cards",
            lambda: generate_cards(
                context,
                language=language,
                fulltexts=fulltexts,
                work_ids=[target.work_id],
            ),
            kind="ingest",
            progress=0.52,
            resumable=False,
        )
        evidence = await _run_stage(
            context,
            "evidence",
            lambda: extract_evidence_units(
                context,
                fulltexts=fulltexts,
                work_ids=[target.work_id],
            ),
            kind="ingest",
            progress=0.72,
            resumable=False,
        )
        matrix = await _run_stage(
            context,
            "qmatrix",
            lambda: build_question_evidence_matrix(context, language=language),
            kind="ingest",
            progress=0.84,
            resumable=False,
        )
        synthesis = await _run_stage(
            context,
            "synth",
            lambda: synthesize_questions(context),
            kind="ingest",
            progress=0.94,
            resumable=False,
        )
        if any(item is None for item in (cards, evidence, matrix, synthesis)):
            await set_pdf_upload_status(
                context,
                target.upload_id,
                "ready",
                error={
                    "stage": "downstream_extraction",
                    "reason": "one_or_more_stages_failed",
                    "warnings": context.warnings,
                },
            )
            # Parsing is the terminal requirement for "全文可用".  Downstream
            # analysis can be retried through the existing literature/evidence
            # jobs, so never leave the upload in a permanently polling state.
            await _finish(context, delivered=True)
            return {
                "project_id": project_id,
                "upload_id": upload_id,
                "status": "ready",
                "analysis_status": "partial",
                "parse": parse_outcome.to_payload(),
                "warnings": context.warnings,
            }

        await set_pdf_upload_status(context, target.upload_id, "ready", error=None)
        await _finish(context, delivered=True)
        return {
            "project_id": project_id,
            "upload_id": upload_id,
            "status": "ready",
            "parse": parse_outcome.to_payload(),
            "cards": cards.to_payload(),
            "evidence": evidence.to_payload(),
            "matrix": matrix.to_payload(),
            "synthesis": synthesis.to_payload(),
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
        event_publisher=ctx.get("redis"),
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
    quality_profile: str = "scholarly",
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
        event_publisher=ctx.get("redis"),
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
        if (
            quality_profile != "draft"
            and report is not None
            and report.readiness_status not in RENDERABLE_READINESS
        ):
            await _finish_needs_input(context, report)
        else:
            await _finish(context, delivered=report is not None)
        return {"project_id": project_id, "quality": report.to_payload() if report else None}


async def run_quality_repair_pipeline(
    ctx: dict,
    project_id: str,
    job_id: str | None = None,
    *,
    quality_profile: str = "scholarly",
    review_style: str = "narrative",
    source_job_id: str | None = None,
) -> dict[str, Any]:
    """独立质量修复：重查当前稿，最多补一次证据并做两轮单调局部改写。

    与全流程使用同一套收敛器，避免写作页的“重新检查”只能重复报告问题、却没有
    任何办法推进稿件。每轮只有阻断项减少且不引入关键退化才接受，否则恢复原稿。

    ``source_job_id`` 由概览页的修复决策卡传入：全流程交付后不再自动收敛，
    这条任务就是用户对那个决策点的回答，结果要回写到 full job 上。写作页的
    「重新检查并修复」不带来源，行为不变。
    """
    settings: Settings = ctx.get("settings") or get_settings()
    project_uuid = uuid.UUID(project_id)
    async with job_context(
        project_id=project_uuid,
        job_id=uuid.UUID(job_id) if job_id else None,
        settings=settings,
        session_factory=ctx.get("session_factory"),
        scholar_cache=ctx.get("scholar_cache"),
        event_publisher=ctx.get("redis"),
    ) as context:
        async with context.session() as session:
            project = await get_project(session, project_uuid)
            if project is None:
                raise ValueError(f"project not found: {project_id}")
            language = project.language
            paper_type = project.paper_type

        await _mark_running(context)
        report = await _run_stage(
            context,
            "quality",
            lambda: _quality(
                context,
                quality_profile=("draft" if quality_profile == "draft" else "scholarly"),
                review_style=review_style,
            ),
            kind="write",
        )
        repair_history: list[dict[str, Any]] = []
        if (
            quality_profile != "draft"
            and report is not None
            and report.readiness_status != "preflight_ready"
        ):
            report, repair_history = await _converge_scholarly_quality(
                context,
                initial_report=report,
                language=language,
                paper_type=paper_type,
                review_style=review_style,
            )
        if (
            quality_profile == "submission"
            and report is not None
            and report.readiness_status == "preflight_ready"
        ):
            report = await _quality(
                context,
                quality_profile="submission",
                review_style=review_style,
            )
        if report is not None:
            await context.emit(
                "quality.final",
                report.to_payload(),
                stage="quality_recheck" if repair_history else "quality",
                checkpoint={
                    "quality": report.to_payload(),
                    "quality_repair_history": repair_history,
                },
            )
        # 收敛器接受了改写就意味着正文变了，此前那份导出件已经对不上稿子。
        # 全流程现在一定会先产出一份 PDF，不在这里重导出，用户点完「修复」
        # 下载到的还是修复前的版本——比不修复更糟，因为他以为已经改过了。
        export_outcome = None
        if report is not None and any(entry["accepted"] for entry in repair_history):
            export_outcome = await _run_stage(
                context,
                "render",
                lambda: export_document(
                    context,
                    store=_object_store(settings),
                    quality_profile=quality_profile,
                    quality_report_id=report.report_id,
                    readiness_status=report.readiness_status,
                    paper_snapshot_hash=report.paper_snapshot_hash,
                ),
            )
        if source_job_id:
            await _set_source_decision(
                context,
                source_job_id,
                QUALITY_REPAIR_DECISION_KEY,
                "completed" if report is not None else "failed",
            )
        if (
            quality_profile != "draft"
            and report is not None
            and report.readiness_status not in RENDERABLE_READINESS
        ):
            await _finish_needs_input(context, report)
        else:
            await _finish(context, delivered=report is not None)
        return {
            "project_id": project_id,
            "quality": report.to_payload() if report else None,
            "quality_repair_history": repair_history,
            "export": export_outcome.to_payload() if export_outcome else None,
        }


async def _quality(
    context: JobContext,
    *,
    quality_profile: str = "scholarly",
    review_style: str = "narrative",
    layout_checks: dict[str, Any] | None = None,
):
    from db import (
        document_snapshot_hash,
        evidence_payload,
        get_claim_entailment_cache,
        get_project,
        get_writing_whitelist,
        grounded_asset_payloads,
        latest_document,
        latest_quality_report,
        list_citation_usage,
        list_claim_evidence,
        list_entries,
        list_evidence_measurements,
        list_evidence_units,
        list_research_questions,
        list_search_runs,
        list_sections,
        store_claim_entailment_cache,
    )

    async with context.session() as session:
        project = await get_project(session, context.project_id)
        whitelist = await get_writing_whitelist(session, context.project_id)
        document = await latest_document(session, context.project_id)
        rows = await list_sections(session, document.id) if document else []
        usage_rows = await list_citation_usage(session, context.project_id)
        current_section_ids = {row.id for row in rows}
        # CitationUsage is project-scoped and retains historical document
        # versions.  Quality must assess the current manuscript only.
        usage_rows = [usage for usage in usage_rows if usage.section_id in current_section_ids]
        entries = await list_entries(session, context.project_id, status="selected")
        cards = await _cards(session, context.project_id)
        search_runs = await list_search_runs(session, context.project_id)
        questions = await list_research_questions(session, context.project_id)
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
        evidence_rows = await list_evidence_units(session, context.project_id)
        measurements = await list_evidence_measurements(
            session,
            [item.id for item in evidence_rows],
        )
        evidence_units = {
            str(item.id): evidence_payload(item, measurements.get(item.id))
            for item in evidence_rows
        }
        grounded_assets = await grounded_asset_payloads(session, context.project_id)
        assets_by_ref = {
            str(item.get("_asset_ref")): item for item in grounded_assets if item.get("_asset_ref")
        }
        cite_key_by_work_id = {
            str(work.id): entry.bibtex_key for entry, work in entries if entry.bibtex_key
        }
        evidence_excerpts: dict[str, list[str]] = {}
        for item in evidence_units.values():
            cite_key = cite_key_by_work_id.get(str(item.get("work_id") or ""))
            if cite_key and item.get("text"):
                evidence_excerpts.setdefault(cite_key, []).append(str(item["text"]))
        semantic_sources = {
            cite_key: "\n".join(excerpts)[:4000] for cite_key, excerpts in evidence_excerpts.items()
        }
        semantic_sources.update(
            {
                cite_key: abstract
                for cite_key, abstract in abstracts.items()
                if cite_key not in semantic_sources
            }
        )

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
        abstracts=semantic_sources,
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
    unused_core_literature = (
        _unused_core_literature(entries, usage_rows) if document is not None else []
    )
    report.claim_evidence = build_claim_evidence(
        rows=rows,
        evidence_sources=evidence_sources,
        evidence_units=evidence_units,
    )
    if project is not None and project.paper_type == "original":
        report.claim_evidence.extend(
            build_original_claim_grounding(rows=rows, assets_by_ref=assets_by_ref)
        )
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
    claim_entailment_mode = getattr(context.settings, "claim_entailment_mode", "shadow")
    verifier_runner = context.llm_runner()
    if claim_entailment_mode != "off" and verifier_runner.enabled:
        verifier_model = verifier_runner.model_for("verifier")
        cache_keys = claim_verification_cache_keys(
            report.claim_evidence,
            model=verifier_model,
        )
        missing_cache_keys = cache_keys.difference(context.claim_verification_cache)
        if missing_cache_keys:
            try:
                async with context.session() as session:
                    context.claim_verification_cache.update(
                        await get_claim_entailment_cache(session, missing_cache_keys)
                    )
            except Exception:  # noqa: BLE001 - cache outages must not fail quality verification
                logger.warning(
                    "failed to load claim entailment cache",
                    extra={"project_id": str(context.project_id)},
                    exc_info=True,
                )
    claim_verification = await verify_claim_evidence(
        anchors=report.claim_evidence,
        runner=verifier_runner,
        cache=context.claim_verification_cache,
        mode=claim_entailment_mode,
    )
    if (
        claim_verification["model_checked_count"]
        or claim_verification["alternative_model_checked_count"]
    ):
        try:
            async with context.session() as session:
                await store_claim_entailment_cache(
                    session,
                    [
                        entry
                        for entry in context.claim_verification_cache.values()
                        if entry.get("cache_key")
                    ],
                )
        except Exception:  # noqa: BLE001 - a cache write failure cannot invalidate verdicts
            logger.warning(
                "failed to persist claim entailment cache",
                extra={"project_id": str(context.project_id)},
                exc_info=True,
            )
    if claim_verification["candidate_count"]:
        await context.emit(
            "quality.claim_evidence_verification",
            claim_verification,
            stage="quality",
        )
    if project is not None:
        apply_readiness_gate(
            report,
            rows=rows,
            project=project,
            whitelist=set(whitelist),
            search_runs=search_runs,
        )
    if project is not None and project.paper_type == "original":
        from ingest.numlint import lint_sections

        number_report = lint_sections(
            [
                {
                    "section_key": row.section_key,
                    "text": _body_text_for_quality(row.body_ir_json or {}),
                }
                for row in rows
            ],
            parsed_assets=grounded_assets,
            paper_type="original",
        )
        report.depth_metrics["original_numlint"] = number_report.to_payload()
        if not number_report.consistent:
            issue = {
                "code": "unsourced_numeric_claim",
                "message": f"{len(number_report.unsourced)} 处原创论文数字无法追溯到用户素材",
                "count": len(number_report.unsourced),
                "samples": [item.to_payload() for item in number_report.unsourced[:10]],
            }
            report.warnings.append(issue)
            if quality_profile != "draft":
                report.blockers.append(issue)
                report.readiness_status = "needs_revision"
    if unused_core_literature:
        report.warnings.append(
            {
                "code": "core_literature_unused",
                "message": "核心文献已完成评估，但尚未在当前正文中引用",
                "evaluated": True,
                "work_ids": [item["work_id"] for item in unused_core_literature],
                "titles": [item["title"] for item in unused_core_literature],
                "literature": unused_core_literature,
            }
        )
    # Evidence-stage failure/partial completion is a delivery fact, not merely
    # an event-stream warning.  Persist it in the report so an independent
    # export cannot silently turn a degraded scholarly run into a deliverable.
    incomplete_stages = [
        stage
        for stage in ("ingest", "evidence", "qmatrix", "synth")
        if f"{stage}_failed" in context.checkpoint
    ]
    evidence_stage_payload = context.stage_payload("evidence")
    evidence_failed = int(evidence_stage_payload.get("works_failed") or 0)
    if incomplete_stages or evidence_failed:
        issue = {
            "code": "evidence_stage_incomplete",
            "message": "证据相关阶段未完整完成，需修复后重新质检",
            "stages": incomplete_stages,
            "works_failed": evidence_failed,
        }
        report.warnings.append(issue)
        if quality_profile != "draft":
            report.blockers.append(issue)
            report.readiness_status = "needs_revision"
    if project is not None and project.language == "zh":
        mismatches = zh_language_mismatches(rows)
        if mismatches:
            issue = {
                "code": "language_mismatch",
                "message": "中文项目含有以英文为主的正文段落，需改写后再导出",
                "paragraphs": mismatches,
            }
            report.warnings.append(issue)
            if quality_profile != "draft":
                report.blockers.append(issue)
                report.readiness_status = "needs_revision"
    depth_metrics = build_depth_metrics(
        report=report,
        rows=rows,
        evidence_units=evidence_units,
        selected_work_count=len(entries),
        questions=questions,
    )
    if claim_verification["candidate_count"]:
        depth_metrics["claim_evidence_verification"] = {
            key: claim_verification[key]
            for key in (
                "mode",
                "status",
                "candidate_count",
                "scheduled_count",
                "checked_count",
                "would_promote_count",
                "raw_would_demote_count",
                "would_demote_count",
                "promoted_count",
                "demoted_count",
                "failed_count",
                "unverified_count",
                "cache_hit_count",
                "persistent_cache_hit_count",
                "job_cache_hit_count",
                "model_checked_count",
                "alternative_checked_count",
                "alternative_failed_count",
                "alternative_cache_hit_count",
                "alternative_model_checked_count",
                "unsafe_demotion_avoided_count",
                "demotion_unconfirmed_count",
                "review_incomplete_count",
            )
        }
        if claim_verification["mode"] == "enforce" and claim_verification["failed_count"]:
            report.warnings.append(
                {
                    "code": "claim_evidence_verification_incomplete",
                    "message": "部分核心论断未完成语义证据核验；既有确定性判定保持不变，需复查",
                    "count": claim_verification["failed_count"],
                }
            )
    report.depth_metrics = {**depth_metrics, **report.depth_metrics}
    if document is not None and report.paper_snapshot_hash:
        await _publish_quality_report(
            context,
            report,
            document_id=document.id,
            document_version=document.version,
            quality_profile=quality_profile,
            review_style=review_style,
        )
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


async def _publish_quality_report(
    context: JobContext,
    report: Any,
    *,
    document_id: uuid.UUID,
    document_version: int,
    quality_profile: str,
    review_style: str,
) -> None:
    """把一份算好的报告落库，并让它成为该档位下唯一有效的结论。

    ``create_quality_report`` 会把同项目同档位此前所有非 stale 的报告置 stale，
    所以「落库」同时也是「切换当前有效结论」——导出与界面读的都是这一份。
    ``report.report_id`` 随之指向新行，调用方后续按 report_id 取论断锚点才取得到。
    """
    from db import create_quality_report, replace_claim_evidence

    async with context.session() as session:
        record = await create_quality_report(
            session,
            project_id=context.project_id,
            document_id=document_id,
            document_version=document_version,
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
            document_id=document_id,
            anchors=report.claim_evidence,
        )
        report.report_id = str(record.id)


async def _republish_after_rollback(
    context: JobContext,
    report: Any,
    *,
    quality_profile: str,
    review_style: str,
) -> Any | None:
    """正文回滚之后，让 live 质量报告重新对上稿子；对不上则返回 None。

    为什么必须做点什么：被拒的候选报告此刻是库里唯一 live 的报告（写它的时候
    把上一份置了 stale），可它描述的是刚刚被回滚掉的那一版正文。放着不管，
    导出与界面就会拿着一份已经不存在的稿子的结论——readiness、阻断项、
    paper_snapshot_hash 全是错的。

    为什么不再跑一次 ``_quality``：那是一次完整的全文 LLM 评估（引用软校验 +
    跨语言论断核验）。``restore_document_sections`` 是逐字回滚，正文此刻与
    ``report`` 评估时完全一致，重算只会花钱重新得出同一个结论；更糟的是 LLM
    不确定性可能让它得出**不同**的结论，于是「被拒绝的这一轮」反而改变了判定。

    快照哈希是这条捷径的守卫：只有回滚后的正文确实与 ``report`` 对得上才走它。
    对不上说明回滚没有完全复原，那时返回 None，由调用方退回真正的重新评估。
    """
    from db import document_snapshot_hash, latest_document, list_sections

    async with context.session() as session:
        document = await latest_document(session, context.project_id)
        rows = await list_sections(session, document.id) if document else []
    if document is None or not rows or not report.paper_snapshot_hash:
        return None
    if document_snapshot_hash(rows) != report.paper_snapshot_hash:
        return None
    await _publish_quality_report(
        context,
        report,
        document_id=document.id,
        document_version=document.version,
        quality_profile=quality_profile,
        review_style=review_style,
    )
    return report


async def _cards(session, project_id):
    from db import get_cards

    return list((await get_cards(session, project_id)).values())


def _unused_core_literature(
    entries: list[tuple[Any, Any]],
    usage_rows: list[Any],
) -> list[dict[str, Any]]:
    """Return core/pinned works not cited in the current manuscript."""
    used_work_ids = {usage.work_id for usage in usage_rows}
    return [
        {
            "work_id": str(work.id),
            "title": work.canonical_title,
            "cite_key": entry.bibtex_key,
        }
        for entry, work in entries
        if (getattr(entry, "literature_role", "general") == "core" or bool(entry.user_pinned))
        and work.id not in used_work_ids
    ]


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


def _body_text_for_quality(body: dict[str, Any]) -> str:
    return " ".join(
        str(run.get("v") or "")
        for block in body.get("blocks") or []
        for runs in (
            [block.get("runs") or []]
            if block.get("type") == "paragraph"
            else [item.get("runs") or [] for item in block.get("items") or []]
        )
        for run in runs
        if isinstance(run, dict) and run.get("t") == "text"
    )


async def run_export_pipeline(
    ctx: dict,
    project_id: str,
    job_id: str | None = None,
    *,
    formats: list[str] | None = None,
    quality_profile: str = "scholarly",
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
        event_publisher=ctx.get("redis"),
    ) as context:
        await _mark_running(context)
        # Independent exports previously trusted caller-supplied readiness and
        # could bypass a failed/missing QUALITY stage.  Resolve the current
        # report from the database for non-draft deliveries instead.
        if quality_profile != "draft":
            async with context.session() as session:
                report = await latest_quality_report(session, project_uuid)
            if report is None or not can_render_after_quality(
                quality_profile, report.readiness_status
            ):
                actual_readiness = report.readiness_status if report else "unassessed"
                await context.emit(
                    "quality.blocked",
                    {
                        "readiness_status": actual_readiness,
                        "reason": "independent_export_requires_passing_quality_report",
                    },
                    stage="quality",
                )
                await _finish(context, delivered=False)
                return {
                    "project_id": project_id,
                    "export": None,
                    "blocked": True,
                    "readiness_status": actual_readiness,
                }
            quality_report_id = str(report.id)
            readiness_status = report.readiness_status
            paper_snapshot_hash = report.paper_snapshot_hash
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
        event_publisher=ctx.get("redis"),
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
        event_publisher=ctx.get("redis"),
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


def _blocker_instances(report: Any) -> int:
    return sum(max(1, int(item.get("count") or 1)) for item in (report.blockers or []))


_NON_DEGENERATION_CODES = frozenset(
    {
        "supported_core_claims_missing",
        "review_source_diversity_low",
        "original_method_grounding_missing",
        "original_results_grounding_missing",
    }
)


def _repair_candidate_improves(previous: Any, candidate: Any) -> bool:
    if candidate.readiness_status == "preflight_ready":
        return True
    previous_codes = {str(item.get("code")) for item in previous.blockers or []}
    candidate_codes = {str(item.get("code")) for item in candidate.blockers or []}
    introduced_degeneration = (candidate_codes - previous_codes) & _NON_DEGENERATION_CODES
    return not introduced_degeneration and _blocker_instances(candidate) < _blocker_instances(
        previous
    )


def _claim_anchor_requires_repair(anchor: Any) -> bool:
    """与 readiness gate 使用相同规则，避免重写已经人工闭合的章节。"""
    located = bool(
        anchor.source_page is not None
        or anchor.source_section
        or anchor.source_paragraph is not None
    )
    resolved = bool(
        anchor.source_kind in {"fulltext", "user_asset"}
        and anchor.grade_ok is not False
        and anchor.comparability_ok is not False
        and anchor.manual_status != "rejected"
        and (
            anchor.support_status == "supported"
            or (anchor.manual_status == "confirmed" and located)
        )
    )
    return not resolved


async def _quality_failing_sections(context: JobContext, report: Any) -> set[str]:
    from db import latest_document, list_claim_evidence, list_sections

    section_keys: set[str] = set()
    if report.report_id:
        async with context.session() as session:
            anchors = await list_claim_evidence(
                session,
                context.project_id,
                quality_report_id=uuid.UUID(str(report.report_id)),
                core_only=True,
            )
        section_keys.update(
            anchor.section_key for anchor in anchors if _claim_anchor_requires_repair(anchor)
        )
    for blocker in report.blockers or []:
        section_keys.update(str(key) for key in blocker.get("section_keys") or [])
        section_keys.update(str(key) for key in blocker.get("sections") or [])
    if section_keys:
        return section_keys
    async with context.session() as session:
        document = await latest_document(session, context.project_id)
        rows = await list_sections(session, document.id) if document else []
    return {
        row.section_key
        for row in rows
        if row.section_key not in {"abstract", "introduction", "conclusion", "search_methods"}
    }


async def _supplement_review_evidence(
    context: JobContext,
    *,
    language: str,
    report: EvidenceReadinessReport | None = None,
    round_index: int = 1,
) -> dict[str, Any]:
    """One bounded, gap-typed retrieval pass for under-evidenced sub-questions.

    补证按**缺口类型**分派，而不是把同一批 ``search_query`` 原样再发一遍：

    * ``needs_second_source``——已经有证据、只差第二个独立来源。重发原查询只会
      再拿回同一批文献，所以这里改发别名替换后的同义查询（换一套术语通常就换到
      了另一个作者群），并在 SCREEN 里把已经链到该问题的文献降权，让追加预算
      真的买到新来源。
    * ``no_candidates``——一条证据都没有。原查询已经被证明无效，因此改用别名与
      比较维度重组查询，并放宽年份窗口。
    """
    from db import (
        get_cards,
        list_entries,
        list_evidence_units,
        list_research_questions,
    )

    async with context.session() as session:
        project = await get_project(session, context.project_id)
        questions = await list_research_questions(session, context.project_id, kind="sub")
        selected_before = {
            work.id
            for _entry, work in await list_entries(
                session,
                context.project_id,
                status="selected",
            )
        }
        scope = dict(project.scope_json or {}) if project else {}
        linked_work_ids = await _question_linked_work_ids(session, context.project_id)

    gaps = _classify_evidence_gaps(questions, report)
    if not gaps:
        # 没有可定向补的缺口（例如唯一的降级项是分类器异常）。再检索一轮只会
        # 把同一批文献重新买一遍。
        return {"round": round_index, "gaps": [], "skipped": "no_targetable_gap"}
    repair_questions = [
        {
            "text": question.text,
            "search_query": question.search_query,
            "term_aliases": question.term_aliases_json,
        }
        for question, _kind in gaps
    ]
    repair_scope = {**scope, "sub_questions": repair_questions}
    # 零候选的问题连年份窗口都可能是元凶，放宽它；只缺第二来源的不动窗口。
    widened_scope = _widen_time_range(repair_scope)
    deprioritized = {
        work_id
        for question, kind in gaps
        if kind == "needs_second_source"
        for work_id in linked_work_ids.get(question.id, set())
    }

    search = None
    for gap_kind, search_scope in (
        ("needs_second_source", repair_scope),
        ("no_candidates", widened_scope),
    ):
        queries = _gap_repair_queries(
            [question for question, kind in gaps if kind == gap_kind],
            gap_kind,
        )
        if not queries:
            continue
        outcome = await _run_stage(
            context,
            "repair_search",
            partial(
                run_search,
                context,
                scope=search_scope,
                query_texts=queries,
                limit_per_provider=min(12, context.settings.search_limit_per_provider),
            ),
            kind="library",
            resumable=False,
        )
        search = outcome or search
    await screen_eligibility(
        context,
        scope=repair_scope,
        focus_questions=repair_questions,
        preserve_selected=True,
        additional_budget=min(15, max(4, len(repair_questions) * 3)),
        deprioritize_work_ids=deprioritized,
    )
    await _assign_keys(context)
    async with context.session() as session:
        selected_after = {
            work.id
            for _entry, work in await list_entries(
                session,
                context.project_id,
                status="selected",
            )
        }
    new_work_ids = sorted(selected_after - selected_before, key=str)
    fulltext_result = await _run_stage(
        context,
        "repair_ingest",
        lambda: acquire_fulltexts(context, max_works=context.settings.fulltext_max_works),
        kind="ingest",
        resumable=False,
    )
    fulltexts = fulltext_result[1] if isinstance(fulltext_result, tuple) else {}
    async with context.session() as session:
        cards_by_work = await get_cards(session, context.project_id)
        durable_evidence = await list_evidence_units(session, context.project_id)
    repair_work_ids = _repair_processing_ids(
        selected_work_ids=selected_after,
        new_work_ids=set(new_work_ids),
        fulltext_work_ids=set(fulltexts),
        card_work_ids=set(cards_by_work),
        eligible_evidence_work_ids={
            unit.work_id
            for unit in durable_evidence
            if unit.grade in {"A_located_structured", "B_located_prose", "C_fulltext_unlocated"}
        },
        checkpoint_work_ids=context.checkpoint.get("repair_work_ids"),
    )
    await context.emit(
        "quality_repair.processing_set",
        {
            "new_selected_count": len(new_work_ids),
            "processing_count": len(repair_work_ids),
        },
        stage="repair_ingest",
        checkpoint={"repair_work_ids": [str(work_id) for work_id in repair_work_ids]},
    )
    cards = await generate_cards(
        context,
        language=language,
        fulltexts=fulltexts,
        work_ids=repair_work_ids,
    )
    evidence = await extract_evidence_units(
        context,
        fulltexts=fulltexts,
        work_ids=repair_work_ids,
    )
    matrix = await build_question_evidence_matrix(context, language=language)
    synthesis = await synthesize_questions(context)
    payload = {
        "round": round_index,
        "gaps": [
            {"question_id": str(question.id), "gap": kind, "text": question.text[:120]}
            for question, kind in gaps
        ],
        "search": search.to_payload() if search else None,
        "new_selected_work_ids": [str(work_id) for work_id in new_work_ids],
        "new_selected_count": len(new_work_ids),
        "processed_work_ids": [str(work_id) for work_id in repair_work_ids],
        "processed_work_count": len(repair_work_ids),
        "cards": cards.to_payload(),
        "evidence": evidence.to_payload(),
        "matrix": matrix.to_payload(),
        "synthesis": synthesis.to_payload(),
    }
    await context.emit(
        "quality_repair.enrichment_completed",
        payload,
        stage="repair_ingest",
        checkpoint={"quality_repair_enrichment": payload},
    )
    return payload


async def _question_linked_work_ids(
    session: Any,
    project_id: uuid.UUID,
) -> dict[uuid.UUID, set[uuid.UUID]]:
    """每个子问题当前已经链到哪些文献（用于「再找一个不同来源」）。"""
    from db import list_evidence_units, list_question_evidence_links

    units = {unit.id: unit for unit in await list_evidence_units(session, project_id)}
    linked: dict[uuid.UUID, set[uuid.UUID]] = {}
    for link in await list_question_evidence_links(session, project_id):
        unit = units.get(link.evidence_unit_id)
        if unit is not None:
            linked.setdefault(link.research_question_id, set()).add(unit.work_id)
    return linked


def _classify_evidence_gaps(
    questions: list[Any],
    report: EvidenceReadinessReport | None,
) -> list[tuple[Any, str]]:
    """把「哪些问题缺证」翻译成「缺的是哪一种证」。

    没有 readiness 报告时（独立管线的首轮）退回按 ``answer_status`` 判断。
    """
    by_id = {str(question.id): question for question in questions if question.search_query}
    gaps: list[tuple[Any, str]] = []
    if report is not None and report.question_details:
        for detail in report.question_details:
            question = by_id.get(str(detail.get("question_id") or ""))
            if question is None or detail.get("ready"):
                continue
            # 有证据但只有一个来源 → 缺的是独立性，不是相关性。
            second_source = int(detail.get("eligible_evidence_count") or 0) > 0
            gaps.append((question, "needs_second_source" if second_source else "no_candidates"))
    else:
        gaps = [
            (question, "no_candidates")
            for question in by_id.values()
            if question.answer_status == "insufficient_evidence"
        ]
    # 空表示没有哪个问题缺证。以前这里会退回「把所有问题再检索一遍」，那正是
    # 单轮补证既贵又无效的原因之一。
    return gaps


def _gap_repair_queries(questions: list[Any], gap_kind: str, *, limit: int = 6) -> list[str]:
    """按缺口类型生成检索式变体（而不是把原查询原样重发）。"""
    variants: list[str] = []
    for question in questions:
        base = " ".join(str(getattr(question, "search_query", "") or "").split())
        if not base:
            continue
        aliases = _alias_map(getattr(question, "term_aliases_json", None))
        substitutions = _alias_substitutions(base, aliases)
        if gap_kind == "no_candidates":
            # 原查询已经零候选，重发没有意义：换术语，并把过窄的长查询截短。
            variants.extend(substitutions)
            tokens = base.split()
            if len(tokens) > 3:
                variants.append(" ".join(tokens[:3]))
            dimensions = [
                cleaned
                for value in getattr(question, "comparison_dimensions_json", None) or []
                if (cleaned := " ".join(str(value).split()))
            ]
            variants.extend(
                f"{alias} {dimensions[0]}" for alias in _flatten(aliases)[:2] if dimensions
            )
        else:
            # 只缺第二来源：保留原查询，另加同义变体去够到用别的术语写作的作者群。
            variants.append(base)
            variants.extend(substitutions)
    return list(dict.fromkeys(query for query in variants if _is_ascii_query(query)))[:limit]


def _alias_map(raw: Any) -> dict[str, list[str]]:
    if isinstance(raw, dict):
        return {
            str(term): [str(item) for item in values if str(item).strip()]
            for term, values in raw.items()
            if isinstance(values, list)
        }
    if isinstance(raw, list):
        return {"": [str(item) for item in raw if str(item).strip()]}
    return {}


def _flatten(aliases: dict[str, list[str]]) -> list[str]:
    return list(dict.fromkeys(alias for values in aliases.values() for alias in values))


def _alias_substitutions(base: str, aliases: dict[str, list[str]], *, limit: int = 2) -> list[str]:
    """把查询里的术语替换成同义词，得到指向不同文献群的查询。"""
    lowered = base.casefold()
    variants: list[str] = []
    for term, values in aliases.items():
        if not term or term.casefold() not in lowered:
            continue
        for alias in values[:limit]:
            if alias.casefold() in lowered:
                continue
            index = lowered.index(term.casefold())
            variants.append(f"{base[:index]}{alias}{base[index + len(term) :]}".strip())
    return variants[:limit]


def _is_ascii_query(text: str) -> bool:
    """provider 只索引英文题录；混入 CJK 的查询等于零召回。"""
    return bool(text) and text.isascii() and any(char.isalpha() for char in text)


def _widen_time_range(scope: dict[str, Any]) -> dict[str, Any]:
    """零候选时先怀疑年份窗口：把起始年往前放宽五年。"""
    time_range = scope.get("time_range")
    if not isinstance(time_range, dict) or not isinstance(time_range.get("start_year"), int):
        return scope
    widened = {**time_range, "start_year": time_range["start_year"] - 5}
    return {**scope, "time_range": widened}


def _repair_processing_ids(
    *,
    selected_work_ids: set[uuid.UUID],
    new_work_ids: set[uuid.UUID],
    fulltext_work_ids: set[str],
    card_work_ids: set[uuid.UUID],
    eligible_evidence_work_ids: set[uuid.UUID],
    checkpoint_work_ids: Any,
) -> list[uuid.UUID]:
    """Recover partially processed repair papers as well as newly selected ones."""
    checkpoint_ids: set[uuid.UUID] = set()
    for value in checkpoint_work_ids if isinstance(checkpoint_work_ids, list) else []:
        try:
            checkpoint_ids.add(uuid.UUID(str(value)))
        except ValueError:
            continue
    parsed_fulltext_ids: set[uuid.UUID] = set()
    for value in fulltext_work_ids:
        try:
            parsed_fulltext_ids.add(uuid.UUID(str(value)))
        except ValueError:
            continue
    incomplete = {
        work_id
        for work_id in selected_work_ids & parsed_fulltext_ids
        if work_id not in card_work_ids or work_id not in eligible_evidence_work_ids
    }
    return sorted(
        selected_work_ids & (new_work_ids | checkpoint_ids | incomplete),
        key=str,
    )


async def _evaluate_prewrite_evidence(
    context: JobContext,
    *,
    quality_profile: str,
    review_style: str,
    matrix_payload: dict[str, Any] | None = None,
) -> EvidenceReadinessReport:
    synthesis = await load_synthesis_bundles(context)
    report = evaluate_evidence_readiness(
        synthesis.bundles,
        matrix_payload=matrix_payload,
        quality_profile=quality_profile,
        review_style=review_style,
    )
    await context.emit(
        "evidence_readiness.evaluated",
        report.to_payload(),
        stage="synth",
        checkpoint={"evidence_readiness": report.to_payload()},
    )
    return report


async def _prewrite_evidence_gate(
    context: JobContext,
    *,
    quality_profile: str,
    review_style: str,
    language: str,
    matrix_payload: dict[str, Any] | None = None,
) -> tuple[EvidenceReadinessReport, list[dict[str, Any]]]:
    """评估 → 定向补证（最多两轮）→ 重新评估，正文写作前的唯一入口。

    以前补证只写在 run_full_pipeline 里、且只跑一轮，独立的 outline/write/rebuild
    直接阻断——同一个项目从不同入口进来会得到完全不同的结论。
    """
    report = await _evaluate_prewrite_evidence(
        context,
        quality_profile=quality_profile,
        review_style=review_style,
        matrix_payload=matrix_payload,
    )
    enrichments: list[dict[str, Any]] = []
    for round_index in range(1, MAX_EVIDENCE_SUPPLEMENT_ROUNDS + 1):
        if not report.blockers and not report.degradations:
            break
        enrichment = await _supplement_review_evidence(
            context,
            language=language,
            report=report,
            round_index=round_index,
        )
        enrichments.append(enrichment)
        if enrichment.get("skipped"):
            break
        previous_ready = report.ready_questions
        report = await _evaluate_prewrite_evidence(
            context,
            quality_profile=quality_profile,
            review_style=review_style,
            matrix_payload=enrichment.get("matrix"),
        )
        if report.ready_questions <= previous_ready and not enrichment.get("new_selected_count"):
            # 这一轮既没带回新文献也没提升覆盖率，再来一轮只是重复烧钱。
            break
    if report.ready and report.degraded:
        # Draft-first：覆盖率没达标不阻断，但必须留痕——缺证的子问题在正文里会被
        # 写成显式的证据缺口小节。用户必须在任务警告里看到这件事，否则降级稿会被
        # 当成完整结论。
        await context.emit(
            "evidence_readiness.degraded",
            report.to_payload(),
            stage="synth",
        )
        for issue in report.degradations:
            context.warn(
                "evidence_readiness",
                str(issue.get("code") or "evidence_degraded"),
                {"message": issue.get("message")},
            )
    return report, enrichments


async def _converge_scholarly_quality(
    context: JobContext,
    *,
    initial_report: Any,
    language: str,
    paper_type: str,
    review_style: str,
) -> tuple[Any, list[dict[str, Any]]]:
    """At most two monotonic local rewrite rounds after the pre-write evidence gate."""
    current = initial_report
    history: list[dict[str, Any]] = []
    for attempt in range(1, 3):
        section_keys = await _quality_failing_sections(context, current)
        before = _blocker_instances(current)
        snapshot = await snapshot_document_sections(context)
        await context.emit(
            "quality_repair.started",
            {"attempt": attempt, "blockers": current.blockers, "sections": sorted(section_keys)},
            stage="quality_repair",
            checkpoint={"quality_repair_attempt": attempt},
        )
        try:
            await repair_document_sections(
                context,
                section_keys=section_keys,
                language=language,
                paper_type=paper_type,
            )
            candidate = await _quality(
                context,
                quality_profile="scholarly",
                review_style=review_style,
            )
        except Exception as error:  # noqa: BLE001 - restore the last known-good manuscript
            await restore_document_sections(context, snapshot)
            context.warn(
                "quality_repair",
                type(error).__name__,
                {"message": str(error)[:300], "attempt": attempt},
            )
            await context.emit(
                "quality_repair.failed",
                {
                    "attempt": attempt,
                    "error": type(error).__name__,
                    "message": str(error)[:300],
                },
                stage="quality_recheck",
            )
            break
        after = _blocker_instances(candidate)
        accepted = _repair_candidate_improves(current, candidate)
        if not accepted:
            await restore_document_sections(context, snapshot)
            # 回滚之后库里 live 的还是被拒候选的报告，必须让它重新对上正文。
            # 正文是逐字复原的，所以先试着把 current 这份结论重新落库（无 LLM 调用）；
            # 快照哈希对不上才说明复原不完整，那时才真的重新评估一次。
            candidate = await _republish_after_rollback(
                context,
                current,
                quality_profile="scholarly",
                review_style=review_style,
            )
            if candidate is None:
                candidate = await _quality(
                    context,
                    quality_profile="scholarly",
                    review_style=review_style,
                )
            after = _blocker_instances(candidate)
        entry = {
            "attempt": attempt,
            "before": before,
            "after": after,
            "accepted": accepted,
            "sections": sorted(section_keys),
            "report_id": candidate.report_id,
            "readiness_status": candidate.readiness_status,
        }
        history.append(entry)
        await context.emit(
            "quality_repair.completed",
            entry,
            stage="quality_recheck",
            checkpoint={"quality_repair_history": list(history)},
        )
        current = candidate
        if current.readiness_status == "preflight_ready":
            break
    return current, history


async def run_full_pipeline(
    ctx: dict,
    project_id: str,
    job_id: str | None = None,
    *,
    quality_profile: str = "scholarly",
    review_style: str = "narrative",
) -> dict:
    """kind=full 一键生成入口。

    严谨/投稿模式会先评估 scholarly gate，最多做一轮证据补充和两轮
    单调局部重写。只有通过质量门才会生成新导出件；未收敛时保留最佳稿件并
    以 needs_input 收尾。
    """
    settings: Settings = ctx.get("settings") or get_settings()
    # finalize=False：文献阶段结束不收尾，任务状态要覆盖到 render 为止。
    library = await run_library_pipeline(
        ctx,
        project_id,
        job_id,
        acquire_fulltext=True,
        finalize=False,
    )
    if library is None:
        # 文献那一段被用户停掉了。job_context 吞掉 JobStopped 后管线函数返回 None，
        # 这里不早退就会接着开第二个 job_context 去跑 outline/write——用户点了停止，
        # 结果最贵的两个阶段照跑不误。
        return {"project_id": project_id, "stopped": True}
    project_uuid = uuid.UUID(project_id)
    async with job_context(
        project_id=project_uuid,
        job_id=uuid.UUID(job_id) if job_id else None,
        settings=settings,
        session_factory=ctx.get("session_factory"),
        scholar_cache=ctx.get("scholar_cache"),
        event_publisher=ctx.get("redis"),
    ) as context:
        async with context.session() as session:
            project = await get_project(session, project_uuid)
            language = project.language if project else "en"
            title = (project.publication_title or project.title) if project else ""
            paper_type = project.paper_type if project else "review"
        evidence_readiness = None
        enrichments: list[dict[str, Any]] = []
        if paper_type == "review":
            evidence_readiness, enrichments = await _prewrite_evidence_gate(
                context,
                quality_profile=quality_profile,
                review_style=review_style,
                language=language,
                matrix_payload=library.get("question_evidence_matrix"),
            )
            if not evidence_readiness.ready:
                await context.emit(
                    "evidence_readiness.blocked",
                    evidence_readiness.to_payload(),
                    stage="synth",
                )
                await _finish_needs_input(context, evidence_readiness)
                return {
                    **library,
                    "evidence_readiness": evidence_readiness.to_payload(),
                    "evidence_enrichment": enrichments[-1] if enrichments else None,
                    "outline": None,
                    "write": None,
                    "quality": None,
                    "quality_repair_history": [],
                    "export": None,
                }
        outline_outcome = await _run_stage(
            context,
            "outline",
            lambda: _outline(context, review_style=review_style),
            critical=True,
        )
        write_outcome = await _run_stage(
            context,
            "write",
            lambda: write_document(
                context,
                language=language,
                title=title,
                coherence=False,
                paper_type=paper_type,
                outline_id=_stage_scalar(
                    context,
                    "outline",
                    "outline_id",
                    outline_outcome,
                ),
            ),
            critical=True,
        )
        quality_outcome = None
        repair_history: list[dict[str, Any]] = []
        export_outcome = None
        # 续跑时 write 可能被跳过（上一轮已写完），outcome 为 None 但正文确实在库里，
        # 标量因此一律走 _stage_scalar：本轮跑过取内存对象，跳过了取 checkpoint。
        if _stage_scalar(context, "write", "section_count", write_outcome):
            quality_outcome = await _run_stage(
                context,
                "quality",
                lambda: _quality(
                    context,
                    quality_profile=("draft" if quality_profile == "draft" else "scholarly"),
                    review_style=review_style,
                ),
                kind="write",
            )
            if (
                quality_profile != "draft"
                and quality_outcome is not None
                and quality_outcome.readiness_status != "preflight_ready"
            ):
                quality_outcome, repair_history = await _converge_scholarly_quality(
                    context,
                    initial_report=quality_outcome,
                    language=language,
                    paper_type=paper_type,
                    review_style=review_style,
                )
            if (
                quality_profile == "submission"
                and quality_outcome is not None
                and quality_outcome.readiness_status == "preflight_ready"
            ):
                quality_outcome = await _quality(
                    context,
                    quality_profile="submission",
                    review_style=review_style,
                )
            if quality_outcome is not None:
                await context.emit(
                    "quality.final",
                    quality_outcome.to_payload(),
                    stage="quality_recheck" if repair_history else "quality",
                    checkpoint={
                        "quality": quality_outcome.to_payload(),
                        "quality_repair_history": repair_history,
                    },
                )
            readiness = (
                _stage_scalar(context, "quality", "readiness_status", quality_outcome)
                or "unassessed"
            )
            if can_render_after_quality(quality_profile, readiness):
                await _run_stage(
                    context,
                    "visual_plan",
                    # 一键全流程不再铺开多条视觉建议：只创建并尝试通过 Yunwu
                    # 生成一张论文摘要图。视觉失败会退回可重试建议，不阻断导出。
                    lambda: suggest_visuals(
                        context,
                        summary_only=True,
                        auto_generate=True,
                    ),
                    kind="visual",
                )
                export_outcome = await _run_stage(
                    context,
                    "render",
                    lambda: export_document(
                        context,
                        store=_object_store(settings),
                        quality_profile=quality_profile,
                        quality_report_id=_stage_scalar(
                            context, "quality", "report_id", quality_outcome
                        ),
                        readiness_status=readiness,
                        paper_snapshot_hash=_stage_scalar(
                            context, "quality", "paper_snapshot_hash", quality_outcome
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
        document_id = _stage_scalar(context, "write", "document_id", write_outcome)
        if (
            _stage_scalar(context, "write", "section_count", write_outcome)
            and document_id
            and (
                quality_profile == "draft"
                or (
                    quality_outcome is not None
                    and quality_outcome.readiness_status in RENDERABLE_READINESS
                )
            )
        ):
            await context.emit(
                "polish.available",
                {
                    "document_id": document_id,
                    "message": "初稿已交付，可由用户决定是否额外润色",
                },
                checkpoint={
                    POLISH_DECISION_KEY: "pending",
                    WRITE_DOCUMENT_KEY: document_id,
                },
            )
            # 质量修复和润色一样，是交付之后的用户决策而不是管线的一环。draft 档
            # 已经跑过一次完整评估，发现项都在报告里；要不要为它们再花一轮
            # 「重写未达标章节 + 全文重新评估」（最坏两轮改写 + 四次重评），
            # 由用户看着问题清单决定。scholarly / submission 是用户主动选的严格档，
            # 收敛在管线内就已经跑过，这里不再重复邀请。
            findings = (
                repairable_finding_count(quality_outcome)
                if quality_profile == "draft" and quality_outcome is not None
                else 0
            )
            if findings:
                await context.emit(
                    "quality_repair.available",
                    {
                        "document_id": document_id,
                        "finding_count": findings,
                        "message": "初稿已交付；质检发现的问题可由用户决定是否修复",
                    },
                    checkpoint={
                        QUALITY_REPAIR_DECISION_KEY: "pending",
                        QUALITY_FINDING_COUNT_KEY: findings,
                    },
                )
        # 严谨稿没有通过质量门时，保留正文但明确要求补材料；不能把“一篇未导出的
        # needs_revision 稿”谎报成全流程成功。
        if quality_profile != "draft" and quality_outcome is None:
            await _finish(context, delivered=False)
        elif (
            quality_profile != "draft"
            and quality_outcome is not None
            and quality_outcome.readiness_status not in RENDERABLE_READINESS
        ):
            await _finish_needs_input(context, quality_outcome)
        else:
            delivered = export_outcome is not None or bool(context.stage_payload("render"))
            await _finish(context, delivered=delivered)
        return {
            **library,
            "evidence_readiness": (evidence_readiness.to_payload() if evidence_readiness else None),
            "evidence_enrichment": enrichments[-1] if enrichments else None,
            "outline": outline_outcome.to_payload() if outline_outcome else None,
            "write": write_outcome.to_payload() if write_outcome else None,
            "quality": quality_outcome.to_payload() if quality_outcome else None,
            "quality_repair_history": repair_history,
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

    synthesis_bundles = (
        (await load_synthesis_bundles(context)).bundles if paper_type == "review" else []
    )

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
        sub_question_bundles=synthesis_bundles,
    )
    async with context.session() as session:
        outline = await create_outline(
            session,
            project_id=context.project_id,
            tree=outcome.tree,
        )
        outcome.outline_id = str(outline.id)
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
        # 已收尾的任务不复活：queued 期间被取消的任务，API 已经把它置成 cancelled，
        # 这里再写回 running 会让前端看到一个「取消了却还在跑」的任务。
        if job is not None and job.status not in FINISHED_JOB_STATUSES:
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
    # ``job.finished`` is committed before this status transition. Wake subscribers again so they
    # observe the terminal row and close immediately instead of waiting for the safety heartbeat.
    await context.notify_event()


async def _finish_needs_input(context: JobContext, report: Any) -> None:
    payload = {
        "status": "needs_input",
        "readiness_status": report.readiness_status,
        "blockers": report.blockers,
        "quality_report_id": report.report_id,
    }
    await context.emit(
        "job.needs_input",
        payload,
        stage="done",
        progress=1.0,
        checkpoint={"quality_blocked": payload},
    )
    if context.job_id is None:
        return
    from db.models.paper import GenerationJob

    async with context.session() as session:
        job = await session.get(GenerationJob, context.job_id)
        if job is not None:
            await update_job(session, job, status="needs_input", error=payload)
    await context.notify_event()


async def startup(ctx: dict) -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    engine = make_engine(settings.database_url, application_name="paperforge-worker")
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
        run_qdecomp_pipeline,
        run_evidence_pipeline,
        run_qmatrix_pipeline,
        run_alignment_pipeline,
        run_synthesis_pipeline,
        run_outline_pipeline,
        func(run_write_pipeline, timeout=LONG_RUNNING_PIPELINE_TIMEOUT_SECONDS),
        func(run_draft_rebuild_pipeline, timeout=LONG_RUNNING_PIPELINE_TIMEOUT_SECONDS),
        func(run_polish_pipeline, timeout=LONG_RUNNING_PIPELINE_TIMEOUT_SECONDS),
        run_export_pipeline,
        run_ingest_pipeline,
        run_pdf_match_pipeline,
        run_uploaded_pdf_pipeline,
        run_snowball_pipeline,
        run_quality_pipeline,
        func(run_quality_repair_pipeline, timeout=LONG_RUNNING_PIPELINE_TIMEOUT_SECONDS),
        run_visual_suggest_pipeline,
        run_visual_generate_pipeline,
        # 一键生成串起 scope→…→render 十个阶段；独立写作和润色虽然阶段更少，
        # 也同样按章节串行调用模型。实测 7 节 / 46 篇约 22 分钟，章节、修复轮次与
        # 文献规模叠加后会顶到默认超时。
        # 超时不是重试而是直接判失败（arq 用 asyncio.wait_for，抛的是 TimeoutError），
        # 所以宁可给宽，真挂住了还有 LLM 侧的单请求超时兜底。
        func(run_full_pipeline, timeout=LONG_RUNNING_PIPELINE_TIMEOUT_SECONDS),
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    # 论文生成是分钟级任务：给足超时，并限制并发以尊重 provider 配额。
    job_timeout = 3600
    max_jobs = 4
