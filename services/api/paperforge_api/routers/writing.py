"""大纲 / 章节 / 引用审计 / Markdown 预览 路由（设计 §4.7）。"""

from __future__ import annotations

import io
import re
import uuid
import zipfile
from datetime import datetime
from typing import Annotated, Any, Literal
from urllib.parse import quote

from arq.connections import ArqRedis
from db import (
    create_document,
    document_snapshot_hash,
    evidence_payload,
    get_cards,
    get_section,
    get_writing_whitelist,
    job_resume_spec,
    latest_document,
    latest_outline,
    latest_quality_report,
    latest_stage_event_payload,
    list_assets,
    list_citation_usage,
    list_claim_evidence,
    list_entries,
    list_evidence_measurements,
    list_evidence_units,
    list_question_evidence_links,
    list_research_questions,
    list_sections,
    list_visuals,
    reference_metadata_payload,
    replace_citation_usage,
    update_outline_tree,
    upsert_question_evidence_link,
    upsert_section,
)
from db.models.paper import (
    ClaimEvidenceAnchor,
    ExportArtifact,
    GenerationJob,
    JobEvent,
    QuestionEvidenceLink,
    ResearchQuestion,
)
from db.repositories.exports import list_export_artifacts
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from paper_ir import Bibliography, PaperIR, PaperMeta, ReferenceMetadata, render_markdown
from paper_ir.schema import Section as IRSection
from paperforge_worker.orchestration.writing_graph import fingerprint
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from storage import make_object_store

from paperforge_api.concurrency import require_section_unchanged
from paperforge_api.config import get_settings
from paperforge_api.deps import (
    authorize_project_request,
    get_queue,
    get_session,
)
from paperforge_api.deps import get_authorized_project as _require_project
from paperforge_api.jobs import start_job
from paperforge_api.llm_accounting import accounted_runner
from paperforge_api.routers.projects import _job_response
from paperforge_api.schemas import (
    AcceptSectionRewriteRequest,
    AcceptSectionRewriteResponse,
    CitationAuditResponse,
    CitationAuditRow,
    ClaimEvidenceResponse,
    EvidenceMatrixDiagnostics,
    EvidenceMatrixPerQuestionDiagnostics,
    EvidenceMatrixResponse,
    EvidenceUnitResponse,
    ExportArtifactResponse,
    ExportRequest,
    FullPipelineOptionsRequest,
    GenerationOptionsRequest,
    IngestRequest,
    JobResponse,
    MarkdownResponse,
    OutlineResponse,
    QualityResponse,
    QuestionEvidenceLinkResponse,
    RebuildDependenciesRequest,
    RefineRequest,
    RefineResponse,
    ResearchQuestionResponse,
    ReviewClaimEvidenceRequest,
    RewriteSectionCandidateResponse,
    RewriteSectionRequest,
    SectionResponse,
    SnowballRequest,
    SynthesisResponse,
    UpdateOutlineRequest,
    UpdateQuestionEvidenceLinkRequest,
    UpdateResearchQuestionRequest,
    UpdateSectionRequest,
    WriteRequest,
)

router = APIRouter(
    prefix="/api/v1", tags=["writing"], dependencies=[Depends(authorize_project_request)]
)

SessionDep = Annotated[AsyncSession, Depends(get_session, scope="function")]
QueueDep = Annotated[ArqRedis | None, Depends(get_queue)]


# ---- 大纲 ----


