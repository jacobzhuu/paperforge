"""项目 / SCOPE / 检索 / 文献库 / 任务 路由（设计 §4.7）。

未实现的端点一律返回 501，不得崩溃；已实现端点在依赖不可用（如 Redis）时返回 503。
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from arq.connections import ArqRedis
from db import (
    create_job,
    create_project,
    get_cards,
    get_entry,
    get_job,
    get_work_authors,
    get_writing_whitelist,
    list_entries,
    list_jobs,
    list_projects,
    list_search_runs,
    project_counters,
    project_llm_cost,
    request_polish_skip,
    set_entry_status,
    update_project,
    update_project_scope,
)
from db.models.library import LibraryEntry, LiteratureCard, ScholarlyWork
from db.models.paper import GenerationJob, PaperProject, SearchRun
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from paperforge_api.deps import (
    CurrentUserDep,
    authorize_project_request,
    get_queue,
    get_session,
)
from paperforge_api.deps import get_authorized_project as _require_project
from paperforge_api.schemas import (
    CostResponse,
    CreateProjectRequest,
    GenerateScopeRequest,
    ImportReferencesRequest,
    JobResponse,
    LibraryEntryResponse,
    LiteratureCardResponse,
    ProjectResponse,
    ScholarlyWorkResponse,
    ScopeResponse,
    SearchRequest,
    SearchRunResponse,
    SelectEntriesRequest,
    UpdateProjectRequest,
    UpdateScopeRequest,
    WhitelistResponse,
)

router = APIRouter(
    prefix="/api/v1", tags=["projects"], dependencies=[Depends(authorize_project_request)]
)

SessionDep = Annotated[AsyncSession, Depends(get_session)]
QueueDep = Annotated[ArqRedis | None, Depends(get_queue)]


# ---- 项目 CRUD ----


@router.post("/projects", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
async def create_project_endpoint(
    request: CreateProjectRequest,
    session: SessionDep,
    user: CurrentUserDep,
) -> ProjectResponse:
    try:
        project = await create_project(
            session,
            title=request.title,
            paper_type=request.paper_type,
            writing_mode=request.writing_mode,
            language=request.language,
            topic=request.topic,
            venue_template=request.venue_template,
            citation_style=request.citation_style,
            contribution_points=request.contribution_points,
            publication_title=request.publication_title,
            authors=request.authors,
            keywords=request.keywords,
            owner_id=user.id,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return _project_response(project, {"library_count": 0, "section_count": 0})


@router.get("/projects", response_model=list[ProjectResponse])
async def list_projects_endpoint(
    session: SessionDep, user: CurrentUserDep
) -> list[ProjectResponse]:
    projects = await list_projects(session, owner_id=user.id)
    responses: list[ProjectResponse] = []
    for project in projects:
        counters = await project_counters(session, project.id)
        responses.append(_project_response(project, counters))
    return responses


@router.get("/projects/{project_id}", response_model=ProjectResponse)
async def get_project_endpoint(project_id: str, session: SessionDep) -> ProjectResponse:
    project = await _require_project(session, project_id)
    counters = await project_counters(session, project.id)
    return _project_response(project, counters)


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
    sent = request.model_fields_set
    patch: dict[str, Any] = {
        name: getattr(request, name) for name in sent if hasattr(request, name)
    }
    try:
        await update_project(session, project, **patch)
    except (ValueError, TypeError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
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
    # 打上 generator='user'：SEARCH 会自动重生成确定性回退留下的降级 scope，
    # 手改过的必须豁免，否则用户调好的关键词会被下一次检索悄悄覆盖。
    await update_project_scope(session, project, {**request.scope, "generator": "user"})
    return ScopeResponse(project_id=str(project.id), scope=project.scope_json or {})


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
    from llm_runtime import LLMRunner
    from paperforge_worker.pipelines.scope import generate_scope as run_generate_scope

    from paperforge_api.config import get_settings

    project = await _require_project(session, project_id)
    topic = (request.topic or (project.scope_json or {}).get("topic") or project.title).strip()
    runner = LLMRunner(get_settings().llm_config())
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
) -> JobResponse:
    project = await _require_project(session, project_id)
    job = await create_job(session, project_id=project.id, kind="search")
    await _enqueue(
        queue,
        "run_library_pipeline",
        str(project.id),
        str(job.id),
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
    job = await create_job(session, project_id=project.id, kind="search")
    await _enqueue(
        queue,
        "run_import_pipeline",
        str(project.id),
        str(job.id),
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
) -> JobResponse:
    project = await _require_project(session, project_id)
    job = await create_job(session, project_id=project.id, kind="cards")
    await _enqueue(queue, "run_cards_pipeline", str(project.id), str(job.id))
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
    responses: list[LibraryEntryResponse] = []
    for entry, work in rows:
        authors = await get_work_authors(session, work.id)
        responses.append(
            _entry_response(entry, work, [a["author_name"] for a in authors], cards.get(work.id))
        )
    return responses


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
    updated: list[LibraryEntryResponse] = []
    cards = await get_cards(session, project.id)
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
        await set_entry_status(session, entry, request.status)
        work = await session.get(ScholarlyWork, work_id)
        authors = await get_work_authors(session, work_id)
        if work is not None:
            updated.append(
                _entry_response(
                    entry, work, [a["author_name"] for a in authors], cards.get(work_id)
                )
            )
    if request.status == "selected":
        await _assign_keys_for_selected(session, project.id)
    return updated


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
async def get_jobs(project_id: str, session: SessionDep) -> list[JobResponse]:
    project = await _require_project(session, project_id)
    return [_job_response(job) for job in await list_jobs(session, project.id)]


@router.get("/projects/{project_id}/jobs/{job_id}", response_model=JobResponse)
async def get_job_endpoint(project_id: str, job_id: str, session: SessionDep) -> JobResponse:
    await _require_project(session, project_id)
    job = await _require_job(session, job_id)
    return _job_response(job)


@router.post("/projects/{project_id}/jobs/{job_id}/polish/skip", response_model=JobResponse)
async def skip_polish(project_id: str, job_id: str, session: SessionDep) -> JobResponse:
    """跳过剩余的连贯性润色。

    润色是每节一次 LLM 调用、整体十几分钟的收尾工序，稿子在此之前就已经完整落库。
    要不要等它，应该由用户当场决定，而不是只能干等或者把整个任务杀掉——
    已润色的章节保留，剩下的直接交付初稿。
    """
    project = await _require_project(session, project_id)
    job = await _require_job(session, job_id)
    if job.project_id != project.id:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status not in {"queued", "running"}:
        raise HTTPException(status_code=409, detail="job already finished")
    await request_polish_skip(session, job)
    return _job_response(job)


@router.get("/projects/{project_id}/cost", response_model=CostResponse)
async def get_cost(project_id: str, session: SessionDep) -> CostResponse:
    project = await _require_project(session, project_id)
    totals = await project_llm_cost(session, project.id)
    return CostResponse(project_id=str(project.id), **totals)


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


async def _enqueue(
    queue: ArqRedis | None,
    function: str,
    *args: Any,
    **kwargs: Any,
) -> None:
    if queue is None:
        raise HTTPException(
            status_code=503,
            detail="task queue unavailable: worker Redis is not reachable",
        )
    await queue.enqueue_job(function, *args, **kwargs)


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


def _project_response(project: PaperProject, counters: dict[str, int]) -> ProjectResponse:
    scope = project.scope_json or {}
    return ProjectResponse(
        id=str(project.id),
        title=project.title,
        paper_type=project.paper_type,
        writing_mode=project.writing_mode,
        language=project.language,
        status=project.status,
        venue_template=project.venue_template,
        citation_style=project.citation_style,
        topic=scope.get("topic"),
        contribution_points=scope.get("contribution_points") or [],
        publication_title=project.publication_title,
        authors=project.authors_json or [],
        keywords=project.keywords_json or [],
        metadata_confirmed=project.metadata_confirmed_at is not None,
        library_count=counters.get("library_count", 0),
        section_count=counters.get("section_count", 0),
        created_at=project.created_at,
        updated_at=project.updated_at,
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


def _entry_response(
    entry: LibraryEntry,
    work: ScholarlyWork,
    authors: list[str],
    card: LiteratureCard | None,
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
