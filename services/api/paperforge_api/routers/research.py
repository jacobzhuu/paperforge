"""Authenticated descriptive-analysis runs and snapshot-bound proposal commits."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from typing import Annotated, Literal

from arq.connections import ArqRedis
from db import (
    create_document,
    document_snapshot_hash,
    get_writing_whitelist,
    latest_document,
    list_sections,
    replace_citation_usage,
    resume_checkpoint,
    upsert_section,
)
from db.models.paper import CitationUsage, GenerationJob, PaperDocument, UserAsset
from db.models.research import ResearchAnalysis
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from storage import make_object_store

from paperforge_api.config import get_settings
from paperforge_api.deps import (
    authorize_project_request,
    get_authorized_project,
    get_queue,
    get_session,
)
from paperforge_api.jobs import ensure_project_job_slot, start_job

router = APIRouter(
    prefix="/api/v1", tags=["research"], dependencies=[Depends(authorize_project_request)]
)
Session = Annotated[AsyncSession, Depends(get_session, scope="function")]
Queue = Annotated[ArqRedis | None, Depends(get_queue)]


class AnalysisSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sheet: str | None = Field(default=None, max_length=128)
    columns: list[str] = Field(default_factory=list, max_length=20)
    group_by: str | None = Field(default=None, max_length=128)
    units: dict[str, str] = Field(default_factory=dict, max_length=20)
    confirmed: bool = False


class ResearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal: str = Field(min_length=1, max_length=2000)
    asset_id: uuid.UUID
    section_key: str | None = Field(default=None, max_length=128)
    spec: AnalysisSpec = Field(default_factory=AnalysisSpec)
    engine: Literal["pi", "deterministic"] = "pi"


class ResearchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    interrupt_id: str
    spec: AnalysisSpec


class ProposalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    choice: Literal["accept", "reject"]


async def get_analysis(session, project_id, analysis_id, *, lock=False):
    query = select(ResearchAnalysis).where(
        ResearchAnalysis.project_id == project_id, ResearchAnalysis.id == analysis_id
    )
    if lock:
        query = query.with_for_update()
    item = await session.scalar(query)
    if item is None:
        raise HTTPException(404, "analysis not found")
    return item


def payload(item):
    return {
        "id": str(item.id),
        "job_id": str(item.job_id),
        "status": item.status,
        "input": item.input_json,
        "result": item.result_json,
        "proposal": item.proposal_json,
        "document_id": str(item.committed_document_id) if item.committed_document_id else None,
    }


@router.post("/projects/{project_id}/research-runs", status_code=202)
async def start_research(project_id: str, body: ResearchRequest, session: Session, queue: Queue):
    project = await get_authorized_project(session, project_id)
    asset = await session.get(UserAsset, body.asset_id)
    if asset is None or asset.project_id != project.id or not asset.object_key:
        raise HTTPException(404, "asset not found")
    if not (asset.title or "").lower().endswith((".csv", ".tsv", ".xlsx")):
        raise HTTPException(422, "请选择 CSV、TSV 或 XLSX 素材")
    store = await asyncio.to_thread(make_object_store, get_settings())
    content = await asyncio.to_thread(store.get, asset.object_key)
    document = await latest_document(session, project.id)
    sections = await list_sections(session, document.id) if document else []
    if body.section_key and not any(s.section_key == body.section_key for s in sections):
        raise HTTPException(422, "目标章节不存在")
    identifier = uuid.uuid4()
    inputs = {
        **body.model_dump(mode="json"),
        "source_hash": hashlib.sha256(content).hexdigest(),
        "document_id": str(document.id) if document else None,
        "document_version": document.version if document else None,
        "snapshot_hash": document_snapshot_hash(sections) if document else None,
        "graph_version": "research-v1",
        "model_calls": 0,
        "model": get_settings().llm_config().model_for_role("planner"),
    }
    job = await start_job(
        session,
        queue,
        project_id=project.id,
        kind="research",
        function="run_research_pipeline",
        analysis_id=str(identifier),
    )
    item = ResearchAnalysis(
        id=identifier, project_id=project.id, job_id=job.id, status="pending", input_json=inputs
    )
    session.add(item)
    await session.flush()
    return payload(item)


@router.get("/projects/{project_id}/research-runs")
async def list_research(project_id: str, session: Session):
    project = await get_authorized_project(session, project_id)
    rows = await session.scalars(
        select(ResearchAnalysis)
        .where(ResearchAnalysis.project_id == project.id)
        .order_by(ResearchAnalysis.created_at.desc())
        .limit(50)
    )
    results = []
    for row in rows:
        value = payload(row)
        if row.status in {"pending", "running"}:
            job = await session.get(GenerationJob, row.job_id)
            if job and job.status in {"failed", "cancelled", "paused"}:
                value["status"] = job.status
        results.append(value)
    return results


@router.post("/projects/{project_id}/research-runs/{job_id}/responses", status_code=202)
async def respond(
    project_id: str, job_id: uuid.UUID, body: ResearchResponse, session: Session, queue: Queue
):
    project = await get_authorized_project(session, project_id)
    # Project-first lock order matches every artifact writer.
    await ensure_project_job_slot(session, project.id, queue)
    item = await session.scalar(
        select(ResearchAnalysis)
        .where(ResearchAnalysis.project_id == project.id, ResearchAnalysis.job_id == job_id)
        .with_for_update()
    )
    if item is None:
        raise HTTPException(404, "analysis not found")
    pending = (item.result_json or {}).get("interrupt_id")
    if item.status != "needs_input" or pending != body.interrupt_id:
        raise HTTPException(409, "response is stale or already consumed")
    if not body.spec.confirmed or (not body.spec.columns and not body.spec.sheet):
        raise HTTPException(422, "请确认数值字段、分组及单位")
    original = await session.get(GenerationJob, item.job_id)
    inputs = {**item.input_json, "spec": body.spec.model_dump(), "response": body.interrupt_id}
    job = await start_job(
        session,
        queue,
        project_id=project.id,
        kind="research",
        function="run_research_pipeline",
        analysis_id=str(item.id),
        checkpoint=resume_checkpoint(original),
    )
    item.job_id, item.input_json, item.status = job.id, inputs, "pending"
    return payload(item)


@router.get("/projects/{project_id}/analysis-results/{result_id}")
async def result(project_id: str, result_id: uuid.UUID, session: Session):
    project = await get_authorized_project(session, project_id)
    item = await get_analysis(session, project.id, result_id)
    value = payload(item)
    if item.status in {"pending", "running"}:
        job = await session.get(GenerationJob, item.job_id)
        if job and job.status in {"failed", "cancelled", "paused"}:
            value["status"] = job.status
    return value


@router.get("/projects/{project_id}/analysis-results/{result_id}/chart")
async def chart(project_id: str, result_id: uuid.UUID, session: Session):
    project = await get_authorized_project(session, project_id)
    item = await get_analysis(session, project.id, result_id)
    key = (item.result_json or {}).get("chart_key")
    if not key:
        raise HTTPException(404, "chart not available")
    store = await asyncio.to_thread(make_object_store, get_settings())
    return Response(
        await asyncio.to_thread(store.get, key),
        media_type="image/png",
        headers={"Cache-Control": "private, no-store"},
    )


@router.post("/projects/{project_id}/artifact-proposals/{proposal_id}/decision")
async def decide(
    project_id: str, proposal_id: uuid.UUID, body: ProposalDecision, session: Session, queue: Queue
):
    project = await get_authorized_project(session, project_id)
    await ensure_project_job_slot(session, project.id, queue)
    item = await get_analysis(session, project.id, proposal_id, lock=True)
    if item.status in {"accepted", "rejected"}:
        if item.status != ("accepted" if body.choice == "accept" else "rejected"):
            raise HTTPException(409, "proposal already decided")
        return payload(item)
    if item.status != "proposed" or not item.proposal_json:
        raise HTTPException(409, "no validated proposal")
    if body.choice == "reject":
        item.status = "rejected"
        return payload(item)
    base = await latest_document(session, project.id)
    if base:
        await session.scalar(
            select(PaperDocument.id).where(PaperDocument.id == base.id).with_for_update()
        )
    sections = await list_sections(session, base.id) if base else []
    if (str(base.id) if base else None) != item.input_json["document_id"] or (
        document_snapshot_hash(sections) if base else None
    ) != item.input_json["snapshot_hash"]:
        raise HTTPException(409, "文稿已变化，请基于当前版本重新分析")
    asset = await session.get(UserAsset, uuid.UUID(item.input_json["asset_id"]))
    if asset is None or asset.project_id != project.id or not asset.object_key:
        raise HTTPException(409, "原素材已删除")
    store = await asyncio.to_thread(make_object_store, get_settings())
    content = await asyncio.to_thread(store.get, asset.object_key)
    if hashlib.sha256(content).hexdigest() != item.input_json["source_hash"]:
        raise HTTPException(409, "原素材已变化")
    from ingest.research import digest
    from paper_ir.schema import Section

    proposal = item.proposal_json
    if digest(proposal["body"]) != proposal["body_hash"]:
        raise HTTPException(409, "proposal integrity failure")
    Section.model_validate(proposal["body"])
    whitelist = set(await get_writing_whitelist(session, project.id))
    if any(key not in whitelist for section in sections for key in (section.cite_keys_json or [])):
        raise HTTPException(409, "引用白名单已变化，请重新核验")
    for ref in proposal["asset_refs"]:
        derived = await session.get(UserAsset, uuid.UUID(ref))
        if derived is None or derived.project_id != project.id:
            raise HTTPException(409, "分析产物已删除，请重新分析")
    document = await create_document(
        session, project_id=project.id, outline_id=base.outline_id if base else None
    )
    for section in sections:
        target = section.section_key == proposal["section_key"]
        created = await upsert_section(
            session,
            document_id=document.id,
            section_key=section.section_key,
            title=section.title,
            order_no=section.order_no,
            parent_key=section.parent_key,
            body_ir=proposal["body"] if target else section.body_ir_json,
            cite_keys=section.cite_keys_json,
            asset_refs=list(
                dict.fromkeys(
                    (section.asset_refs_json or []) + (proposal["asset_refs"] if target else [])
                )
            ),
            status="edited" if target else section.status,
            model=section.model,
        )
        usages = await session.scalars(
            select(CitationUsage).where(CitationUsage.section_id == section.id)
        )
        await replace_citation_usage(
            session,
            project_id=project.id,
            section_id=created.id,
            usages=[
                {"work_id": u.work_id, "cite_key": u.cite_key, "context_snippet": u.context_snippet}
                for u in usages
            ],
        )
    item.status, item.committed_document_id = "accepted", document.id
    await session.flush()
    return payload(item)


@router.get("/projects/{project_id}/analysis-results/{result_id}/reproducibility")
async def reproducibility(project_id: str, result_id: uuid.UUID, session: Session):
    project = await get_authorized_project(session, project_id)
    item = await get_analysis(session, project.id, result_id)
    key = (item.result_json or {}).get("bundle_key")
    if not key:
        raise HTTPException(404, "reproducibility bundle not available")
    store = await asyncio.to_thread(make_object_store, get_settings())
    return Response(
        await asyncio.to_thread(store.get, key),
        media_type="application/zip",
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": 'attachment; filename="analysis-reproducibility.zip"',
        },
    )


@router.post("/projects/{project_id}/research-runs/{job_id}/resume", status_code=202)
async def resume_research(project_id: str, job_id: uuid.UUID, session: Session, queue: Queue):
    project = await get_authorized_project(session, project_id)
    await ensure_project_job_slot(session, project.id, queue)
    item = await session.scalar(
        select(ResearchAnalysis)
        .where(ResearchAnalysis.project_id == project.id, ResearchAnalysis.job_id == job_id)
        .with_for_update()
    )
    job = await session.get(GenerationJob, job_id)
    if item is None or job is None:
        raise HTTPException(404, "analysis not found")
    if item.status == "needs_input":
        raise HTTPException(409, "请先确认分析口径")
    if job.status not in {"paused", "failed"} or item.status in {
        "proposed",
        "completed",
        "accepted",
        "rejected",
    }:
        raise HTTPException(409, "该任务不能继续")
    resumed = await start_job(
        session,
        queue,
        project_id=project.id,
        kind="research",
        function="run_research_pipeline",
        analysis_id=str(item.id),
        checkpoint=resume_checkpoint(job),
    )
    item.job_id, item.status = resumed.id, "pending"
    return payload(item)
