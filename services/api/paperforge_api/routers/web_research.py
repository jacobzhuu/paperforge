"""Authorized, project-scoped web research jobs and source snapshots."""

import uuid
from typing import Annotated

from arq.connections import ArqRedis
from db.models.paper import GenerationJob
from db.models.web_research import WebResearchRun, WebResearchSource
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from paperforge_api.config import get_settings
from paperforge_api.deps import (
    authorize_project_request,
    get_authorized_project,
    get_queue,
    get_session,
)
from paperforge_api.jobs import start_job
from paperforge_api.routers.projects import _job_response
from paperforge_api.schemas import JobResponse

router = APIRouter(
    prefix="/api/v1/projects/{project_id}/web-research",
    tags=["web-research"],
    dependencies=[Depends(authorize_project_request)],
)
SessionDep = Annotated[AsyncSession, Depends(get_session, scope="function")]
QueueDep = Annotated[ArqRedis | None, Depends(get_queue)]


def _run_payload(run, job_status=None):
    status = run.status
    if status == "running" and job_status in {"paused", "cancelled", "failed"}:
        status = job_status
    return {
        "id": str(run.id),
        "job_id": str(run.job_id) if run.job_id else None,
        "status": status,
        "queries": run.plan_json["queries"],
        "calls": run.calls,
        "pricing": "unknown",
        "error": run.error,
        "created_at": run.created_at,
        "finished_at": run.finished_at,
    }


@router.post("/runs", response_model=JobResponse, status_code=202)
async def refresh(project_id: str, session: SessionDep, queue: QueueDep):
    project = await get_authorized_project(session, project_id)
    if not get_settings().mcp_web_enabled:
        raise HTTPException(503, detail="web research is unavailable")
    if not project.web_research_enabled:
        raise HTTPException(409, detail="enable web research for this project first")
    job = await start_job(
        session,
        queue,
        project_id=project.id,
        kind="web_research",
        function="run_web_research_pipeline",
    )
    return _job_response(job)


@router.get("/runs")
async def list_runs(
    project_id: str,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    project = await get_authorized_project(session, project_id)
    rows = await session.execute(
        select(WebResearchRun, GenerationJob.status)
        .outerjoin(GenerationJob, GenerationJob.id == WebResearchRun.job_id)
        .where(WebResearchRun.project_id == project.id)
        .order_by(WebResearchRun.created_at.desc(), WebResearchRun.id)
        .limit(limit)
        .offset(offset)
    )
    return {
        "available": get_settings().mcp_web_enabled,
        "runs": [_run_payload(row, job_status) for row, job_status in rows],
    }


@router.get("/runs/{run_id}")
async def get_run(project_id: str, run_id: uuid.UUID, session: SessionDep):
    project = await get_authorized_project(session, project_id)
    run = await session.scalar(
        select(WebResearchRun).where(
            WebResearchRun.id == run_id,
            WebResearchRun.project_id == project.id,
        )
    )
    if run is None:
        raise HTTPException(404, detail="research run not found")
    sources = await session.scalars(
        select(WebResearchSource)
        .where(
            WebResearchSource.run_id == run.id,
            WebResearchSource.project_id == project.id,
        )
        .order_by(WebResearchSource.order_no, WebResearchSource.id)
        .limit(15)
    )
    job_status = await session.scalar(
        select(GenerationJob.status).where(GenerationJob.id == run.job_id)
    )
    return {
        **_run_payload(run, job_status),
        "sources": [
            {
                "id": str(row.id),
                "url": row.url,
                "title": row.title,
                "snippet": row.snippet,
                "body": row.body,
                "status": row.status,
                "fetched_at": row.fetched_at,
                "verification": row.verification_json,
            }
            for row in sources
        ],
    }
