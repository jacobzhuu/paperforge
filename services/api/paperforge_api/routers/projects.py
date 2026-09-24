"""项目 / SCOPE / 检索 / 文献库 / 任务 路由（设计 §4.7）。

未实现的端点一律返回 501，不得崩溃；已实现端点在依赖不可用（如 Redis）时返回 503。
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from arq.connections import ArqRedis
from db import (
    POLISH_DECISION_KEY,
    POLISH_JOB_ID_KEY,
    POLISH_SOURCE_JOB_KEY,
    QUALITY_REPAIR_DECISION_KEY,
    QUALITY_REPAIR_JOB_ID_KEY,
    QUALITY_REPAIR_SOURCE_JOB_KEY,
    WRITE_DOCUMENT_KEY,
    create_project,
    get_cards,
    get_entry,
    get_job,
    get_work_authors,
    get_writing_whitelist,
    job_resume_spec,
    latest_document,
    list_eligibility_decisions,
    list_entries,
    list_jobs,
    list_project_task_bindings,
    list_project_task_specs,
    list_projects,
    list_search_runs,
    list_task_definitions,
    parsed_asset_payloads,
    project_counters,
    project_llm_cost,
    replace_project_task_profile,
    request_job_stop,
    request_polish_skip,
    restore_project,
    resume_checkpoint,
    set_entry_status,
    soft_delete_project,
    update_job,
    update_project,
    update_project_scope,
)
from db.execution_profile import EXECUTION_PROFILE_KEY, source_execution_profile
from db.models.library import (
    DocumentFile,
    DocumentParse,
    EvidenceUnit,
    LibraryEntry,
    LiteratureCard,
    LiteraturePdfUpload,
    ScholarlyWork,
)
from db.models.paper import (
    CitationUsage,
    ExportArtifact,
    GenerationJob,
    PaperDocument,
    PaperProject,
    PaperSection,
    QualityReportRecord,
    QuestionEvidenceLink,
    ResearchQuestion,
    SearchRun,
    VisualAsset,
)
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from paperforge_api.deps import (
    CurrentUserDep,
    authorize_project_request,
    get_queue,
    get_session,
)
from paperforge_api.deps import get_authorized_project as _require_project
from paperforge_api.jobs import reconcile_abandoned_jobs, retry_profile_checkpoint, start_job
from paperforge_api.llm_accounting import accounted_runner
from paperforge_api.repair_response import RepairResponse, consume_response, validate_response
from paperforge_api.schemas import (
    CostResponse,
    CreateProjectRequest,
    EligibilityDecisionResponse,
    GenerateScopeRequest,
    ImportReferencesRequest,
    JobResponse,
    LibraryEntryResponse,
    LibraryUtilizationResponse,
    LiteratureCardResponse,
    ProjectResponse,
    ProjectTaskProfileResponse,
    ScholarlyWorkResponse,
    ScopeResponse,
    SearchRequest,
    SearchRunResponse,
    SelectEntriesRequest,
    SubmissionReadinessResponse,
    TaskDefinitionResponse,
    UpdateLibraryEntryRequest,
    UpdateProjectRequest,
    UpdateProjectTasksRequest,
    UpdateScopeRequest,
    WhitelistResponse,
)

router = APIRouter(
    prefix="/api/v1", tags=["projects"], dependencies=[Depends(authorize_project_request)]
)

SessionDep = Annotated[AsyncSession, Depends(get_session, scope="function")]
QueueDep = Annotated[ArqRedis | None, Depends(get_queue)]


@router.get("/projects/{project_id}/readiness", response_model=SubmissionReadinessResponse)
async def submission_readiness(project_id: str, session: SessionDep) -> SubmissionReadinessResponse:
    """统一 pass/warn/fail/unknown/stale 语义；未知与过期永不计为通过。"""
    from ingest.numlint import lint_sections

    project = await _require_project(session, project_id)
    document = await latest_document(session, project.id)
    sections = (
        []
        if document is None
        else list(
            (
                await session.scalars(
                    select(PaperSection)
                    .where(PaperSection.document_id == document.id)
                    .order_by(PaperSection.order_no)
                )
            ).all()
        )
    )
    now_href = f"/projects/{project.id}"
    checked_now = datetime.now(UTC)
    items: list[dict[str, Any]] = []

    whitelist = set(await get_writing_whitelist(session, project.id))
    used = set(
        (
            await session.scalars(
                select(CitationUsage.cite_key).where(CitationUsage.project_id == project.id)
            )
        ).all()
    )
    unauthorized = sorted(used - whitelist)
    items.append(
        {
            "key": "citations",
            "label": "引用真实性与白名单",
            "state": "unknown" if document is None else "fail" if unauthorized else "pass",
            "reason": "尚无正文，无法检查引用"
            if document is None
            else f"发现 {len(unauthorized)} 个越权引用"
            if unauthorized
            else f"已检查 {len(used)} 个引用键",
            "fix_href": f"{now_href}/write",
            "checked_at": checked_now,
        }
    )

    if project.paper_type == "original":
        lint = lint_sections(
            [
                {
                    "section_key": row.section_key,
                    "text": " ".join(
                        str(run.get("v") or "")
                        for block in (row.body_ir_json or {}).get("blocks", [])
                        for run in block.get("runs", [])
                        if run.get("t") == "text"
                    ),
                }
                for row in sections
            ],
            parsed_assets=await parsed_asset_payloads(session, project.id),
        )
        items.append(
            {
                "key": "numbers",
                "label": "数字与证据可追溯",
                "state": "unknown"
                if document is None
                else "fail"
                if lint.unsourced_count
                else "pass",
                "reason": "尚无正文，无法检查数字"
                if document is None
                else f"{lint.unsourced_count} 处数值无素材出处"
                if lint.unsourced_count
                else f"已核对 {lint.checked_count} 处数值",
                "fix_href": f"{now_href}/write",
                "checked_at": checked_now,
            }
        )

    retracted = int(
        await session.scalar(
            select(func.count())
            .select_from(LibraryEntry)
            .join(ScholarlyWork, ScholarlyWork.id == LibraryEntry.work_id)
            .where(
                LibraryEntry.project_id == project.id,
                LibraryEntry.status == "selected",
                ScholarlyWork.is_retracted.is_(True),
            )
        )
        or 0
    )
    items.append(
        {
            "key": "literature",
            "label": "撤稿与风险文献",
            "state": "fail" if retracted else "pass",
            "reason": f"发现 {retracted} 篇撤稿文献" if retracted else "未发现已标记撤稿的入库文献",
            "fix_href": f"{now_href}/library",
            "checked_at": checked_now,
        }
    )

    visuals = list(
        (
            await session.scalars(
                select(VisualAsset).where(
                    VisualAsset.project_id == project.id, VisualAsset.is_active.is_(True)
                )
            )
        ).all()
    )
    visual_failures = sum(row.generation_status == "failed" for row in visuals)
    visual_pending = sum(row.review_status == "pending" for row in visuals)
    items.append(
        {
            "key": "visuals",
            "label": "视觉完整性",
            "state": "fail" if visual_failures else "warn" if visual_pending else "pass",
            "reason": f"{visual_failures} 张图生成失败"
            if visual_failures
            else f"{visual_pending} 条建议待处理"
            if visual_pending
            else "视觉内容无待处理项",
            "fix_href": f"{now_href}/visuals",
            "checked_at": checked_now,
        }
    )

    metadata_ok = bool(
        project.metadata_confirmed_at
        and (project.publication_title or project.title)
        and (project.author_details_json or project.authors_json)
    )
    items.append(
        {
            "key": "metadata",
            "label": "投稿元数据",
            "state": "pass" if metadata_ok else "fail",
            "reason": "题名与作者信息已确认" if metadata_ok else "请确认投稿题名与作者信息",
            "fix_href": now_href,
            "checked_at": project.metadata_confirmed_at or checked_now,
        }
    )

    report = await session.scalar(
        select(QualityReportRecord)
        .where(QualityReportRecord.project_id == project.id)
        .order_by(QualityReportRecord.created_at.desc())
        .limit(1)
    )
    items.append(
        {
            "key": "quality",
            "label": "投稿质量门",
            "state": "unknown"
            if report is None
            else "stale"
            if report.stale
            else "fail"
            if report.blockers_json
            else "warn"
            if report.warnings_json
            else "pass",
            "reason": "尚未运行质量检查"
            if report is None
            else "报告已过期，请重新检查"
            if report.stale
            else (
                f"{len(report.blockers_json or [])} 个阻断项，"
                f"{len(report.warnings_json or [])} 个提醒"
            ),
            "fix_href": f"{now_href}/write",
            "checked_at": report.created_at if report else checked_now,
        }
    )

    pdf = await session.scalar(
        select(ExportArtifact)
        .where(ExportArtifact.project_id == project.id, ExportArtifact.format == "pdf")
        .order_by(ExportArtifact.created_at.desc())
        .limit(1)
    )
    manuscript_updated = max((row.updated_at for row in sections if row.updated_at), default=None)
    pdf_stale = bool(
        pdf and manuscript_updated and pdf.created_at and manuscript_updated > pdf.created_at
    )
    items.append(
        {
            "key": "export",
            "label": "PDF 与导出新鲜度",
            "state": "unknown" if pdf is None else "stale" if pdf_stale else "pass",
            "reason": "尚未生成 PDF"
            if pdf is None
            else "正文修改晚于最新 PDF"
            if pdf_stale
            else "最新 PDF 包含当前已保存正文",
            "fix_href": f"{now_href}/export",
            "checked_at": pdf.created_at if pdf else checked_now,
        }
    )

    states = {item["state"] for item in items}
    overall = (
        "fail"
        if "fail" in states
        else "unknown"
        if "unknown" in states
        else "stale"
        if "stale" in states
        else "warn"
        if "warn" in states
        else "pass"
    )
    checked = max((item["checked_at"] for item in items if item["checked_at"]), default=None)
    return SubmissionReadinessResponse(
        project_id=str(project.id), state=overall, checked_at=checked, items=items
    )


# ---- 项目 CRUD ----


@router.post("/projects", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
async def create_project_endpoint(
    request: CreateProjectRequest,
    session: SessionDep,
    user: CurrentUserDep,
) -> ProjectResponse:
    if {"authors", "author_details"} <= request.model_fields_set:
        raise HTTPException(status_code=422, detail="send authors or author_details, not both")
    try:
        project = await create_project(
            session,
            title=request.title,
            paper_type=request.paper_type,
            writing_mode=(
                "assisted"
                if request.intake is not None and "writing_mode" not in request.model_fields_set
                else request.writing_mode
            ),
            execution_profile=request.execution_profile,
            language=(
                request.intake.language
                or (request.language if "language" in request.model_fields_set else "zh")
            )
            if request.intake is not None
            else request.language,
            topic=request.topic,
            venue_template=request.venue_template
            or ("article" if request.intake is not None else None),
            citation_style=(
                "gbt7714"
                if request.intake is not None and "citation_style" not in request.model_fields_set
                else request.citation_style
            ),
            contribution_points=request.contribution_points,
            publication_title=request.publication_title,
            authors=request.authors,
            author_details=(
                [item.model_dump(mode="json") for item in request.author_details]
                if request.author_details is not None
                else None
            ),
            keywords=request.keywords,
            owner_id=user.id,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    if request.intake is not None:
        from db.intake import initial_intake

        project.scope_json = {
            **(project.scope_json or {}),
            "intake": initial_intake(request.intake.model_dump(exclude_none=True)),
        }
        await session.flush()
        await session.refresh(project)
    return _project_response(project, {"library_count": 0, "section_count": 0})

@router.get("/projects", response_model=list[ProjectResponse])
async def list_projects_endpoint(
    session: SessionDep,
    user: CurrentUserDep,
    queue: QueueDep,
    deleted: bool = Query(default=False, description="取回收站（只列已删除的项目）"),
) -> list[ProjectResponse]:
    projects = await list_projects(session, owner_id=user.id, deleted=deleted)
    if deleted or not projects:
        return [
            _project_response(project, {"library_count": 0, "section_count": 0})
            for project in projects
        ]
    rollups = await _project_attention_rollups(session, projects, queue)
    return [
        _project_response(project, rollups[project.id][0], rollups[project.id][1])
        for project in projects
    ]


@router.get("/projects/{project_id}", response_model=ProjectResponse)
async def get_project_endpoint(
    project_id: str, session: SessionDep, queue: QueueDep
) -> ProjectResponse:
    project = await _require_project(session, project_id)
    counters, summary = (await _project_attention_rollups(session, [project], queue))[project.id]
    return _project_response(project, counters, summary)


@router.patch("/projects/{project_id}", response_model=ProjectResponse)
async def update_project_endpoint(
    project_id: str,
    request: UpdateProjectRequest,
    session: SessionDep,
) -> ProjectResponse:
    """
    改项目元数据（题目 / 主题 / 模板 / 语言 / 引用样式 / 写作模式 / 贡献点）。

    此前项目建出来就再也改不了题目——创建向导是唯一的写入口。这在首页从
    「一句话研究意图」起步之后是硬伤：标题是从那句话推导出来的，不可改就等于
    把用户锁死在一个凑合的题目上（docs/ui-design.md §3.2）。

    只更新请求里**实际出现**的字段：`title=null` 与不传 title 在 JSON 里长得不一样，
    前者应当报 422（题目不能为空），后者应当不动。
    """
    project = await _require_project(session, project_id)
    await session.refresh(project, with_for_update=True)
    sent = request.model_fields_set
    if {"authors", "author_details"} <= sent:
        raise HTTPException(status_code=422, detail="send authors or author_details, not both")
    patch: dict[str, Any] = {
        name: (
            [item.model_dump(mode="json") for item in getattr(request, name)]
            if name == "author_details" and getattr(request, name) is not None
            else getattr(request, name)
        )
        for name in sent
        if hasattr(request, name)
    }
    try:
        await update_project(session, project, **patch)
    except (ValueError, TypeError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    counters = await project_counters(session, project.id)
    return _project_response(project, counters)


@router.delete("/projects/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project_endpoint(project_id: str, session: SessionDep) -> Response:
    """删除项目（软删除，可从回收站恢复）。

    项目里正在跑的任务会被一并取消——要求用户先手动取消再删除只是把一步拆成两步，
    而删除的意图本身已经说明了「这些都不要了」。worker 在下一个安全点看到停止标记
    后干净退出，不会留下一个对着已删项目继续烧 LLM 的任务。

    真正的行删除与对象存储回收由保留期后的 `paperforge-admin purge-projects` 执行：
    一次误删可能抹掉几小时的 LLM 花费，不给反悔的余地代价太大。
    """
    project = await _require_project(session, project_id)
    for job in await list_jobs(session, project.id):
        if job.status in {"queued", "running"}:
            await request_job_stop(session, job, mode="cancel")
            await update_job(session, job, status="cancelled")
            await _make_cancelled_pdf_retryable(session, job)
    await soft_delete_project(session, project)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/projects/{project_id}/restore", response_model=ProjectResponse)
async def restore_project_endpoint(project_id: str, session: SessionDep) -> ProjectResponse:
    """从回收站恢复项目。已被取消的任务不会自动重启，需要用户重新触发。"""
    project = await _require_project(session, project_id, include_deleted=True)
    await restore_project(session, project)
    counters = await project_counters(session, project.id)
    return _project_response(project, counters)


# ---- SCOPE ----


@router.get("/projects/{project_id}/scope", response_model=ScopeResponse)
async def get_scope(project_id: str, session: SessionDep) -> ScopeResponse:
    project = await _require_project(session, project_id)
    return ScopeResponse(project_id=str(project.id), scope=project.scope_json or {})


@router.put("/projects/{project_id}/scope", response_model=ScopeResponse)
async def put_scope(
    project_id: str,
    request: UpdateScopeRequest,
    session: SessionDep,
) -> ScopeResponse:
    """SCOPE 可编辑、可随时重生成（设计 §3.2：去掉协议锁定语义）。"""
    project = await _require_project(session, project_id)
    await session.refresh(project, with_for_update=True)
    if (project.scope_json or {}).get("intake", {}).get("status", "ready") != "ready":
        raise HTTPException(409, "请先完成研究方向理解或澄清")

    # 打上 generator='user'：SEARCH 会自动重生成确定性回退留下的降级 scope，
    # 手改过的必须豁免，否则用户调好的关键词会被下一次检索悄悄覆盖。
    scope = {**request.scope, "generator": "user"}
    # Clients may edit scope, never the protected intake state or pinned preferences.
    scope.pop("intake", None)
    if (project.scope_json or {}).get("intake"):
        scope["intake"] = project.scope_json["intake"]
    await update_project_scope(session, project, scope)
    return ScopeResponse(project_id=str(project.id), scope=project.scope_json or {})


@router.get("/tasks", response_model=list[TaskDefinitionResponse])
async def list_tasks(
    session: SessionDep,
    domain: str | None = None,
) -> list[TaskDefinitionResponse]:
    """任务本体目录（非项目作用域），供绑定选择器渲染。"""
    return [_task_response(spec) for spec in await list_task_definitions(session, domain=domain)]


@router.get("/projects/{project_id}/tasks", response_model=ProjectTaskProfileResponse)
async def get_project_tasks(
    project_id: str,
    session: SessionDep,
) -> ProjectTaskProfileResponse:
    """项目实际生效的任务集及其来源。"""
    from paperforge_api.config import get_settings

    project = await _require_project(session, project_id)
    settings = get_settings()
    bound = await list_project_task_bindings(session, project.id)
    effective = await list_project_task_specs(
        session,
        project.id,
        fallback=settings.task_profile_fallback,
    )
    if bound:
        source = "explicit" if _has_user_bound_marker(project) else "inferred"
        note = None
    else:
        source = "fallback"
        note = (
            "该项目未绑定任务，正在继承全部领域的指标与数据集白名单；"
            "绑定后抽取只会使用相关领域的术语。"
            if settings.task_profile_fallback == "all_tasks"
            else "该项目未绑定任务，按通用学术任务处理。"
        )
    return ProjectTaskProfileResponse(
        project_id=str(project.id),
        source=source,
        bound=bool(bound),
        task_ids=[row.task_id for row in bound],
        effective_tasks=[_task_response(spec) for spec in effective],
        fallback_mode=settings.task_profile_fallback,
        fallback_note=note,
    )


@router.put("/projects/{project_id}/tasks", response_model=ProjectTaskProfileResponse)
async def put_project_tasks(
    project_id: str,
    request: UpdateProjectTasksRequest,
    session: SessionDep,
) -> ProjectTaskProfileResponse:
    """显式绑定任务集；空列表解除绑定并回到回退行为。

    绑定会被打上用户标记，之后 QDECOMP 的自动推断不再覆盖它——与 SCOPE 的
    ``generator='user'`` 豁免、以及研究问题的 ``locked`` 是同一套约定。
    """
    project = await _require_project(session, project_id)
    requested = list(
        dict.fromkeys(task_id.strip() for task_id in request.task_ids if task_id.strip())
    )
    known = {spec.slug for spec in await list_task_definitions(session)}
    unknown = [task_id for task_id in requested if task_id not in known]
    if unknown:
        raise HTTPException(
            status_code=422,
            detail={"code": "unknown_task_ids", "task_ids": unknown},
        )
    await replace_project_task_profile(session, project_id=project.id, task_ids=requested)
    await _set_user_bound_marker(session, project, bound=bool(requested))
    return await get_project_tasks(project_id, session)


def _task_response(spec: Any) -> TaskDefinitionResponse:
    labels = spec.labels or {}
    return TaskDefinitionResponse(
        slug=spec.slug,
        domain=spec.domain,
        label=str(labels.get("zh") or labels.get("en") or spec.slug),
        metric_count=len(spec.metrics),
        dataset_count=len(spec.datasets),
        has_vocabulary=not spec.vocabulary.empty,
    )


#: 用户是否亲手定过任务集。存在 ``scope_json`` 里而不是新开一列：这只是一个布尔
#: 意图标记，``project_task_profile`` 的行本身仍是唯一的绑定真相。加一列会需要一次
#: 迁移，而迁移应当由 schema 的真实需要驱动，不是由一个标记驱动。
#: worker 侧在 ``pipelines.qdecomp`` 读同一个键。
TASK_PROFILE_USER_BOUND_KEY = "task_profile_user_bound"


def _has_user_bound_marker(project: Any) -> bool:
    return bool((project.scope_json or {}).get(TASK_PROFILE_USER_BOUND_KEY))


async def _set_user_bound_marker(session: AsyncSession, project: Any, *, bound: bool) -> None:
    scope = dict(project.scope_json or {})
    if bound:
        scope[TASK_PROFILE_USER_BOUND_KEY] = True
    else:
        scope.pop(TASK_PROFILE_USER_BOUND_KEY, None)
    await update_project_scope(session, project, scope)


@router.post(
    "/projects/{project_id}/scope/generate",
    response_model=ScopeResponse,
    status_code=status.HTTP_200_OK,
)
async def generate_scope_endpoint(
    project_id: str,
    request: GenerateScopeRequest,
    session: SessionDep,
) -> ScopeResponse:
    """同步生成 SCOPE：planner 角色调用 + 确定性回退，秒级返回。"""
    from paperforge_worker.pipelines.scope import generate_scope as run_generate_scope

    from paperforge_api.config import get_settings

    project = await _require_project(session, project_id)
    await session.refresh(project, with_for_update=True)
    if (project.scope_json or {}).get("intake", {}).get("status", "ready") != "ready":
        raise HTTPException(409, "请先完成研究方向理解或澄清")

    topic = (request.topic or (project.scope_json or {}).get("topic") or project.title).strip()
    async with accounted_runner(
        session, get_settings().llm_config(), project_id=project.id
    ) as runner:
        scope = await run_generate_scope(
            topic,
            language=project.language,
            paper_type=project.paper_type,
            runner=runner,
        )
        merged = {**(project.scope_json or {}), **scope}
        await update_project_scope(session, project, merged)
        return ScopeResponse(project_id=str(project.id), scope=merged)


# ---- 检索与导入（异步任务） ----


@router.post(
    "/projects/{project_id}/search/runs",
    response_model=JobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_search(
    project_id: str,
    request: SearchRequest,
    session: SessionDep,
    queue: QueueDep,
    retry_of: str | None = None,
) -> JobResponse:
    project = await _require_project(session, project_id)
    job = await start_job(
        session,
        queue,
        project_id=project.id,
        kind="search",
        function="run_library_pipeline",
        checkpoint=await retry_profile_checkpoint(session, project.id, retry_of),
        providers=request.providers,
        regenerate_scope=request.regenerate_scope,
    )
    return _job_response(job)


@router.get("/projects/{project_id}/search/runs", response_model=list[SearchRunResponse])
async def get_search_runs(project_id: str, session: SessionDep) -> list[SearchRunResponse]:
    project = await _require_project(session, project_id)
    runs = await list_search_runs(session, project.id)
    return [_search_run_response(run) for run in runs]


@router.post(
    "/projects/{project_id}/library/import",
    response_model=JobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def import_references_endpoint(
    project_id: str,
    request: ImportReferencesRequest,
    session: SessionDep,
    queue: QueueDep,
) -> JobResponse:
    """DOI / BibTeX 导入：入库前必须经 R1 反查核验（设计 §4.4.3）。"""
    project = await _require_project(session, project_id)
    if not request.dois and not (request.bibtex or "").strip():
        raise HTTPException(status_code=422, detail="provide dois or bibtex to import")
    job = await start_job(
        session,
        queue,
        project_id=project.id,
        kind="search",
        function="run_import_pipeline",
        dois=request.dois,
        bibtex=request.bibtex,
    )
    return _job_response(job)


@router.post(
    "/projects/{project_id}/cards/generate",
    response_model=JobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def generate_cards_endpoint(
    project_id: str,
    session: SessionDep,
    queue: QueueDep,
    retry_of: str | None = None,
) -> JobResponse:
    project = await _require_project(session, project_id)
    job = await start_job(
        session,
        queue,
        project_id=project.id,
        kind="cards",
        function="run_cards_pipeline",
        checkpoint=await retry_profile_checkpoint(session, project.id, retry_of),
    )
    return _job_response(job)


# ---- 文献库 ----


@router.get("/projects/{project_id}/library", response_model=list[LibraryEntryResponse])
async def get_library(
    project_id: str,
    session: SessionDep,
    entry_status: Annotated[str | None, Query(alias="status")] = None,
) -> list[LibraryEntryResponse]:
    project = await _require_project(session, project_id)
    try:
        rows = await list_entries(session, project.id, status=entry_status)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    cards = await get_cards(session, project.id)
    utilization = await _library_utilization_map(
        session,
        project.id,
        rows=rows,
        cards=cards,
    )
    responses: list[LibraryEntryResponse] = []
    for entry, work in rows:
        authors = await get_work_authors(session, work.id)
        responses.append(
            _entry_response(
                entry,
                work,
                [a["author_name"] for a in authors],
                cards.get(work.id),
                utilization.get(work.id),
            )
        )
    return responses


@router.get(
    "/projects/{project_id}/library/eligibility-decisions",
    response_model=list[EligibilityDecisionResponse],
)
async def get_eligibility_decisions(
    project_id: str,
    session: SessionDep,
    decision: Annotated[str | None, Query()] = None,
) -> list[EligibilityDecisionResponse]:
    """SCREEN 为什么留下/排除/存疑某篇文献——诊断筛选偏差的唯一入口。"""
    project = await _require_project(session, project_id)
    try:
        rows = await list_eligibility_decisions(session, project.id, decision=decision)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return [
        EligibilityDecisionResponse(
            work_id=str(work.id),
            title=work.canonical_title,
            decision=row.decision,  # type: ignore[arg-type]
            reason=row.reason,
            anchor_facet_hit=row.anchor_facet_hit,
            criterion_hits=row.criterion_hits_json or {},
            decided_by=row.decided_by,
            model=row.model,
        )
        for row, work in rows
    ]


@router.post("/projects/{project_id}/library/entries", response_model=list[LibraryEntryResponse])
async def select_entries(
    project_id: str,
    request: SelectEntriesRequest,
    session: SessionDep,
) -> list[LibraryEntryResponse]:
    """圈选入库/排除。

    R1：只能在**已核验的候选**之间切换状态——这里不接受任何新文献线索，
    新增文献必须走 /library/import 的反查路径。
    """
    project = await _require_project(session, project_id)
    updated_rows: list[tuple[LibraryEntry, ScholarlyWork]] = []
    for raw_work_id in request.work_ids:
        try:
            work_id = uuid.UUID(raw_work_id)
        except ValueError as error:
            raise HTTPException(
                status_code=422, detail=f"invalid work id: {raw_work_id}"
            ) from error
        entry = await get_entry(session, project_id=project.id, work_id=work_id)
        if entry is None:
            raise HTTPException(
                status_code=404,
                detail=f"work {raw_work_id} is not a verified candidate in this project",
            )
        # An explicit user admission must survive the automatic SCREEN top-K
        # budget. Deselecting/excluding clears that pin again.
        await set_entry_status(
            session,
            entry,
            request.status,
            user_pinned=request.status == "selected",
        )
        work = await session.get(ScholarlyWork, work_id)
        if work is not None:
            updated_rows.append((entry, work))
    if request.status == "selected":
        await _assign_keys_for_selected(session, project.id)
    cards = await get_cards(session, project.id)
    utilization = await _library_utilization_map(
        session,
        project.id,
        rows=updated_rows,
        cards=cards,
    )
    updated: list[LibraryEntryResponse] = []
    for entry, work in updated_rows:
        authors = await get_work_authors(session, work.id)
        updated.append(
            _entry_response(
                entry,
                work,
                [a["author_name"] for a in authors],
                cards.get(work.id),
                utilization.get(work.id),
            )
        )
    return updated


@router.patch(
    "/projects/{project_id}/library/entries/{entry_id}",
    response_model=LibraryEntryResponse,
)
async def update_library_entry(
    project_id: str,
    entry_id: str,
    request: UpdateLibraryEntryRequest,
    session: SessionDep,
) -> LibraryEntryResponse:
    """Update a lightweight literature role without changing inclusion status."""
    project = await _require_project(session, project_id)
    try:
        entry_uuid = uuid.UUID(entry_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail="library entry not found") from error
    entry = await session.get(LibraryEntry, entry_uuid)
    if entry is None or entry.project_id != project.id:
        raise HTTPException(status_code=404, detail="library entry not found")
    entry.literature_role = request.literature_role
    # Keep the historical prioritization signal aligned for SCREEN/ranking.
    entry.user_pinned = request.literature_role == "core"
    await session.flush()
    work = await session.get(ScholarlyWork, entry.work_id)
    if work is None:
        raise HTTPException(status_code=404, detail="scholarly work not found")
    cards = await get_cards(session, project.id)
    rows = [(entry, work)]
    utilization = await _library_utilization_map(session, project.id, rows=rows, cards=cards)
    authors = await get_work_authors(session, work.id)
    return _entry_response(
        entry,
        work,
        [item["author_name"] for item in authors],
        cards.get(work.id),
        utilization.get(work.id),
    )


@router.delete(
    "/projects/{project_id}/library/entries/{entry_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_library_entry(
    project_id: str,
    entry_id: str,
    session: SessionDep,
) -> None:
    from db import delete_entry

    project = await _require_project(session, project_id)
    try:
        entry_uuid = uuid.UUID(entry_id)
    except ValueError as error:
        raise HTTPException(status_code=422, detail="invalid entry id") from error
    entry = await session.get(LibraryEntry, entry_uuid)
    if entry is None or entry.project_id != project.id:
        raise HTTPException(status_code=404, detail="library entry not found")
    await delete_entry(session, entry)


@router.get("/projects/{project_id}/library/whitelist", response_model=WhitelistResponse)
async def get_whitelist(project_id: str, session: SessionDep) -> WhitelistResponse:
    """R1 写作白名单：前端引用 chip 只能从这里取值。"""
    project = await _require_project(session, project_id)
    whitelist = await get_writing_whitelist(session, project.id)
    return WhitelistResponse(project_id=str(project.id), cite_keys=sorted(whitelist))


# ---- 任务 ----


@router.get("/projects/{project_id}/jobs", response_model=list[JobResponse])
async def get_jobs(project_id: str, session: SessionDep, queue: QueueDep) -> list[JobResponse]:
    project = await _require_project(session, project_id)
    jobs = await list_jobs(session, project.id)
    # 读一次就顺手收一次尸：被硬杀掉的任务没有任何代码能替它收尾，只有来看它的人
    # 才有机会发现「这条已经没人在跑了」。
    await reconcile_abandoned_jobs(session, queue, jobs)
    return [_job_response(job) for job in jobs]


@router.get("/projects/{project_id}/jobs/{job_id}", response_model=JobResponse)
async def get_job_endpoint(
    project_id: str, job_id: str, session: SessionDep, queue: QueueDep
) -> JobResponse:
    await _require_project(session, project_id)
    job = await _require_job(session, job_id)
    await reconcile_abandoned_jobs(session, queue, [job])
    return _job_response(job)


@router.post("/projects/{project_id}/jobs/{job_id}/polish/skip", response_model=JobResponse)
async def skip_polish(project_id: str, job_id: str, session: SessionDep) -> JobResponse:
    """跳过剩余的连贯性润色。

    润色是每节一次 LLM 调用、整体十几分钟的收尾工序，稿子在此之前就已经完整落库。
    要不要等它，应该由用户当场决定，而不是只能干等或者把整个任务杀掉——
    已润色的章节保留，剩下的直接交付初稿。
    """
    job = await _require_project_job(session, project_id, job_id)
    if job.status in {"queued", "running"}:
        await request_polish_skip(session, job)
        return _job_response(job)
    decision = (job.checkpoint_json or {}).get(POLISH_DECISION_KEY)
    if job.kind == "full" and job.status == "succeeded" and decision == "pending":
        await update_job(session, job, checkpoint={POLISH_DECISION_KEY: "skipped"})
        return _job_response(job)
    raise HTTPException(status_code=409, detail="job has no pending polish decision")


@router.post(
    "/projects/{project_id}/jobs/{job_id}/polish",
    response_model=JobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_polish(
    project_id: str,
    job_id: str,
    session: SessionDep,
    queue: QueueDep,
) -> JobResponse:
    """首轮全流程完成后，由用户显式启动独立润色任务。

    润色复用首稿 document，不会重跑检索、大纲或正文起草；完成后 worker 会刷新
    质量报告与导出件。一个 pending 决策只能消费一次，避免双击产生两条付费任务。
    """
    source = await _require_project_job(session, project_id, job_id)
    decision = (source.checkpoint_json or {}).get(POLISH_DECISION_KEY)
    if (
        source.kind != "full"
        or source.status != "succeeded"
        or decision
        not in {
            "pending",
            "failed",
        }
    ):
        raise HTTPException(status_code=409, detail="job has no pending polish decision")
    document = await latest_document(session, source.project_id)
    if document is None:
        raise HTTPException(status_code=409, detail="paper document not found")
    spec = job_resume_spec(source) or {"kwargs": {}}
    options = spec.get("kwargs") or {}
    polished = await start_job(
        session,
        queue,
        project_id=source.project_id,
        kind="write",
        function="run_polish_pipeline",
        checkpoint={
            WRITE_DOCUMENT_KEY: str(document.id),
            POLISH_SOURCE_JOB_KEY: str(source.id),
            EXECUTION_PROFILE_KEY: source_execution_profile(source),
        },
        source_job_id=str(source.id),
        quality_profile=str(options.get("quality_profile") or "scholarly"),
        review_style=str(options.get("review_style") or "narrative"),
    )
    await update_job(
        session,
        source,
        checkpoint={
            POLISH_DECISION_KEY: "started",
            POLISH_JOB_ID_KEY: str(polished.id),
        },
    )
    return _job_response(polished)


@router.post(
    "/projects/{project_id}/jobs/{job_id}/quality-repair/skip",
    response_model=JobResponse,
)
async def skip_quality_repair(project_id: str, job_id: str, session: SessionDep) -> JobResponse:
    """明确放弃这一轮质量修复。

    不记录这个决定，概览页每次刷新都会重新邀请一遍；用户看过问题清单后决定
    「就这样先用着」是一个有效答案，不该被反复追问。
    """
    job = await _require_project_job(session, project_id, job_id)
    decision = (job.checkpoint_json or {}).get(QUALITY_REPAIR_DECISION_KEY)
    if job.kind == "full" and decision in {"pending", "failed"}:
        await update_job(session, job, checkpoint={QUALITY_REPAIR_DECISION_KEY: "skipped"})
        return _job_response(job)
    raise HTTPException(status_code=409, detail="job has no pending quality repair decision")


@router.post(
    "/projects/{project_id}/jobs/{job_id}/quality-repair",
    response_model=JobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_quality_repair_from_job(
    project_id: str,
    job_id: str,
    session: SessionDep,
    queue: QueueDep,
) -> JobResponse:
    """全流程交付之后，由用户显式启动一轮质量修复。

    档位固定 scholarly：全流程用 draft 跑完，发现项都还挂在 warnings 上，只有
    按学术严谨档重评它们才会变成阻断项、进而驱动收敛器去重写对应章节。用 draft
    起这条任务等于跑一遍评估然后什么都不改。一个 pending 决策只消费一次，
    避免双击起两条付费任务。
    """
    source = await _require_project_job(session, project_id, job_id)
    decision = (source.checkpoint_json or {}).get(QUALITY_REPAIR_DECISION_KEY)
    if (
        source.kind != "full"
        or source.status != "succeeded"
        or decision not in {"pending", "failed"}
    ):
        raise HTTPException(status_code=409, detail="job has no pending quality repair decision")
    document = await latest_document(session, source.project_id)
    if document is None:
        raise HTTPException(status_code=409, detail="paper document not found")
    spec = job_resume_spec(source) or {"kwargs": {}}
    options = spec.get("kwargs") or {}
    repair = await start_job(
        session,
        queue,
        project_id=source.project_id,
        kind="write",
        function="run_quality_repair_pipeline",
        checkpoint={
            QUALITY_REPAIR_SOURCE_JOB_KEY: str(source.id),
            EXECUTION_PROFILE_KEY: source_execution_profile(source),
        },
        source_job_id=str(source.id),
        quality_profile="scholarly",
        review_style=str(options.get("review_style") or "narrative"),
    )
    await update_job(
        session,
        source,
        checkpoint={
            QUALITY_REPAIR_DECISION_KEY: "started",
            QUALITY_REPAIR_JOB_ID_KEY: str(repair.id),
        },
    )
    return _job_response(repair)


@router.post("/projects/{project_id}/jobs/{job_id}/cancel", response_model=JobResponse)
async def cancel_job(project_id: str, job_id: str, session: SessionDep) -> JobResponse:
    """取消任务：已经产出的内容全部保留，只是这一轮不再往下跑。

    协作式停止——worker 跑完当前这一步（阶段/章节/条目）才会看到标记。真正的
    进程级中断做不到：LLM 调用都在 asyncio.to_thread 里，掐掉 await 点也停不掉
    底层线程，只会把已经付过钱的那次调用白白丢掉。
    """
    job = await _require_project_job(session, project_id, job_id)
    if job.status not in {"queued", "running"}:
        raise HTTPException(status_code=409, detail="job already finished")
    await request_job_stop(session, job, mode="cancel")
    if job.status == "queued":
        # 还没被 worker 捡走：立刻落终态，用户不用盯着一个「排队中」的僵尸任务。
        # worker 万一同时捡走了，它在 job_context 的入口检查里会立刻退出。
        await update_job(session, job, status="cancelled")
        await _make_cancelled_pdf_retryable(session, job)
    return _job_response(job)


@router.post("/projects/{project_id}/jobs/{job_id}/pause", response_model=JobResponse)
async def pause_job(project_id: str, job_id: str, session: SessionDep) -> JobResponse:
    """暂停任务：停在最近的安全点，断点与已产出内容都保留，可用 resume 接着跑。"""
    job = await _require_project_job(session, project_id, job_id)
    if job.status != "running":
        raise HTTPException(status_code=409, detail="only a running job can be paused")
    if _pdf_upload_job_target(job) is not None:
        # PDF stages are deliberately atomic and do not expose resumable
        # checkpoints. Cancellation moves the upload to a retryable state;
        # pretending they can pause would strand it in matching/extracting.
        raise HTTPException(
            status_code=409,
            detail="PDF jobs cannot be paused; cancel the job and retry the upload",
        )
    await request_job_stop(session, job, mode="pause")
    return _job_response(job)


@router.post(
    "/projects/{project_id}/jobs/{job_id}/resume",
    response_model=JobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def resume_job(
    project_id: str,
    job_id: str,
    session: SessionDep,
    queue: QueueDep,
    response: RepairResponse | None = None,
) -> JobResponse:
    """从断点继续：新建一个 job，播种上一轮的 checkpoint，已完成的阶段直接跳过。

    不复用原 job：它已经收过尾（paused 是终态之一），事件序列也已经闭合；
    续跑另起一条，前端的进度流和历史记录才对得上。
    """
    job = await _require_project_job(session, project_id, job_id)
    if job.kind == "research":
        raise HTTPException(409, "请在素材中心确认分析口径后继续研究任务")
    # Lock the source through dispatch so repeated submissions cannot fork a resume chain.
    await session.refresh(job, with_for_update=True)
    response_checkpoint = await validate_response(session, job, response)
    if job.status != "paused":
        raise HTTPException(status_code=409, detail="only a paused job can be resumed")
    spec = job_resume_spec(job)
    if spec is None:
        # 暂停功能上线前建的任务没有重放信息，只能请用户重新触发。
        raise HTTPException(status_code=409, detail="job cannot be resumed: no resume spec")
    if _pdf_upload_job_target(job) is not None:
        raise HTTPException(
            status_code=409,
            detail="PDF jobs cannot be resumed; retry the upload instead",
        )
    resumed = await start_job(
        session,
        queue,
        project_id=job.project_id,
        kind=job.kind,
        function=spec["function"],
        checkpoint={**resume_checkpoint(job), **response_checkpoint},
        **spec["kwargs"],
    )
    await consume_response(session, job, resumed)
    return _job_response(resumed)


@router.get("/projects/{project_id}/cost", response_model=CostResponse)
async def get_cost(project_id: str, session: SessionDep) -> CostResponse:
    from paperforge_api.config import get_settings

    project = await _require_project(session, project_id)
    totals = await project_llm_cost(session, project.id)
    return CostResponse(
        project_id=str(project.id),
        currency=get_settings().llm_price_currency,
        **totals,
    )


# ---- 内部工具 ----


async def _require_job(session: AsyncSession, job_id: str) -> GenerationJob:
    try:
        job_uuid = uuid.UUID(job_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail="job not found") from error
    job = await get_job(session, job_uuid)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job


def _pdf_upload_job_target(
    job: GenerationJob,
) -> tuple[uuid.UUID, frozenset[str], str] | None:
    """Return the upload and CAS transition for one of the two PDF jobs."""
    spec = job_resume_spec(job)
    if spec is None:
        return None
    transitions = {
        "run_pdf_match_pipeline": (frozenset({"matching"}), "match_failed"),
        "match_literature_pdf": (frozenset({"matching"}), "match_failed"),
        "run_uploaded_pdf_pipeline": (
            frozenset({"parsing", "extracting"}),
            "parse_failed",
        ),
        "parse_literature_pdf": (
            frozenset({"parsing", "extracting"}),
            "parse_failed",
        ),
    }
    transition = transitions.get(spec["function"])
    raw_upload_id = spec["kwargs"].get("upload_id")
    if transition is None or not isinstance(raw_upload_id, str):
        return None
    try:
        upload_id = uuid.UUID(raw_upload_id)
    except ValueError:
        return None
    return upload_id, transition[0], transition[1]


async def _make_cancelled_pdf_retryable(
    session: AsyncSession,
    job: GenerationJob,
) -> None:
    """CAS an inactive PDF job's live upload into a user-retryable state."""
    target = _pdf_upload_job_target(job)
    if target is None:
        return
    upload_id, active_statuses, failure_status = target
    upload = await session.scalar(
        select(LiteraturePdfUpload)
        .where(
            LiteraturePdfUpload.id == upload_id,
            LiteraturePdfUpload.project_id == job.project_id,
            LiteraturePdfUpload.status.in_(active_statuses),
        )
        .with_for_update()
    )
    if upload is None:
        return
    upload.status = failure_status
    upload.error_json = {
        "reason": "job_cancelled",
        "job_id": str(job.id),
    }


