from __future__ import annotations

import hashlib
import json
import uuid
from typing import Annotated, Any, Literal

from arq.connections import ArqRedis
from db import (
    add_visual_source,
    create_job,
    create_visual,
    get_section,
    get_visual,
    latest_document,
    list_assets,
    list_sections,
    list_visuals,
    upsert_section,
    visual_input_hash,
)
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from paper_ir import FigureBlock, PaperIR, PaperMeta
from paper_ir.schema import Section as IRSection
from sqlalchemy.ext.asyncio import AsyncSession
from storage import make_object_store
from visuals import ChartSpec, parse_visual_spec

from paperforge_api.config import get_settings
from paperforge_api.deps import (
    authorize_project_request,
    get_queue,
    get_session,
)
from paperforge_api.deps import get_authorized_project as _require_project
from paperforge_api.schemas import (
    ApproveVisualRequest,
    CreateVisualRequest,
    JobResponse,
    RegenerateVisualRequest,
    UpdateVisualRequest,
    VisualResponse,
)

router = APIRouter(
    prefix="/api/v1", tags=["visuals"], dependencies=[Depends(authorize_project_request)]
)
SessionDep = Annotated[AsyncSession, Depends(get_session)]
QueueDep = Annotated[ArqRedis | None, Depends(get_queue)]