@router.post(
    "/projects/{project_id}/outline/generate",
    response_model=JobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def generate_outline_endpoint(
    project_id: str,
    session: SessionDep,
    queue: QueueDep,
) -> JobResponse:
    project = await _require_project(session, project_id)
    job = await start_job(
        session,
        queue,
        project_id=project.id,
        kind="outline",
        function="run_outline_pipeline",
    )
    return _job_response(job)


@router.post(
    "/projects/{project_id}/outline/dependencies/rebuild",
    response_model=JobResponse,
    status_code=202,
)
async def rebuild_dependencies(
    project_id: str, request: RebuildDependenciesRequest, session: SessionDep, queue: QueueDep
):
    from paperforge_worker.orchestration.writing_graph import fingerprint

    project = await _require_project(session, project_id)
    source = await latest_outline(session, project.id)
    if source is None or source.id != request.outline_id:
        raise HTTPException(409, "outline version changed")
    if fingerprint(source.tree_json) != request.content_hash:
        raise HTTPException(409, "outline content changed")
    job = await start_job(
        session,
        queue,
        project_id=project.id,
        kind="outline",
        function="run_dependency_rebuild_pipeline",
        outline_id=str(source.id),
        source_hash=request.content_hash,
    )
    return _job_response(job)


@router.get("/projects/{project_id}/outline", response_model=OutlineResponse)
async def get_outline_endpoint(project_id: str, session: SessionDep) -> OutlineResponse:
    project = await _require_project(session, project_id)
    outline = await latest_outline(session, project.id)
    if outline is None:
        return OutlineResponse(project_id=str(project.id), version=0, status="draft", tree={})
    latest_synthesis_at = await session.scalar(
        select(func.max(JobEvent.created_at))
        .join(GenerationJob, GenerationJob.id == JobEvent.job_id)
        .where(
            GenerationJob.project_id == project.id,
            JobEvent.event_type == "synth.completed",
        )
    )
    stale = bool(latest_synthesis_at and latest_synthesis_at > outline.updated_at)
    return OutlineResponse(
        project_id=str(project.id),
        outline_id=str(outline.id),
        content_hash=fingerprint(outline.tree_json),
        version=outline.version,
        status=outline.status,
        tree=outline.tree_json or {},
        stale=stale,
        stale_reason="evidence_synthesis_newer" if stale else None,
    )


@router.put("/projects/{project_id}/outline", response_model=OutlineResponse)
async def put_outline(
    project_id: str,
    request: UpdateOutlineRequest,
    session: SessionDep,
) -> OutlineResponse:
    """人工编辑大纲。

    R2 前置：这里会把章节 cite_keys 收敛到写作白名单内——手工编辑同样
    无法把白名单外的引用带进写作上下文。
    """
    project = await _require_project(session, project_id)
    outline = await latest_outline(session, project.id)
    if outline is None:
        raise HTTPException(status_code=404, detail="outline not generated yet")
    whitelist = set(await get_writing_whitelist(session, project.id))
    from db import create_outline
    from paperforge_worker.orchestration.dependency_contract import content_hash, validate_edit

    tree = _sanitize_outline_tree(request.tree, whitelist)
    try:
        tree = validate_edit(tree, outline.tree_json or {})
    except (ValueError, TypeError) as error:
        raise HTTPException(422, str(error)) from error
    if (outline.tree_json or {}).get("dependency_contract") and content_hash(tree) != content_hash(
        outline.tree_json
    ):
        outline = await create_outline(session, project_id=project.id, tree=tree, status="draft")
    else:
        await update_outline_tree(session, outline, tree, status=request.status)
    return OutlineResponse(
        project_id=str(project.id),
        outline_id=str(outline.id),
        content_hash=fingerprint(outline.tree_json),
        version=outline.version,
        status=outline.status,
        tree=outline.tree_json or {},
    )


# ---- 研究问题与证据矩阵 ----


def _clean_string_list(values: list[str]) -> list[str]:
    return list(dict.fromkeys(cleaned for value in values if (cleaned := " ".join(value.split()))))


def _question_response(row: ResearchQuestion) -> ResearchQuestionResponse:
    return ResearchQuestionResponse(
        id=str(row.id),
        parent_id=str(row.parent_id) if row.parent_id else None,
        text=row.text,
        kind=row.kind,
        order_index=row.order_index,
        comparison_dimensions=row.comparison_dimensions_json or [],
        expected_evidence_kinds=row.expected_evidence_kinds_json or [],
        answer_status=row.answer_status,
        generator=row.generator,
        origin=row.origin if row.origin in {"auto", "user"} else "auto",
        locked=row.locked,
        task_id=row.task_id,
        search_query=row.search_query,
    )


def _matrix_link_response(row: QuestionEvidenceLink) -> QuestionEvidenceLinkResponse:
    return QuestionEvidenceLinkResponse(
        id=str(row.id),
        research_question_id=str(row.research_question_id),
        evidence_unit_id=str(row.evidence_unit_id),
        stance=row.stance,
        condition_note=row.condition_note,
        confidence=row.confidence,
        manually_overridden=row.manually_overridden,
    )


async def _evidence_responses(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> list[EvidenceUnitResponse]:
    units = await list_evidence_units(session, project_id)
    measurements = await list_evidence_measurements(session, [row.id for row in units])
    entries = await list_entries(session, project_id, status="selected")
    metadata = {str(work.id): (entry.bibtex_key, work.canonical_title) for entry, work in entries}
    responses: list[EvidenceUnitResponse] = []
    for unit in units:
        payload = evidence_payload(unit, measurements.get(unit.id))
        cite_key, title = metadata.get(str(unit.work_id), (None, None))
        responses.append(
            EvidenceUnitResponse(
                **payload,
                cite_key=cite_key,
                title=title,
            )
        )
    return responses


async def _matrix_diagnostics(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> EvidenceMatrixDiagnostics:
    evidence = await list_evidence_units(session, project_id)
    links = await list_question_evidence_links(session, project_id)
    questions = await list_research_questions(session, project_id)
    linked_ids = {row.evidence_unit_id for row in links}
    diagnostics = EvidenceMatrixDiagnostics(
        evidence_unit_count=len(evidence),
        link_count=len(links),
        question_count=len(questions),
        unlinked_evidence_count=max(0, len(evidence) - len(linked_ids)),
    )
    payload = await latest_stage_event_payload(session, project_id, "qmatrix")
    if not payload:
        return diagnostics
    if payload.get("rejected_by_lexical") is not None:
        diagnostics.rejected_by_lexical = int(payload["rejected_by_lexical"])
    if payload.get("rejected_by_task") is not None:
        diagnostics.rejected_by_task = int(payload["rejected_by_task"])
    if payload.get("zero_candidate_questions") is not None:
        diagnostics.zero_candidate_questions = int(payload["zero_candidate_questions"])
    bridge_sources = payload.get("bridge_sources")
    if isinstance(bridge_sources, dict) and bridge_sources:
        diagnostics.bridge_sources = {str(key): int(value) for key, value in bridge_sources.items()}
    per_question = payload.get("diagnostics")
    if isinstance(per_question, list) and per_question:
        diagnostics.per_question = [
            EvidenceMatrixPerQuestionDiagnostics.model_validate(item)
            for item in per_question
            if isinstance(item, dict)
        ]
    return diagnostics


@router.get(
    "/projects/{project_id}/questions",
    response_model=list[ResearchQuestionResponse],
)
async def get_research_questions(
    project_id: str,
    session: SessionDep,
) -> list[ResearchQuestionResponse]:
    project = await _require_project(session, project_id)
    return [_question_response(row) for row in await list_research_questions(session, project.id)]


@router.post(
    "/projects/{project_id}/questions/generate",
    response_model=JobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def generate_research_questions(
    project_id: str,
    session: SessionDep,
    queue: QueueDep,
) -> JobResponse:
    project = await _require_project(session, project_id)
    job = await start_job(
        session,
        queue,
        project_id=project.id,
        kind="qdecomp",
        function="run_qdecomp_pipeline",
    )
    return _job_response(job)


@router.patch(
    "/projects/{project_id}/questions/{question_id}",
    response_model=ResearchQuestionResponse,
)
async def patch_research_question(
    project_id: str,
    question_id: str,
    request: UpdateResearchQuestionRequest,
    session: SessionDep,
) -> ResearchQuestionResponse:
    project = await _require_project(session, project_id)
    try:
        row = await session.get(ResearchQuestion, uuid.UUID(question_id))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="research question not found") from exc
    if row is None or row.project_id != project.id:
        raise HTTPException(status_code=404, detail="research question not found")
    changes = request.model_fields_set
    if "text" in changes:
        text = " ".join((request.text or "").split())
        if not text:
            raise HTTPException(status_code=422, detail="question text cannot be empty")
        row.text = text
    if "comparison_dimensions" in changes:
        row.comparison_dimensions_json = _clean_string_list(request.comparison_dimensions or [])
    if "expected_evidence_kinds" in changes:
        row.expected_evidence_kinds_json = _clean_string_list(request.expected_evidence_kinds or [])
    if "task_id" in changes:
        row.task_id = " ".join((request.task_id or "").split()) or None
    if "search_query" in changes:
        row.search_query = " ".join((request.search_query or "").split()) or None
    if request.answer_status is not None:
        row.answer_status = request.answer_status
    row.generator = "user"
    # 改动了问题的定义（而不只是给矩阵结论打分）就锁定这一行：否则下一次 QDECOMP
    # 会按 scope 重新生成，把用户刚写的问题覆盖掉。显式传 locked 时以它为准。
    if changes & {"text", "comparison_dimensions", "expected_evidence_kinds", "search_query"}:
        row.origin = "user"
        row.locked = True
    if request.locked is not None:
        row.locked = request.locked
    await session.flush()
    return _question_response(row)


@router.get(
    "/projects/{project_id}/evidence-units",
    response_model=list[EvidenceUnitResponse],
)
async def get_evidence_units(
    project_id: str,
    session: SessionDep,
) -> list[EvidenceUnitResponse]:
    project = await _require_project(session, project_id)
    return await _evidence_responses(session, project.id)


@router.post(
    "/projects/{project_id}/evidence-units/generate",
    response_model=JobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def generate_evidence_units(
    project_id: str,
    session: SessionDep,
    queue: QueueDep,
) -> JobResponse:
    project = await _require_project(session, project_id)
    job = await start_job(
        session,
        queue,
        project_id=project.id,
        kind="evidence",
        function="run_evidence_pipeline",
    )
    return _job_response(job)


@router.get(
    "/projects/{project_id}/evidence-matrix",
    response_model=EvidenceMatrixResponse,
)
async def get_evidence_matrix(
    project_id: str,
    session: SessionDep,
) -> EvidenceMatrixResponse:
    project = await _require_project(session, project_id)
    questions = await list_research_questions(session, project.id)
    links = await list_question_evidence_links(session, project.id)
    return EvidenceMatrixResponse(
        questions=[_question_response(row) for row in questions],
        evidence=await _evidence_responses(session, project.id),
        links=[_matrix_link_response(row) for row in links],
        diagnostics=await _matrix_diagnostics(session, project.id),
    )


@router.post(
    "/projects/{project_id}/evidence-matrix/generate",
    response_model=JobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def generate_evidence_matrix(
    project_id: str,
    session: SessionDep,
    queue: QueueDep,
) -> JobResponse:
    project = await _require_project(session, project_id)
    job = await start_job(
        session,
        queue,
        project_id=project.id,
        kind="qmatrix",
        function="run_alignment_pipeline",
    )
    return _job_response(job)


@router.post(
    "/projects/{project_id}/draft/rebuild",
    response_model=JobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def rebuild_draft_from_latest_evidence(
    project_id: str,
    session: SessionDep,
    queue: QueueDep,
    request: GenerationOptionsRequest | None = None,
) -> JobResponse:
    project = await _require_project(session, project_id)
    options = request or GenerationOptionsRequest()
    job = await start_job(
        session,
        queue,
        project_id=project.id,
        kind="write",
        function="run_draft_rebuild_pipeline",
        quality_profile=options.quality_profile,
        review_style=options.review_style,
    )
    return _job_response(job)


@router.post(
    "/projects/{project_id}/synthesis/generate",
    response_model=JobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def generate_synthesis(
    project_id: str,
    session: SessionDep,
    queue: QueueDep,
) -> JobResponse:
    project = await _require_project(session, project_id)
    job = await start_job(
        session,
        queue,
        project_id=project.id,
        kind="synth",
        function="run_synthesis_pipeline",
    )
    return _job_response(job)


@router.get(
    "/projects/{project_id}/synthesis",
    response_model=SynthesisResponse,
)
async def get_synthesis(
    project_id: str,
    session: SessionDep,
) -> SynthesisResponse:
    project = await _require_project(session, project_id)
    questions = await list_research_questions(session, project.id)
    synth_payload = await latest_stage_event_payload(session, project.id, "synth")
    bundles: list[dict[str, Any]] | None = None
    comparison_cluster_count = 0
    if synth_payload:
        raw_bundles = synth_payload.get("bundles")
        if isinstance(raw_bundles, list):
            bundles = [item for item in raw_bundles if isinstance(item, dict)]
        comparison_cluster_count = int(synth_payload.get("comparison_clusters") or 0)
    return SynthesisResponse(
        questions=[_question_response(row) for row in questions],
        bundles=bundles,
        comparison_cluster_count=comparison_cluster_count,
    )


@router.patch(
    "/projects/{project_id}/evidence-matrix/{link_id}",
    response_model=QuestionEvidenceLinkResponse,
)
async def patch_evidence_matrix_link(
    project_id: str,
    link_id: str,
    request: UpdateQuestionEvidenceLinkRequest,
    session: SessionDep,
) -> QuestionEvidenceLinkResponse:
    project = await _require_project(session, project_id)
    try:
        link = await session.get(QuestionEvidenceLink, uuid.UUID(link_id))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="matrix link not found") from exc
    question = (
        await session.get(ResearchQuestion, link.research_question_id) if link is not None else None
    )
    if link is None or question is None or question.project_id != project.id:
        raise HTTPException(status_code=404, detail="matrix link not found")
    updated = await upsert_question_evidence_link(
        session,
        research_question_id=link.research_question_id,
        evidence_unit_id=link.evidence_unit_id,
        stance=request.stance,
        condition_note=(request.condition_note or "").strip() or None,
        confidence=link.confidence,
        manually_overridden=True,
    )
    return _matrix_link_response(updated)


# ---- 写作 ----


@router.post(
    "/projects/{project_id}/generate",
    response_model=JobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def generate_full(
    project_id: str,
    session: SessionDep,
    queue: QueueDep,
    request: FullPipelineOptionsRequest | None = None,
) -> JobResponse:
    """一键全管线（kind=full）。"""
    project = await _require_project(session, project_id)
    if project.paper_type == "original":
        issues = _original_generation_prerequisites(await list_assets(session, project.id))
        if issues:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "original_materials_required",
                    "message": "原创论文全自动生成需要可核验的方法与结果素材",
                    "issues": issues,
                },
            )
    options = request or FullPipelineOptionsRequest()
    job = await start_job(
        session,
        queue,
        project_id=project.id,
        kind="full",
        function="run_full_pipeline",
        quality_profile=options.quality_profile,
        review_style=options.review_style,
    )
    return _job_response(job)


def _original_generation_prerequisites(assets: list[Any]) -> list[dict[str, str]]:
    """Return actionable, deterministic blockers before an expensive original-paper run."""
    has_results = False
    has_method = False
    for asset in assets:
        parsed = asset.parsed_json if isinstance(asset.parsed_json, dict) else {}
        if asset.kind in {"dataset", "result_table"}:
            has_results = (
                bool(parsed.get("rows") and (parsed.get("numeric_cells") or parsed.get("numbers")))
                or has_results
            )
        if asset.kind in {"method_note", "code"}:
            has_method = (
                bool(str(parsed.get("text") or asset.description or "").strip()) or has_method
            )
    issues: list[dict[str, str]] = []
    if not has_results:
        issues.append(
            {
                "code": "result_material_missing",
                "message": "请上传包含数据行和可解析数值的结果表或数据集",
            }
        )
    if not has_method:
        issues.append(
            {
                "code": "method_material_missing",
                "message": "请上传可解析的方法笔记或代码",
            }
        )
    return issues


@router.post(
    "/projects/{project_id}/sections/generate",
    response_model=JobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def generate_sections(
    project_id: str,
    request: WriteRequest,
    session: SessionDep,
    queue: QueueDep,
) -> JobResponse:
    project = await _require_project(session, project_id)
    outline = await latest_outline(session, project.id)
    if outline is None:
        raise HTTPException(status_code=409, detail="generate an outline first")
    contract = (outline.tree_json or {}).get("dependency_contract")
    if contract and contract.get("requires_confirmation", True) and outline.status != "confirmed":
        raise HTTPException(409, "confirm rebuilt outline before writing")
    from paperforge_api.config import get_settings

    settings = get_settings()
    checkpoint = {
        "writer_outline_id": str(outline.id),
        "writer_outline_hash": fingerprint(outline.tree_json),
        "semantic_repair_engine": settings.semantic_repair_engine,
        "evidence_retrieval_mode": settings.evidence_retrieval_mode,
        "writer_polish_policy": settings.writer_polish_policy,
        "writer_polish_concurrency": settings.writer_polish_concurrency,
    }
    if request.polish_policy is not None:
        checkpoint["writer_polish_policy"] = request.polish_policy
    if contract and contract.get("requires_confirmation", True):
        checkpoint["writer_execution_mode"] = "dag_parallel"
    job = await start_job(
        session,
        queue,
        project_id=project.id,
        kind="write",
        function="run_write_pipeline",
        checkpoint=checkpoint or None,
        coherence=request.coherence,
    )
    return _job_response(job)


@router.get("/projects/{project_id}/sections", response_model=list[SectionResponse])
async def get_sections(project_id: str, session: SessionDep) -> list[SectionResponse]:
    project = await _require_project(session, project_id)
    document = await latest_document(session, project.id)
    if document is None:
        return []
    rows = await list_sections(session, document.id)
    return [_section_response(row) for row in rows]


@router.put("/projects/{project_id}/sections/{section_key}", response_model=SectionResponse)
async def put_section(
    project_id: str,
    section_key: str,
    request: UpdateSectionRequest,
    session: SessionDep,
) -> SectionResponse:
    """人工编辑章节。

    R2：编辑器提交的 body_ir 会再过一次白名单——前端 chip 已限制取值，
    这里是服务端的兜底，手工构造请求也无法引入幻觉引用。

    乐观并发：带 `expected_updated_at` 时，若章节已被别处改动就返回 409
    `section_changed`。最典型的场景是视觉批准刚把 FigureBlock 写进这一节，
    而编辑器手上还是插图之前的草稿——直接保存会把图静默删掉。
    """
    project = await _require_project(session, project_id)
    document = await latest_document(session, project.id)
    if document is None:
        raise HTTPException(status_code=404, detail="document not generated yet")
    row = await get_section(session, document_id=document.id, section_key=section_key)
    if row is None:
        raise HTTPException(status_code=404, detail="section not found")
    require_section_unchanged(row, request.expected_updated_at)

    whitelist = await get_writing_whitelist(session, project.id)
    try:
        ir_section = IRSection(**request.body_ir)
    except Exception as error:  # noqa: BLE001 - 结构非法直接 422
        raise HTTPException(status_code=422, detail=f"invalid section IR: {error}") from error

    ir = PaperIR(meta=PaperMeta(title=project.title), sections=[ir_section])
    ir.enforce_cite_key_whitelist(set(whitelist), strip=True)
    cleaned = ir.sections[0]
    cite_keys = sorted(ir.collect_cite_keys())
    asset_refs = sorted(ir.collect_asset_refs())

    updated = await upsert_section(
        session,
        document_id=document.id,
        section_key=section_key,
        title=request.title or cleaned.title,
        order_no=row.order_no,
        body_ir=cleaned.model_dump(mode="json"),
        cite_keys=cite_keys,
        asset_refs=asset_refs,
        status="edited",
        model=row.model,
    )
    await replace_citation_usage(
        session,
        project_id=project.id,
        section_id=updated.id,
        usages=[
            {"work_id": whitelist[key], "cite_key": key, "context_snippet": None}
            for key in cite_keys
            if key in whitelist
        ],
    )
    return _section_response(updated)


# ---- 引用审计与预览 ----


@router.get("/projects/{project_id}/citations/audit", response_model=CitationAuditResponse)
async def citations_audit(project_id: str, session: SessionDep) -> CitationAuditResponse:
    """引用审计报告（R2 结果）。

    ``hallucinated_cite_keys`` 恒为空即为 0 幻觉引用；非空表示某处绕过了
    白名单，属于必须修复的缺陷而非提示。
    """
    project = await _require_project(session, project_id)
    whitelist = await get_writing_whitelist(session, project.id)
    document = await latest_document(session, project.id)
    rows = await list_sections(session, document.id) if document else []
    usage = await list_citation_usage(session, project.id)

    used_keys: set[str] = set()
    warnings: list[dict[str, Any]] = []
    for row in rows:
        used_keys.update(row.cite_keys_json or [])
        body = row.body_ir_json or {}
        for warning in body.get("citation_warnings") or []:
            warnings.append({"section_key": row.section_key, **warning})

    return CitationAuditResponse(
        project_id=str(project.id),
        whitelist_size=len(whitelist),
        used_cite_keys=sorted(used_keys),
        hallucinated_cite_keys=sorted(used_keys - set(whitelist)),
        removed_citation_warnings=warnings,
        unused_cite_keys=sorted(set(whitelist) - used_keys),
        rows=[
            CitationAuditRow(
                cite_key=item.cite_key,
                section_id=str(item.section_id),
                work_id=str(item.work_id),
                context_snippet=item.context_snippet,
            )
            for item in usage
        ],
    )


@router.get("/projects/{project_id}/preview/markdown", response_model=MarkdownResponse)
async def markdown_preview(project_id: str, session: SessionDep) -> MarkdownResponse:
    """Markdown 预览：渲染器只消费持久化的 Section IR。"""
    project = await _require_project(session, project_id)
    document = await latest_document(session, project.id)
    if document is None:
        return MarkdownResponse(project_id=str(project.id), markdown="", word_count=0)

    rows = await list_sections(session, document.id)
    whitelist = await get_writing_whitelist(session, project.id)
    used_keys = {key for row in rows for key in (row.cite_keys_json or [])}
    references: list[ReferenceMetadata] = []
    for entry, work in await list_entries(session, project.id, status="selected"):
        if entry.bibtex_key and entry.bibtex_key in used_keys:
            payload = await reference_metadata_payload(session, work, bibtex_key=entry.bibtex_key)
            references.append(ReferenceMetadata(**payload))

    sections = [IRSection(**row.body_ir_json) for row in rows if row.body_ir_json]
    abstract_section = next((s for s in sections if s.key == "abstract"), None)
    body_sections = [s for s in sections if s.key != "abstract"]
    abstract_text = ""
    if abstract_section is not None:
        abstract_text = " ".join(
            run.get("v", "")
            for block in abstract_section.model_dump(mode="json").get("blocks", [])
            for run in block.get("runs", [])
            if run.get("t") == "text"
        ).strip()

    ir = PaperIR(
        meta=PaperMeta(
            title=project.publication_title or project.title,
            authors=project.authors_json or [],
            author_details=getattr(project, "author_details_json", None) or [],
            abstract=abstract_text,
            keywords=project.keywords_json or [],
            language=project.language,  # type: ignore[arg-type]
        ),
        sections=body_sections,
        bibliography=Bibliography(style=project.citation_style),  # type: ignore[arg-type]
    )
    ir.enforce_cite_key_whitelist(set(whitelist), strip=True)
    asset_urls: dict[str, str] = {}
    for asset in await list_assets(session, project.id):
        url = f"/api/v1/projects/{project.id}/assets/{asset.id}/download"
        asset_urls[str(asset.id)] = url
        asset_urls[f"ua_{str(asset.id)[:8]}"] = url
    for visual in await list_visuals(session, project.id):
        renditions = visual.renditions_json or {}
        fmt = "png" if "png" in renditions else ("svg" if "svg" in renditions else None)
        if fmt:
            url = f"/api/v1/projects/{project.id}/visuals/{visual.id}/renditions/{fmt}"
            asset_urls[str(visual.id)] = url
            asset_urls[f"va_{str(visual.id)[:8]}"] = url
    markdown = render_markdown(
        ir,
        references=references,
        style=project.citation_style,
        asset_urls=asset_urls,
    )
    return MarkdownResponse(
        project_id=str(project.id),
        markdown=markdown,
        word_count=_count_words(markdown),
        document_version=document.version,
    )


# ---- 内部工具 ----


def _sanitize_outline_tree(tree: dict[str, Any], whitelist: set[str]) -> dict[str, Any]:
    sections = []
    for section in tree.get("sections") or []:
        if not isinstance(section, dict):
            continue
        sections.append(
            {
                **section,
                "cite_keys": [key for key in (section.get("cite_keys") or []) if key in whitelist],
            }
        )
    return {**tree, "sections": sections}


def _section_response(row) -> SectionResponse:
    body = row.body_ir_json or {}
    return SectionResponse(
        section_key=row.section_key,
        title=row.title,
        order_no=row.order_no,
        status=row.status,
        model=row.model,
        cite_keys=row.cite_keys_json or [],
        body_ir=body,
        citation_warnings=body.get("citation_warnings") or [],
        word_count=_count_section_words(body),
        updated_at=row.updated_at,
    )


def _claim_evidence_response(row: ClaimEvidenceAnchor) -> ClaimEvidenceResponse:
    return ClaimEvidenceResponse(
        id=str(row.id),
        report_id=str(row.quality_report_id),
        section_key=row.section_key,
        claim_text=row.claim_text,
        claim_kind=row.claim_kind,
        is_core=row.is_core,
        cite_key=row.cite_key,
        source_key=row.source_key,
        user_asset_id=str(row.user_asset_id) if row.user_asset_id else None,
        source_kind=row.source_kind,
        source_page=row.source_page,
        source_section=row.source_section,
        source_paragraph=row.source_paragraph,
        evidence_excerpt=row.evidence_excerpt,
        evidence_hash=row.evidence_hash,
        evidence_unit_id=str(row.evidence_unit_id) if row.evidence_unit_id else None,
        comparability_ok=row.comparability_ok,
        grade_ok=row.grade_ok,
        support_status=row.support_status,
        support_score=row.support_score,
        manual_status=row.manual_status,
        entailment_verdict=row.entailment_verdict,
        entailment_confidence=row.entailment_confidence,
        entailment_reason=row.entailment_reason,
        entailment_model=row.entailment_model,
        entailment_verifier_version=row.entailment_verifier_version,
        entailment_cached=row.entailment_cached,
        entailment_review=row.entailment_review_json,
        entailment_checked_at=row.entailment_checked_at,
    )


def _count_section_words(body: dict[str, Any]) -> int:
    text = " ".join(
        run.get("v", "")
        for block in body.get("blocks", [])
        for run in block.get("runs", [])
        if run.get("t") == "text"
    )
    return _count_words(text)


def _count_words(text: str) -> int:
    import re

    cjk = len(re.findall(r"[一-鿿]", text))
    latin = len(re.findall(r"[A-Za-z][A-Za-z'-]*", text))
    return cjk + latin


# ---- 导出（M3） ----


@router.post(
    "/projects/{project_id}/exports",
    response_model=JobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_export(
    project_id: str,
    request: ExportRequest,
    session: SessionDep,
    queue: QueueDep,
) -> JobResponse:
    project = await _require_project(session, project_id)
    document = await latest_document(session, project.id)
    if document is None:
        raise HTTPException(status_code=409, detail="generate the paper before exporting")
    rows = await list_sections(session, document.id)
    snapshot_hash = document_snapshot_hash(rows)
    quality_report = await latest_quality_report(
        session,
        project.id,
        quality_profile=request.quality_profile,
    )
    if request.quality_profile != "draft":
        blockers: list[dict[str, Any]] = []
        if quality_report is None:
            blockers.append({"code": "quality_report_missing", "message": "请先生成投稿质量报告"})
        else:
            stale = quality_report.stale or quality_report.paper_snapshot_hash != snapshot_hash
            if stale:
                quality_report.stale = True
                blockers.append(
                    {
                        "code": "quality_report_stale",
                        "message": "正文、引用或视觉已变化，请重新质检",
                    }
                )
            if quality_report.readiness_status not in {"preflight_ready", "submission_ready"}:
                blockers.extend(quality_report.blockers_json or [])
        if blockers:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "quality_gate_failed",
                    "readiness_status": (
                        quality_report.readiness_status if quality_report else "unassessed"
                    ),
                    "blockers": blockers,
                },
            )
    job = await start_job(
        session,
        queue,
        project_id=project.id,
        kind="compile",
        function="run_export_pipeline",
        formats=request.formats,
        quality_profile=request.quality_profile,
        quality_report_id=str(quality_report.id) if quality_report else None,
        readiness_status=quality_report.readiness_status if quality_report else "unassessed",
        paper_snapshot_hash=snapshot_hash,
    )
    return _job_response(job)


@router.get("/projects/{project_id}/exports", response_model=list[ExportArtifactResponse])
async def list_exports(project_id: str, session: SessionDep) -> list[ExportArtifactResponse]:
    project = await _require_project(session, project_id)
    rows = await list_export_artifacts(session, project.id)
    return [
        ExportArtifactResponse(
            id=str(row.id),
            format=row.format,
            document_version=row.document_version,
            object_key=row.object_key,
            content_hash=row.content_hash,
            created_at=row.created_at,
            download_url=f"/api/v1/projects/{project.id}/exports/{row.id}/download",
            quality_report_id=str(row.quality_report_id) if row.quality_report_id else None,
            quality_profile=row.quality_profile,
            readiness_status=row.readiness_status,
            paper_snapshot_hash=row.paper_snapshot_hash,
            export_run_id=str(row.export_run_id) if row.export_run_id else None,
        )
        for row in rows
    ]


@router.get("/projects/{project_id}/exports/runs/{run_id}/download")
async def download_export_run(project_id: str, run_id: str, session: SessionDep) -> Response:
    """Download every surviving file from one real export run as a zip archive."""
    project = await _require_project(session, project_id)
    try:
        run_uuid = uuid.UUID(run_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail="export run not found") from error
    job = await session.get(GenerationJob, run_uuid)
    if job is None or job.project_id != project.id or job.kind != "compile":
        raise HTTPException(status_code=404, detail="export run not found")
    artifacts = list(
        (
            await session.scalars(
                select(ExportArtifact)
                .where(
                    ExportArtifact.project_id == project.id,
                    ExportArtifact.export_run_id == job.id,
                )
                .order_by(ExportArtifact.created_at, ExportArtifact.id)
            )
        ).all()
    )
    if not artifacts:
        raise HTTPException(status_code=409, detail="export run has no downloadable files")

    store = make_object_store(get_settings())
    archive = io.BytesIO()
    written = 0
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for artifact in artifacts:
            if not artifact.object_key:
                continue
            try:
                payload = store.get(artifact.object_key)
            except (FileNotFoundError, ValueError):
                continue
            _, suffix = _MEDIA_TYPES.get(artifact.format, ("application/octet-stream", "bin"))
            bundle.writestr(
                export_filename(
                    project.title,
                    fmt=artifact.format,
                    suffix=suffix,
                    document_version=artifact.document_version,
                    created_at=artifact.created_at,
                ),
                payload,
            )
            written += 1
    if written == 0:
        raise HTTPException(status_code=410, detail="export run payloads are gone")
    return Response(
        content=archive.getvalue(),
        media_type="application/zip",
        headers={
            "Content-Disposition": _content_disposition(
                "attachment", f"{project.title}-export-run-{str(job.id)[:8]}.zip"
            ),
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post(
    "/projects/{project_id}/exports/runs/{run_id}/retry",
    response_model=JobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def retry_export_run(
    project_id: str,
    run_id: str,
    session: SessionDep,
    queue: QueueDep,
) -> JobResponse:
    """Start a new export run with a failed/cancelled run's persisted parameters."""
    project = await _require_project(session, project_id)
    try:
        run_uuid = uuid.UUID(run_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail="export run not found") from error
    source = await session.get(GenerationJob, run_uuid)
    if source is None or source.project_id != project.id or source.kind != "compile":
        raise HTTPException(status_code=404, detail="export run not found")
    if source.status not in {"failed", "cancelled"}:
        raise HTTPException(status_code=409, detail="only a failed export run can be retried")
    spec = job_resume_spec(source)
    if spec is None or spec["function"] != "run_export_pipeline":
        raise HTTPException(status_code=409, detail="export run has no retry parameters")
    retried = await start_job(
        session,
        queue,
        project_id=project.id,
        kind="compile",
        function=spec["function"],
        checkpoint={"retried_from": str(source.id)},
        **spec["kwargs"],
    )
    return _job_response(retried)


@router.get("/projects/{project_id}/exports/{artifact_id}/download")
async def download_export(
    project_id: str,
    artifact_id: str,
    session: SessionDep,
    disposition: str = "attachment",
) -> Response:
    """取产物。``disposition=inline`` 供页内预览用。

    预览与下载必须是两条不同的响应：`attachment` 会让 `<iframe src>` 变成一次
    下载，于是「打开导出中心」等于「凭空下载一个文件」，而预览框永远是空的。
    """
    project = await _require_project(session, project_id)
    try:
        artifact_uuid = uuid.UUID(artifact_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail="artifact not found") from error
    artifact = await session.get(ExportArtifact, artifact_uuid)
    if artifact is None or artifact.project_id != project.id or not artifact.object_key:
        raise HTTPException(status_code=404, detail="artifact not found")

    store = make_object_store(get_settings())
    try:
        data = store.get(artifact.object_key)
    except (FileNotFoundError, ValueError) as error:
        raise HTTPException(status_code=410, detail="artifact payload is gone") from error

    media_type, suffix = _MEDIA_TYPES.get(artifact.format, ("application/octet-stream", "bin"))
    mode = "inline" if disposition.lower() == "inline" else "attachment"
    return Response(
        content=data,
        media_type=media_type,
        headers={
            "Content-Disposition": _content_disposition(
                mode,
                export_filename(
                    project.title,
                    fmt=artifact.format,
                    suffix=suffix,
                    document_version=artifact.document_version,
                    created_at=artifact.created_at,
                ),
            ),
            # 产物按内容寻址（object_key 含 sha256 前缀），内容永不原地变化。
            "Cache-Control": "private, max-age=3600",
        },
    )


_FORMAT_FILENAME_TAG = {
    "compile_log": "编译日志",
    "latex_zip": "latex",
    "bibtex": "refs",
}

# 文件名里不许出现的字符：路径分隔符与 Windows 保留字符、控制字符、各类空白，
# 以及中英文标点。中日韩文字与字母数字保留——中文标题正是要读得懂的那部分。
_UNSAFE_FILENAME_CHARS = re.compile(
    r'[\x00-\x1f\x7f<>:"/\\|?*\s.,;:!\'’“”，。；：！？、·（）()\[\]{}【】《》〈〉…]+'
)
MAX_FILENAME_STEM = 60


def _slugify_title(title: str) -> str:
    """标题 → 文件名主干。保留中日韩文字与字母数字，其余压成连字符。"""
    cleaned = _UNSAFE_FILENAME_CHARS.sub("-", (title or "").strip())
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-.")
    if len(cleaned) > MAX_FILENAME_STEM:
        # 从右侧的连字符处截断，避免把词/短语切成半截。
        head = cleaned[:MAX_FILENAME_STEM]
        cut = head.rfind("-")
        cleaned = (head[:cut] if cut >= MAX_FILENAME_STEM // 2 else head).strip("-")
    return cleaned


def export_filename(
    title: str,
    *,
    fmt: str,
    suffix: str,
    document_version: int | None,
    created_at: datetime | None = None,
) -> str:
    """可读文件名：`标题-v2-20260726.pdf`。

    `paperforge-v1.pdf` 在下载目录里既认不出是哪篇论文，也认不出是哪一次导出——
    同一天导出三次就是三个 `paperforge-v1(1).pdf`。
    """
    parts = [_slugify_title(title) or "paperforge"]
    tag = _FORMAT_FILENAME_TAG.get(fmt)
    if tag:
        parts.append(tag)
    parts.append(f"v{document_version or 1}")
    if created_at is not None:
        parts.append(created_at.strftime("%Y%m%d"))
    return f"{'-'.join(parts)}.{suffix}"


def _content_disposition(mode: str, filename: str) -> str:
    """RFC 6266 / 5987 头部。

    HTTP 头只能承载 latin-1，中文标题因此必须走 ``filename*``；``filename``
    保留纯 ASCII 兜底（老浏览器与 curl -OJ）。把中文塞进 ``filename``
    会让 ASGI 编码头部时直接抛 UnicodeEncodeError——下载整个 500。
    """
    ascii_fallback = re.sub(r"[^A-Za-z0-9._-]+", "-", filename)
    ascii_fallback = re.sub(r"-{2,}", "-", ascii_fallback).strip("-.")
    if not re.match(r"^[A-Za-z0-9]", ascii_fallback):
        # 纯中文标题会被压成 `v2-20260726.pdf` 之类，加回可识别的前缀。
        ascii_fallback = f"paperforge-{ascii_fallback}"
    header = f'{mode}; filename="{ascii_fallback}"'
    if filename != ascii_fallback:
        header += f"; filename*=UTF-8''{quote(filename, safe='')}"
    return header


_MEDIA_TYPES = {
    "pdf": ("application/pdf", "pdf"),
    "latex_zip": ("application/zip", "zip"),
    "markdown": ("text/markdown; charset=utf-8", "md"),
    "markdown_bundle": ("application/zip", "zip"),
    "bibtex": ("application/x-bibtex; charset=utf-8", "bib"),
    "docx": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "docx",
    ),
    # 编译日志要能在浏览器里直接打开看，不该强制下载成二进制。
    "compile_log": ("text/plain; charset=utf-8", "log"),
}


# ---- M5：雪球 / 全文 / 质量 / 润色 ----


@router.post(
    "/projects/{project_id}/snowball",
    response_model=JobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_snowball(
    project_id: str,
    request: SnowballRequest,
    session: SessionDep,
    queue: QueueDep,
) -> JobResponse:
    """引文雪球扩展：邻居入库为 candidate，需用户圈选后才进写作白名单。"""
    project = await _require_project(session, project_id)
    job = await start_job(
        session,
        queue,
        project_id=project.id,
        kind="search",
        function="run_snowball_pipeline",
        direction=request.direction,
        max_seeds=request.max_seeds,
    )
    return _job_response(job)


@router.post(
    "/projects/{project_id}/ingest",
    response_model=JobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_ingest(
    project_id: str,
    request: IngestRequest,
    session: SessionDep,
    queue: QueueDep,
) -> JobResponse:
    """OA 全文获取 → 解析 → 全文级卡片。只走 OA/官方渠道，不绕 paywall。"""
    project = await _require_project(session, project_id)
    job = await start_job(
        session,
        queue,
        project_id=project.id,
        kind="ingest",
        function="run_ingest_pipeline",
        max_works=request.max_works,
    )
    return _job_response(job)


@router.post(
    "/projects/{project_id}/quality/generate",
    response_model=JobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_quality(
    project_id: str,
    session: SessionDep,
    queue: QueueDep,
    request: GenerationOptionsRequest | None = None,
) -> JobResponse:
    project = await _require_project(session, project_id)
    options = request or GenerationOptionsRequest()
    job = await start_job(
        session,
        queue,
        project_id=project.id,
        kind="write",
        function="run_quality_pipeline",
        quality_profile=options.quality_profile,
        review_style=options.review_style,
    )
    return _job_response(job)


@router.post(
    "/projects/{project_id}/quality/repair",
    response_model=JobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_quality_repair(
    project_id: str,
    session: SessionDep,
    queue: QueueDep,
    request: GenerationOptionsRequest | None = None,
) -> JobResponse:
    """重新评估并对未通过的论断做有界补证据与局部修订。"""
    project = await _require_project(session, project_id)
    options = request or GenerationOptionsRequest()
    job = await start_job(
        session,
        queue,
        project_id=project.id,
        kind="write",
        function="run_quality_repair_pipeline",
        quality_profile=options.quality_profile,
        review_style=options.review_style,
    )
    return _job_response(job)


@router.get("/projects/{project_id}/quality", response_model=QualityResponse)
async def get_quality(
    project_id: str,
    session: SessionDep,
    quality_profile: Literal["draft", "scholarly", "submission"] = Query(default="scholarly"),
) -> QualityResponse:
    """同步计算质量报告的确定性部分（软校验需要 LLM，走任务）。"""
    from paperforge_worker.pipelines.quality import build_quality_report, count_words

    project = await _require_project(session, project_id)
    whitelist = await get_writing_whitelist(session, project.id)
    document = await latest_document(session, project.id)
    rows = await list_sections(session, document.id) if document else []
    persisted = await latest_quality_report(
        session,
        project.id,
        quality_profile=quality_profile,
    )
    current_hash = document_snapshot_hash(rows) if document else None
    if persisted is not None:
        stale = persisted.stale or persisted.paper_snapshot_hash != current_hash
        if stale and not persisted.stale:
            persisted.stale = True
            await session.flush()
        payload = dict(persisted.metrics_json or {})
        payload.update(
            {
                "report_id": str(persisted.id),
                "document_version": persisted.document_version,
                "paper_snapshot_hash": persisted.paper_snapshot_hash,
                "quality_profile": persisted.quality_profile,
                "review_style": persisted.review_style,
                "readiness_status": persisted.readiness_status,
                "stale": stale,
                "blockers": persisted.blockers_json or [],
                "warnings": persisted.warnings_json or [],
                "scores": persisted.scores_json or {},
                "layout_checks": persisted.layout_checks_json or {},
            }
        )
        return QualityResponse(project_id=str(project.id), **payload)
    entries = await list_entries(session, project.id, status="selected")
    cards = await get_cards(session, project.id)

    frame_keys = {"abstract", "introduction", "conclusion"}
    sections = [
        {
            "section_key": row.section_key,
            "title": row.title,
            "word_count": count_words(
                " ".join(
                    run.get("v", "")
                    for block in (row.body_ir_json or {}).get("blocks", [])
                    for run in block.get("runs", [])
                    if run.get("t") == "text"
                )
            ),
            "cite_keys": row.cite_keys_json or [],
            "kind": "frame" if row.section_key in frame_keys else "body",
        }
        for row in rows
    ]
    fulltext_used = sum(1 for card in cards.values() if card.fulltext_used)
    report = build_quality_report(
        sections=sections,
        whitelist_size=len(whitelist),
        publication_years=[w.publication_year for _e, w in entries if w.publication_year],
        fulltext_coverage=(fulltext_used / len(entries)) if entries else 0.0,
        scope=dict(project.scope_json or {}),
    )
    report.document_version = document.version if document else None
    report.paper_snapshot_hash = current_hash
    report.quality_profile = quality_profile
    report.readiness_status = "unassessed"
    return QualityResponse(project_id=str(project.id), **report.to_payload())


@router.get(
    "/projects/{project_id}/quality/evidence",
    response_model=list[ClaimEvidenceResponse],
)
async def get_claim_evidence(
    project_id: str,
    session: SessionDep,
    report_id: str | None = Query(default=None),
    core_only: bool = Query(default=False),
) -> list[ClaimEvidenceResponse]:
    project = await _require_project(session, project_id)
    try:
        selected_report_id = uuid.UUID(report_id) if report_id else None
    except ValueError as error:
        raise HTTPException(status_code=404, detail="quality report not found") from error
    if selected_report_id is None:
        report = await latest_quality_report(session, project.id)
        selected_report_id = report.id if report else None
    if selected_report_id is None:
        return []
    rows = await list_claim_evidence(
        session,
        project.id,
        quality_report_id=selected_report_id,
        core_only=core_only,
    )
    return [_claim_evidence_response(row) for row in rows]


@router.patch(
    "/projects/{project_id}/quality/evidence/{anchor_id}",
    response_model=ClaimEvidenceResponse,
)
async def review_claim_evidence(
    project_id: str,
    anchor_id: str,
    request: ReviewClaimEvidenceRequest,
    session: SessionDep,
) -> ClaimEvidenceResponse:
    from db import set_claim_manual_status

    project = await _require_project(session, project_id)
    try:
        anchor_uuid = uuid.UUID(anchor_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail="evidence anchor not found") from error
    anchor = await session.get(ClaimEvidenceAnchor, anchor_uuid)
    if anchor is None or anchor.project_id != project.id:
        raise HTTPException(status_code=404, detail="evidence anchor not found")
    await set_claim_manual_status(session, anchor, request.manual_status)
    return _claim_evidence_response(anchor)


@router.post("/projects/{project_id}/sections/{section_key}/refine", response_model=RefineResponse)
async def refine_text(
    project_id: str,
    section_key: str,
    request: RefineRequest,
    session: SessionDep,
) -> RefineResponse:
    """编辑器润色浮条（polisher 角色）。

    红线：润色**不得**新增引用键、不得改动任何数字——改动后一旦发现数字变化，
    直接放弃改写并原样返回（宁可不润色，也不让数字漂移）。
    """
    from paperforge_worker.pipelines.writing import extract_numbers

    from paperforge_api.config import get_settings as api_settings

    await _require_project(session, project_id)
    del section_key
    original = request.text.strip()
    if not original:
        raise HTTPException(status_code=422, detail="text must not be empty")

    async with accounted_runner(
        session, api_settings().llm_config(), project_id=project_id
    ) as runner:
        if not runner.enabled:
            return RefineResponse(
                action=request.action,
                original=original,
                refined=original,
                changed=False,
                note="未配置 LLM provider：润色不可用，已原样返回",
            )

        goals = {
            "polish": "润色文字，使表达更准确流畅",
            "expand": "在不引入新事实的前提下展开论述",
            "shorten": "精简表达，保留全部论点",
            "academic_tone": "调整为学术书面语气",
        }
        response = await runner.agenerate(
            "polisher",
            system_prompt=(
                "你是学术论文润色助手。只输出改写后的正文纯文本，不要解释。"
                "绝对禁止：新增或修改任何数字、新增引用标记、引入原文没有的事实。"
            ),
            user_prompt=(
                f"目标：{goals.get(request.action, '润色')}\n"
                f"额外要求：{request.instruction or '无'}\n\n原文：\n{original}"
            ),
            max_output_tokens=2000,
            temperature=0.3,
            metadata={"stage": "refine", "action": request.action},
        )
        if response is None or not response.text.strip():
            return RefineResponse(
                action=request.action,
                original=original,
                refined=original,
                changed=False,
                note="润色调用失败，已原样返回",
            )

        refined = response.text.strip()
        if extract_numbers(refined) != extract_numbers(original):
            # 数字红线优先于文采。
            return RefineResponse(
                action=request.action,
                original=original,
                refined=original,
                changed=False,
                note="改写改动了正文数字，已放弃本次润色",
            )
        return RefineResponse(
            action=request.action,
            original=original,
            refined=refined,
            changed=refined != original,
        )


@router.post(
    "/projects/{project_id}/sections/{section_key}/rewrite-candidate",
    response_model=RewriteSectionCandidateResponse,
)
async def rewrite_section_candidate(
    project_id: str,
    section_key: str,
    request: RewriteSectionRequest,
    session: SessionDep,
) -> RewriteSectionCandidateResponse:
    """只生成候选，不写数据库；引用、数字与素材引用必须原样守恒。"""
    from paperforge_worker.pipelines.writing import extract_numbers

    from paperforge_api.config import get_settings as api_settings

    project = await _require_project(session, project_id)
    document = await latest_document(session, project.id)
    row = (
        await get_section(session, document_id=document.id, section_key=section_key)
        if document
        else None
    )
    if row is None:
        raise HTTPException(status_code=404, detail="section not found")
    require_section_unchanged(row, request.expected_updated_at)
    original = IRSection(**(row.body_ir_json or {}))
    async with accounted_runner(
        session, api_settings().llm_config(), project_id=project.id
    ) as runner:
        if not runner.enabled:
            return RewriteSectionCandidateResponse(
                section_key=section_key,
                original_body_ir=original.model_dump(mode="json"),
                candidate_body_ir=original.model_dump(mode="json"),
                changed=False,
                checks={"citations": "pass", "numbers": "pass", "assets": "pass"},
                note="未配置 LLM provider，未生成候选",
            )
        response = await runner.agenerate_json(
            "writer",
            system_prompt=(
                "你是学术论文单章重写助手。返回完整 Section JSON。只能修改 text run 的 v 字段；"
                "所有 cite/grounding/xref/math_inline run、非文字 block、key、title "
                "和结构必须保留。"
                "禁止新增、删除或修改任何数字、引用键、证据 id、素材引用与图表。"
            ),
            user_prompt=(
                f"用户要求：{request.instruction}\n"
                f"允许的证据引用（仅作范围说明，不得新增）：{request.allowed_evidence_refs}\n"
                f"原章节 JSON：{original.model_dump_json()}"
            ),
            max_output_tokens=8000,
            temperature=0.2,
            metadata={"stage": "rewrite_section", "section_key": section_key},
        )
        if not response.ok or not isinstance(response.value, dict):
            raise HTTPException(
                status_code=502,
                detail={
                    "code": "rewrite_generation_failed",
                    "message": response.error or "模型未返回有效候选",
                },
            )
        try:
            candidate = IRSection(**response.value)
        except Exception as error:  # noqa: BLE001
            raise HTTPException(
                status_code=422, detail={"code": "rewrite_invalid_ir", "message": str(error)}
            ) from error
        checks = _rewrite_invariant_checks(original, candidate, extract_numbers=extract_numbers)
        if any(value != "pass" for value in checks.values()):
            raise HTTPException(
                status_code=422, detail={"code": "rewrite_quality_failed", "checks": checks}
            )
        return RewriteSectionCandidateResponse(
            section_key=section_key,
            original_body_ir=original.model_dump(mode="json"),
            candidate_body_ir=candidate.model_dump(mode="json"),
            changed=candidate != original,
            checks=checks,
        )


@router.post(
    "/projects/{project_id}/sections/{section_key}/rewrite-accept",
    response_model=AcceptSectionRewriteResponse,
)
async def accept_section_rewrite(
    project_id: str,
    section_key: str,
    request: AcceptSectionRewriteRequest,
    session: SessionDep,
) -> AcceptSectionRewriteResponse:
    """接受后复制整份文档到新版本；失败或冲突时当前版本完全不动。"""
    from db import invalidate_quality_reports_for_project
    from paperforge_worker.pipelines.writing import extract_numbers

    project = await _require_project(session, project_id)
    document = await latest_document(session, project.id)
    if document is None:
        raise HTTPException(status_code=404, detail="document not generated yet")
    source_rows = await list_sections(session, document.id)
    source = next((item for item in source_rows if item.section_key == section_key), None)
    if source is None:
        raise HTTPException(status_code=404, detail="section not found")
    require_section_unchanged(source, request.expected_updated_at)
    original = IRSection(**(source.body_ir_json or {}))
    try:
        candidate = IRSection(**request.candidate_body_ir)
    except Exception as error:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"invalid section IR: {error}") from error
    checks = _rewrite_invariant_checks(original, candidate, extract_numbers=extract_numbers)
    if any(value != "pass" for value in checks.values()):
        raise HTTPException(
            status_code=422, detail={"code": "rewrite_quality_failed", "checks": checks}
        )

    whitelist = await get_writing_whitelist(session, project.id)
    new_document = await create_document(
        session,
        project_id=project.id,
        outline_id=document.outline_id,
        status="draft",
    )
    accepted_row = None
    for row in source_rows:
        ir = candidate if row.section_key == section_key else IRSection(**(row.body_ir_json or {}))
        cite_keys = sorted(
            PaperIR(meta=PaperMeta(title=project.title), sections=[ir]).collect_cite_keys()
        )
        asset_refs = sorted(
            PaperIR(meta=PaperMeta(title=project.title), sections=[ir]).collect_asset_refs()
        )
        copied = await upsert_section(
            session,
            document_id=new_document.id,
            section_key=row.section_key,
            title=ir.title,
            parent_key=row.parent_key,
            order_no=row.order_no,
            body_ir=ir.model_dump(mode="json"),
            cite_keys=cite_keys,
            asset_refs=asset_refs,
            status="edited" if row.section_key == section_key else row.status,
            model=row.model,
        )
        await replace_citation_usage(
            session,
            project_id=project.id,
            section_id=copied.id,
            usages=[
                {"work_id": whitelist[key], "cite_key": key, "context_snippet": None}
                for key in cite_keys
                if key in whitelist
            ],
        )
        if row.section_key == section_key:
            accepted_row = copied
    await invalidate_quality_reports_for_project(session, project.id)
    assert accepted_row is not None
    return AcceptSectionRewriteResponse(
        document_version=new_document.version, section=_section_response(accepted_row)
    )


def _rewrite_invariant_checks(
    original: IRSection, candidate: IRSection, *, extract_numbers: Any
) -> dict[str, str]:
    original_ir = PaperIR(meta=PaperMeta(title="check"), sections=[original])
    candidate_ir = PaperIR(meta=PaperMeta(title="check"), sections=[candidate])
    original_payload = original.model_dump(mode="json")
    candidate_payload = candidate.model_dump(mode="json")

    def text_values(value: Any) -> list[str]:
        if isinstance(value, dict):
            own = [str(value.get("v") or "")] if value.get("t") == "text" else []
            return own + [text for child in value.values() for text in text_values(child)]
        if isinstance(value, list):
            return [text for child in value for text in text_values(child)]
        return []

    def protected_shape(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: "<editable-text>"
                if value.get("t") == "text" and key == "v"
                else protected_shape(child)
                for key, child in value.items()
            }
        if isinstance(value, list):
            return [protected_shape(child) for child in value]
        return value

    original_text = " ".join(text_values(original_payload))
    candidate_text = " ".join(text_values(candidate_payload))
    return {
        "structure": (
            "pass"
            if protected_shape(original_payload) == protected_shape(candidate_payload)
            else "fail"
        ),
        "citations": "pass"
        if original_ir.collect_cite_keys() == candidate_ir.collect_cite_keys()
        else "fail",
        "numbers": "pass"
        if extract_numbers(original_text) == extract_numbers(candidate_text)
        else "fail",
        "assets": "pass"
        if original_ir.collect_asset_refs() == candidate_ir.collect_asset_refs()
        else "fail",
    }