async def _require_project_job(
    session: AsyncSession, project_id: str, job_id: str
) -> GenerationJob:
    """取本项目下的任务。跨项目的 job id 一律 404，不泄露它是否存在。"""
    project = await _require_project(session, project_id)
    job = await _require_job(session, job_id)
    if job.project_id != project.id:
        raise HTTPException(status_code=404, detail="job not found")
    return job


async def _assign_keys_for_selected(session: AsyncSession, project_id: uuid.UUID) -> None:
    """R3：勾选入库即分配并持久化 bibtex_key（渲染期只消费）。"""
    from db import assign_bibtex_key, reference_metadata_payload
    from paper_ir import ReferenceMetadata, make_bibtex_key

    rows = await list_entries(session, project_id, status="selected")
    for entry, work in rows:
        if entry.bibtex_key or entry.verified_at is None or work.is_retracted:
            continue
        payload = await reference_metadata_payload(session, work)
        await assign_bibtex_key(
            session,
            entry,
            make_bibtex_key,
            reference=ReferenceMetadata(**payload),
        )


async def _project_attention_rollups(
    session: AsyncSession,
    projects: list[PaperProject],
    queue: ArqRedis | None = None,
) -> dict[uuid.UUID, tuple[dict[str, int], dict[str, Any]]]:
    """固定数量批量查询，项目数增加时不产生逐卡 N+1。"""
    ids = [project.id for project in projects]
    library_counts = dict(
        (
            await session.execute(
                select(LibraryEntry.project_id, func.count())
                .where(LibraryEntry.project_id.in_(ids), LibraryEntry.status == "selected")
                .group_by(LibraryEntry.project_id)
            )
        ).all()
    )

    latest_versions = (
        select(PaperDocument.project_id, func.max(PaperDocument.version).label("version"))
        .where(PaperDocument.project_id.in_(ids))
        .group_by(PaperDocument.project_id)
        .subquery()
    )
    section_rows = (
        await session.execute(
            select(PaperDocument.project_id, PaperSection)
            .join(
                latest_versions,
                (latest_versions.c.project_id == PaperDocument.project_id)
                & (latest_versions.c.version == PaperDocument.version),
            )
            .join(PaperSection, PaperSection.document_id == PaperDocument.id)
        )
    ).all()
    sections: dict[uuid.UUID, list[PaperSection]] = {project_id: [] for project_id in ids}
    for project_id, section in section_rows:
        sections[project_id].append(section)

    job_rows = list(
        (
            await session.scalars(
                select(GenerationJob)
                .where(GenerationJob.project_id.in_(ids))
                .order_by(GenerationJob.created_at.desc())
            )
        ).all()
    )
    # 概览卡上的「进行中」就是从这批行里挑的：不先收尸，一条被硬杀掉的任务会在
    # 项目列表上一直转圈。
    await reconcile_abandoned_jobs(session, queue, job_rows)
    quality_rows = list(
        (
            await session.scalars(
                select(QualityReportRecord)
                .where(QualityReportRecord.project_id.in_(ids))
                .order_by(QualityReportRecord.created_at.desc())
            )
        ).all()
    )
    pdf_rows = list(
        (
            await session.scalars(
                select(ExportArtifact)
                .where(ExportArtifact.project_id.in_(ids), ExportArtifact.format == "pdf")
                .order_by(ExportArtifact.created_at.desc())
            )
        ).all()
    )

    result: dict[uuid.UUID, tuple[dict[str, int], dict[str, Any]]] = {}
    for project in projects:
        manuscript = sections[project.id]
        word_count = sum(_section_word_count(section.body_ir_json or {}) for section in manuscript)
        manuscript_updated = max(
            (section.updated_at for section in manuscript if section.updated_at), default=None
        )
        active = next(
            (
                row
                for row in job_rows
                if row.project_id == project.id
                and row.status in {"queued", "running", "needs_input"}
            ),
            None,
        )
        report = next((row for row in quality_rows if row.project_id == project.id), None)
        pdf = next((row for row in pdf_rows if row.project_id == project.id), None)

        if report is None:
            readiness_state = "unknown"
            attention_count = None
            next_action = "运行投稿质量检查" if manuscript else "继续生成正文"
            href = f"/projects/{project.id}/write"
            checked_at = None
        else:
            blockers = report.blockers_json or []
            warnings = report.warnings_json or []
            readiness_state = (
                "stale"
                if report.stale
                else ("fail" if blockers else "warn" if warnings else "pass")
            )
            attention_count = len(blockers) + len(warnings)
            next_action = (
                "重新检查投稿就绪度"
                if report.stale
                else (
                    "修复投稿阻断项" if blockers else "查看投稿提醒" if warnings else "生成投稿文件"
                )
            )
            href = (
                f"/projects/{project.id}/write"
                if blockers or warnings or report.stale
                else f"/projects/{project.id}/export"
            )
            checked_at = report.created_at

        checkpoint = (
            active.checkpoint_json if active and isinstance(active.checkpoint_json, dict) else {}
        )
        result[project.id] = (
            {
                "library_count": int(library_counts.get(project.id, 0)),
                "section_count": len(manuscript),
            },
            {
                "manuscript": {
                    "sectionCount": len(manuscript),
                    "wordCount": word_count if manuscript else None,
                    "updatedAt": manuscript_updated,
                },
                "active_job": (
                    {
                        "stage": active.stage or active.kind,
                        "completed": checkpoint.get("completed"),
                        "total": checkpoint.get("total"),
                    }
                    if active
                    else None
                ),
                "readiness": {
                    "state": readiness_state,
                    "attentionCount": attention_count,
                    "nextAction": next_action,
                    "href": href,
                    "checkedAt": checked_at,
                },
                "latest_pdf": (
                    {
                        "createdAt": pdf.created_at,
                        "stale": bool(
                            manuscript_updated
                            and pdf.created_at
                            and manuscript_updated > pdf.created_at
                        ),
                    }
                    if pdf
                    else None
                ),
            },
        )
    return result