@router.post(
    "/projects/{project_id}/visuals/suggest",
    response_model=JobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def suggest(project_id: str, session: SessionDep, queue: QueueDep) -> JobResponse:
    project = await _require_project(session, project_id)
    _require_visuals_enabled()
    await _require_queue(queue)
    job = await create_job(session, project_id=project.id, kind="visual")
    await queue.enqueue_job("run_visual_suggest_pipeline", str(project.id), str(job.id))
    return _job_response(job)


@router.get("/projects/{project_id}/visuals", response_model=list[VisualResponse])
async def get_visuals(
    project_id: str,
    session: SessionDep,
    kind: Literal["chart", "diagram", "ai_image"] | None = Query(default=None),
    generation_status: Literal["proposed", "queued", "running", "ready", "failed"] | None = Query(
        default=None
    ),
    review_status: Literal["pending", "approved", "rejected"] | None = Query(default=None),
) -> list[VisualResponse]:
    project = await _require_project(session, project_id)
    rows = await list_visuals(
        session,
        project.id,
        kind=kind,
        generation_status=generation_status,
        review_status=review_status,
    )
    return [_visual_response(project.id, row) for row in rows]


@router.post(
    "/projects/{project_id}/visuals",
    response_model=VisualResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_visual_endpoint(
    project_id: str,
    request: CreateVisualRequest,
    session: SessionDep,
) -> VisualResponse:
    project = await _require_project(session, project_id)
    _require_visuals_enabled()
    try:
        spec = parse_visual_spec(request.spec)
    except Exception as error:  # noqa: BLE001 - pydantic contract error -> 422
        raise HTTPException(status_code=422, detail=str(error)) from error
    document = await latest_document(session, project.id)
    visual = await create_visual(
        session,
        project_id=project.id,
        kind=spec.kind,
        spec=spec.model_dump(mode="json"),
        title=request.title,
        caption=request.caption,
        alt_text=request.alt_text,
        target_section_key=request.target_section_key,
        suggested_block_index=request.suggested_block_index,
        document_version=document.version if document else None,
    )
    if isinstance(spec, ChartSpec):
        asset, digest = await _resolve_chart_source(session, project.id, spec.source_asset_ref)
        await add_visual_source(session, visual_id=visual.id, user_asset=asset, source_hash=digest)
    return _visual_response(project.id, visual)


@router.patch("/projects/{project_id}/visuals/{visual_id}", response_model=VisualResponse)
async def update_visual_endpoint(
    project_id: str,
    visual_id: str,
    request: UpdateVisualRequest,
    session: SessionDep,
) -> VisualResponse:
    project = await _require_project(session, project_id)
    visual = await _require_visual(session, project.id, visual_id)
    if visual.review_status != "pending":
        raise HTTPException(status_code=409, detail="approved or rejected visual is immutable")
    sent = request.model_fields_set
    if "spec" in sent:
        if visual.generation_status not in {"proposed", "failed"}:
            raise HTTPException(status_code=409, detail="use regenerate after a preview exists")
        if request.spec is None:
            raise HTTPException(status_code=422, detail="spec cannot be null")
        try:
            spec = parse_visual_spec(request.spec)
        except Exception as error:  # noqa: BLE001
            raise HTTPException(status_code=422, detail=str(error)) from error
        if spec.kind != visual.kind:
            raise HTTPException(status_code=409, detail="visual kind cannot change")
        if isinstance(spec, ChartSpec):
            current = ChartSpec.model_validate(visual.spec_json)
            if spec.source_asset_ref != current.source_asset_ref:
                raise HTTPException(
                    status_code=409,
                    detail="use regenerate when changing a chart source asset",
                )
        visual.spec_json = spec.model_dump(mode="json")
        visual.input_hash = visual_input_hash(visual.spec_json)
        visual.generation_status = "proposed"
        visual.error_code = None
        visual.error_message = None
    for field in (
        "title",
        "caption",
        "alt_text",
        "target_section_key",
        "suggested_block_index",
    ):
        if field in sent:
            setattr(visual, field, getattr(request, field))
    await session.flush()
    return _visual_response(project.id, visual)


@router.post(
    "/projects/{project_id}/visuals/{visual_id}/generate",
    response_model=JobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def generate(
    project_id: str,
    visual_id: str,
    session: SessionDep,
    queue: QueueDep,
) -> JobResponse:
    project = await _require_project(session, project_id)
    _require_visuals_enabled()
    visual = await _require_visual(session, project.id, visual_id)
    if visual.review_status != "pending" or visual.generation_status in {"queued", "running"}:
        raise HTTPException(
            status_code=409, detail="visual cannot be generated in its current state"
        )
    if visual.kind == "ai_image" and not get_settings().ai_images_enabled:
        raise HTTPException(status_code=409, detail="AI image generation is disabled")
    await _require_queue(queue)
    job = await create_job(session, project_id=project.id, kind="visual")
    visual.generation_status = "queued"
    await queue.enqueue_job(
        "run_visual_generate_pipeline",
        str(project.id),
        str(job.id),
        visual_id=str(visual.id),
    )
    return _job_response(job)


@router.post("/projects/{project_id}/visuals/{visual_id}/approve", response_model=VisualResponse)
async def approve(
    project_id: str,
    visual_id: str,
    request: ApproveVisualRequest,
    session: SessionDep,
) -> VisualResponse:
    project = await _require_project(session, project_id)
    visual = await _require_visual(session, project.id, visual_id)
    if visual.review_status == "approved":
        return _visual_response(project.id, visual)
    if visual.review_status != "pending" or visual.generation_status != "ready":
        raise HTTPException(status_code=409, detail="only ready pending visuals can be approved")
    if not visual.caption.strip() or not visual.alt_text.strip():
        raise HTTPException(status_code=422, detail="caption and alt_text are required")
    document = await latest_document(session, project.id)
    if document is None:
        raise HTTPException(status_code=409, detail="generate the paper before inserting visuals")
    rows = await list_sections(session, document.id)
    target = await get_section(session, document_id=document.id, section_key=request.section_key)
    if target is None:
        raise HTTPException(status_code=404, detail="target section not found")

    sections = [IRSection(**row.body_ir_json) for row in rows if row.body_ir_json]
    asset_ref = f"va_{str(visual.id)[:8]}"
    if any(
        isinstance(block, FigureBlock) and block.asset_ref == asset_ref
        for section in sections
        for block in section.blocks
    ):
        visual.review_status = "approved"
        return _visual_response(project.id, visual)

    replacement_ref = f"va_{str(visual.supersedes_id)[:8]}" if visual.supersedes_id else None
    target_ir = next((section for section in sections if section.key == target.section_key), None)
    if target_ir is None:
        raise HTTPException(status_code=422, detail="target section has invalid or missing IR")
    replaced = False
    if replacement_ref:
        for section in sections:
            for index, block in enumerate(section.blocks):
                if isinstance(block, FigureBlock) and block.asset_ref == replacement_ref:
                    section.blocks[index] = _figure_block(visual, asset_ref)
                    target_ir = section
                    target = next(row for row in rows if row.section_key == section.key)
                    replaced = True
                    break
            if replaced:
                break
    if not replaced:
        if request.block_index < 0 or request.block_index > len(target_ir.blocks):
            raise HTTPException(status_code=422, detail="block_index is outside the section")
        target_ir.blocks.insert(request.block_index, _figure_block(visual, asset_ref))

    try:
        whole = PaperIR(meta=PaperMeta(title=project.title), sections=sections)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    target_ir = next(section for section in whole.sections if section.key == target.section_key)
    section_only = PaperIR(meta=PaperMeta(title=project.title), sections=[target_ir])
    await upsert_section(
        session,
        document_id=document.id,
        section_key=target.section_key,
        title=target.title,
        order_no=target.order_no,
        body_ir=target_ir.model_dump(mode="json"),
        cite_keys=sorted(section_only.collect_cite_keys()),
        asset_refs=sorted(section_only.collect_asset_refs()),
        parent_key=target.parent_key,
        status="edited",
        model=target.model,
    )
    visual.review_status = "approved"
    visual.target_section_key = target.section_key
    visual.suggested_block_index = request.block_index
    await session.flush()
    return _visual_response(project.id, visual)


@router.post("/projects/{project_id}/visuals/{visual_id}/reject", response_model=VisualResponse)
async def reject(project_id: str, visual_id: str, session: SessionDep) -> VisualResponse:
    project = await _require_project(session, project_id)
    visual = await _require_visual(session, project.id, visual_id)
    if visual.review_status == "approved":
        raise HTTPException(status_code=409, detail="approved visual cannot be rejected")
    visual.review_status = "rejected"
    await session.flush()
    return _visual_response(project.id, visual)


@router.post(
    "/projects/{project_id}/visuals/{visual_id}/regenerate",
    response_model=VisualResponse,
    status_code=status.HTTP_201_CREATED,
)
async def regenerate(
    project_id: str,
    visual_id: str,
    request: RegenerateVisualRequest,
    session: SessionDep,
) -> VisualResponse:
    project = await _require_project(session, project_id)
    old = await _require_visual(session, project.id, visual_id)
    payload = request.spec or old.spec_json
    try:
        spec = parse_visual_spec(payload)
    except Exception as error:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=str(error)) from error
    if spec.kind != old.kind:
        raise HTTPException(status_code=409, detail="visual kind cannot change across revisions")
    new = await create_visual(
        session,
        project_id=project.id,
        kind=old.kind,
        spec=spec.model_dump(mode="json"),
        title=old.title,
        caption=request.caption if request.caption is not None else old.caption,
        alt_text=request.alt_text if request.alt_text is not None else old.alt_text,
        target_section_key=old.target_section_key,
        suggested_block_index=old.suggested_block_index,
        document_version=old.document_version,
        version=old.version + 1,
        supersedes_id=old.id,
        figure_label=old.figure_label,
    )
    if isinstance(spec, ChartSpec):
        asset, digest = await _resolve_chart_source(session, project.id, spec.source_asset_ref)
        await add_visual_source(session, visual_id=new.id, user_asset=asset, source_hash=digest)
    return _visual_response(project.id, new)


@router.get("/projects/{project_id}/visuals/{visual_id}/renditions/{format}")
async def rendition(
    project_id: str,
    visual_id: str,
    format: str,
    session: SessionDep,
    disposition: str = "inline",
) -> Response:
    project = await _require_project(session, project_id)
    visual = await _require_visual(session, project.id, visual_id)
    if format not in {"svg", "pdf", "png"}:
        raise HTTPException(status_code=404, detail="rendition not found")
    item = (visual.renditions_json or {}).get(format)
    if not isinstance(item, dict) or not item.get("object_key"):
        raise HTTPException(status_code=404, detail="rendition not found")
    store = make_object_store(get_settings())
    try:
        content = store.get(item["object_key"])
    except (FileNotFoundError, ValueError) as error:
        raise HTTPException(status_code=410, detail="rendition payload is gone") from error
    media_type = {"svg": "image/svg+xml", "pdf": "application/pdf", "png": "image/png"}[format]
    mode = "attachment" if disposition == "attachment" else "inline"
    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": f'{mode}; filename="visual-{visual.id}.{format}"',
            "Cache-Control": "private, max-age=3600",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _figure_block(visual: Any, asset_ref: str) -> FigureBlock:
    width = visual.spec_json.get("width", "column")
    return FigureBlock(
        asset_ref=asset_ref,
        caption=visual.caption,
        alt_text=visual.alt_text,
        label=visual.figure_label,
        width=width,
    )


async def _resolve_chart_source(
    session: AsyncSession, project_id: uuid.UUID, asset_ref: str
) -> tuple[Any, str]:
    prefix = asset_ref.removeprefix("ua_")
    matches = [
        asset
        for asset in await list_assets(session, project_id)
        if str(asset.id).startswith(prefix)
    ]
    if len(matches) != 1 or not isinstance(matches[0].parsed_json, dict):
        raise HTTPException(
            status_code=422, detail="chart source does not resolve to one parsed project asset"
        )
    asset = matches[0]
    if not asset.parsed_json.get("headers") or not asset.parsed_json.get("rows"):
        raise HTTPException(status_code=422, detail="chart source must be a parsed CSV/XLSX table")
    if asset.object_key:
        try:
            source = make_object_store(get_settings()).get(asset.object_key)
        except (FileNotFoundError, ValueError) as error:
            raise HTTPException(status_code=410, detail="chart source payload is gone") from error
    else:
        source = json.dumps(asset.parsed_json, sort_keys=True).encode()
    return asset, hashlib.sha256(source).hexdigest()


def _visual_response(project_id: uuid.UUID, visual: Any) -> VisualResponse:
    renditions = {}
    for fmt, item in (visual.renditions_json or {}).items():
        if fmt not in {"svg", "pdf", "png"} or not isinstance(item, dict):
            continue
        renditions[fmt] = {
            **item,
            "url": f"/api/v1/projects/{project_id}/visuals/{visual.id}/renditions/{fmt}",
        }
    return VisualResponse(
        id=str(visual.id),
        asset_ref=f"va_{str(visual.id)[:8]}",
        kind=visual.kind,
        generation_status=visual.generation_status,
        review_status=visual.review_status,
        title=visual.title,
        caption=visual.caption,
        caption_hint=_chart_caption_hint(visual.spec_json),
        alt_text=visual.alt_text,
        target_section_key=visual.target_section_key,
        suggested_block_index=visual.suggested_block_index,
        figure_label=visual.figure_label,
        spec=visual.spec_json,
        provider=visual.provider,
        model=visual.model,
        error_code=visual.error_code,
        error_message=visual.error_message,
        renditions=renditions,
        input_hash=visual.input_hash,
        content_hash=visual.content_hash,
        version=visual.version,
        supersedes_id=str(visual.supersedes_id) if visual.supersedes_id else None,
        created_at=visual.created_at,
    )


def _chart_caption_hint(spec: dict[str, Any]) -> str | None:
    if spec.get("kind") != "chart":
        return None
    notes: list[str] = []
    filters = spec.get("filters")
    if isinstance(filters, list) and filters:
        notes.append(f"已显式应用 {len(filters)} 个过滤条件")
    aggregation = spec.get("aggregation")
    if aggregation and aggregation != "none":
        notes.append(f"按 {aggregation} 聚合")
    sort = spec.get("sort")
    if sort and sort != "none":
        notes.append("横轴升序排列" if sort == "asc" else "横轴降序排列")
    if not notes:
        return None
    return "建议在图注中说明数据处理：" + "；".join(notes) + "。"


async def _require_visual(session: AsyncSession, project_id: uuid.UUID, visual_id: str):
    try:
        visual_uuid = uuid.UUID(visual_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail="visual not found") from error
    visual = await get_visual(session, visual_uuid)
    if visual is None or visual.project_id != project_id:
        raise HTTPException(status_code=404, detail="visual not found")
    return visual


def _require_visuals_enabled() -> None:
    if not get_settings().visuals_enabled:
        raise HTTPException(status_code=404, detail="visual generation is disabled")


async def _require_queue(queue: ArqRedis | None) -> None:
    if queue is None:
        raise HTTPException(status_code=503, detail="task queue is unavailable")


def _job_response(job: Any) -> JobResponse:
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