def _section_word_count(body: dict[str, Any]) -> int:
    text = " ".join(
        str(run.get("v") or "")
        for block in body.get("blocks", [])
        for run in block.get("runs", [])
        if run.get("t") == "text"
    )
    return len(re.findall(r"[一-鿿]", text)) + len(re.findall(r"[A-Za-z][A-Za-z'-]*", text))


def _project_response(
    project: PaperProject,
    counters: dict[str, int],
    attention_summary: dict[str, Any] | None = None,
) -> ProjectResponse:
    scope = project.scope_json or {}
    return ProjectResponse(
        id=str(project.id),
        intake={k: v for k, v in scope["intake"].items()
                if k in {"version", "status", "paper_type", "language", "summary", "next_step"}}
        if scope.get("intake") else None,
        title=project.title,
        paper_type=project.paper_type,
        writing_mode=project.writing_mode,
        execution_profile=project.execution_profile,
        language=project.language,
        status=project.status,
        venue_template=project.venue_template,
        citation_style=project.citation_style,
        topic=scope.get("topic"),
        contribution_points=scope.get("contribution_points") or [],
        publication_title=project.publication_title,
        authors=project.authors_json or [],
        author_details=getattr(project, "author_details_json", None)
        or [
            {
                "id": f"legacy-{index + 1}",
                "name": name,
                "affiliations": [],
                "corresponding": False,
            }
            for index, name in enumerate(project.authors_json or [])
        ],
        keywords=project.keywords_json or [],
        metadata_confirmed=project.metadata_confirmed_at is not None,
        web_research_enabled=project.web_research_enabled,
        library_count=counters.get("library_count", 0),
        section_count=counters.get("section_count", 0),
        created_at=project.created_at,
        updated_at=project.updated_at,
        deleted_at=project.deleted_at,
        attention_summary=attention_summary,
    )


def _job_response(job: GenerationJob) -> JobResponse:
    return JobResponse(
        id=str(job.id),
        project_id=str(job.project_id),
        kind=job.kind,
        status=job.status,
        stage=job.stage,
        progress=job.progress,
        checkpoint=job.checkpoint_json,
        error=job.error_json,
        created_at=job.created_at,
        finished_at=job.finished_at,
    )


def _search_run_response(run: SearchRun) -> SearchRunResponse:
    return SearchRunResponse(
        id=str(run.id),
        provider=run.provider,
        query_text=run.query_text,
        hit_count=run.hit_count or 0,
        retrieved_count=run.retrieved_count or 0,
        status=run.status or "running",
        executed_at=run.executed_at,
        error=run.error,
    )


async def _library_utilization_map(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    rows: list[tuple[LibraryEntry, ScholarlyWork]],
    cards: dict[uuid.UUID, LiteratureCard],
) -> dict[uuid.UUID, LibraryUtilizationResponse]:
    """Aggregate the full-text → evidence → assignment → citation chain.

    Private documents/evidence are visible only to their owning project. Shared
    OA rows remain reusable across projects.
    """
    if not rows:
        return {}
    work_ids = {work.id for _entry, work in rows}
    document_rows = (
        await session.execute(
            select(
                DocumentFile.work_id,
                DocumentFile.access_scope,
                DocumentParse.status,
            )
            .outerjoin(DocumentParse, DocumentParse.document_file_id == DocumentFile.id)
            .where(
                DocumentFile.work_id.in_(work_ids),
                or_(
                    DocumentFile.access_scope == "shared",
                    DocumentFile.project_id == project_id,
                ),
            )
            .order_by(DocumentFile.created_at.desc())
        )
    ).all()
    documents: dict[uuid.UUID, list[tuple[str, str | None]]] = {}
    for work_id, access_scope, parse_status in document_rows:
        documents.setdefault(work_id, []).append((access_scope or "shared", parse_status))

    evidence_counts = {
        work_id: int(count)
        for work_id, count in (
            await session.execute(
                select(EvidenceUnit.work_id, func.count(EvidenceUnit.id))
                .where(
                    EvidenceUnit.work_id.in_(work_ids),
                    or_(
                        EvidenceUnit.project_id.is_(None),
                        EvidenceUnit.project_id == project_id,
                    ),
                )
                .group_by(EvidenceUnit.work_id)
            )
        ).all()
    }
    assignment_counts = {
        work_id: int(count)
        for work_id, count in (
            await session.execute(
                select(EvidenceUnit.work_id, func.count(func.distinct(QuestionEvidenceLink.id)))
                .join(
                    QuestionEvidenceLink,
                    QuestionEvidenceLink.evidence_unit_id == EvidenceUnit.id,
                )
                .join(
                    ResearchQuestion,
                    ResearchQuestion.id == QuestionEvidenceLink.research_question_id,
                )
                .where(
                    EvidenceUnit.work_id.in_(work_ids),
                    ResearchQuestion.project_id == project_id,
                    or_(
                        EvidenceUnit.project_id.is_(None),
                        EvidenceUnit.project_id == project_id,
                    ),
                )
                .group_by(EvidenceUnit.work_id)
            )
        ).all()
    }
    current_document = await latest_document(session, project_id)
    citation_counts = (
        {
            work_id: int(count)
            for work_id, count in (
                await session.execute(
                    select(CitationUsage.work_id, func.count(CitationUsage.id))
                    .join(PaperSection, PaperSection.id == CitationUsage.section_id)
                    .where(
                        CitationUsage.project_id == project_id,
                        CitationUsage.work_id.in_(work_ids),
                        PaperSection.document_id == current_document.id,
                    )
                    .group_by(CitationUsage.work_id)
                )
            ).all()
        }
        if current_document is not None
        else {}
    )
    usage_evaluated = current_document is not None

    result: dict[uuid.UUID, LibraryUtilizationResponse] = {}
    for entry, work in rows:
        source_rows = documents.get(work.id, [])
        parsed_rows = [item for item in source_rows if item[1] == "parsed"]
        pending_rows = [item for item in source_rows if item[1] not in {"parsed", "failed"}]
        failed_rows = [item for item in source_rows if item[1] == "failed"]
        fulltext_status: Literal["available", "parsing", "failed", "abstract_only", "unavailable"]
        fulltext_source: Literal["user_pdf", "oa", "none"]
        if parsed_rows:
            fulltext_status = "available"
            fulltext_source = (
                "user_pdf" if any(item[0] == "private" for item in parsed_rows) else "oa"
            )
        elif pending_rows:
            fulltext_status = "parsing"
            fulltext_source = (
                "user_pdf" if any(item[0] == "private" for item in pending_rows) else "oa"
            )
        elif failed_rows:
            fulltext_status = "failed"
            fulltext_source = (
                "user_pdf" if any(item[0] == "private" for item in failed_rows) else "oa"
            )
        elif work.abstract:
            fulltext_status = "abstract_only"
            fulltext_source = "none"
        else:
            fulltext_status = "unavailable"
            fulltext_source = "none"

        evidence_count = evidence_counts.get(work.id, 0)
        assignment_count = assignment_counts.get(work.id, 0)
        citation_count = citation_counts.get(work.id, 0)
        evidence_status: Literal["extracted", "pending", "none"] = (
            "extracted"
            if evidence_count
            else "pending"
            if fulltext_status in {"available", "parsing"}
            else "none"
        )
        assignment_status: Literal["assigned", "pending", "unassigned"] = (
            "assigned" if assignment_count else "pending" if evidence_count else "unassigned"
        )
        unused_reason: str | None = None
        if entry.status == "selected" and citation_count == 0 and usage_evaluated:
            if fulltext_status == "abstract_only":
                unused_reason = "只有摘要，建议上传 PDF 全文"
            elif fulltext_status == "unavailable":
                unused_reason = "无可用文本"
            elif fulltext_status == "failed":
                unused_reason = "全文解析失败"
            elif fulltext_status == "parsing":
                unused_reason = "全文仍在解析"
            elif evidence_count == 0:
                unused_reason = "尚未提取有效证据"
            elif assignment_count == 0:
                unused_reason = "证据尚未分配到研究子问题"
            else:
                unused_reason = "证据与当前章节相关性不足或与已用证据重复"

        result[work.id] = LibraryUtilizationResponse(
            fulltext_status=fulltext_status,
            fulltext_source=fulltext_source,
            evidence_status=evidence_status,
            assignment_status=assignment_status,
            citation_status="cited" if citation_count else "not_cited",
            evidence_count=evidence_count,
            assignment_count=assignment_count,
            citation_count=citation_count,
            usage_evaluated=usage_evaluated,
            unused_reason=unused_reason,
        )
    return result


def _entry_response(
    entry: LibraryEntry,
    work: ScholarlyWork,
    authors: list[str],
    card: LiteratureCard | None,
    utilization: LibraryUtilizationResponse | None = None,
) -> LibraryEntryResponse:
    reason = entry.rank_reason_json or {}
    return LibraryEntryResponse(
        id=str(entry.id),
        work=ScholarlyWorkResponse(
            id=str(work.id),
            canonical_title=work.canonical_title,
            authors=authors,
            publication_year=work.publication_year,
            venue_name=work.venue_name,
            doi=work.doi,
            arxiv_id=work.arxiv_id,
            oa_status=work.oa_status,
            is_retracted=work.is_retracted,
            abstract=work.abstract,
            citation_count=work.citation_count,
        ),
        status=entry.status,
        relevance_score=entry.relevance_score or 0.0,
        rank_reason=reason.get("llm_reason") or reason.get("method"),
        user_pinned=entry.user_pinned,
        added_via=entry.added_via,
        bibtex_key=entry.bibtex_key,
        verified_at=entry.verified_at,
        card=_card_response(card),
        literature_role=getattr(entry, "literature_role", None)
        or ("core" if entry.user_pinned else "general"),
        utilization=utilization,
    )


def _card_response(card: LiteratureCard | None) -> LiteratureCardResponse | None:
    if card is None:
        return None
    return LiteratureCardResponse(
        summary=card.summary or "",
        contributions=card.contributions_json or [],
        methods=card.methods_json or [],
        results=card.results_json or [],
        limitations=card.limitations_json or [],
        quotable_points=card.quotable_points_json or [],
        fulltext_used=card.fulltext_used,
        extraction_model=card.extraction_model,
    )
