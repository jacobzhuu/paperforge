"""Authenticated task inspection and evidence search."""

import uuid
from typing import Annotated

from arq.connections import ArqRedis
from db.repositories.agent_trace import task_trace
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from paperforge_api.deps import (
    authorize_project_request,
    get_authorized_project,
    get_queue,
    get_session,
)

router = APIRouter(
    prefix="/api/v1", tags=["agent"], dependencies=[Depends(authorize_project_request)]
)
SessionDep = Annotated[AsyncSession, Depends(get_session, scope="function")]


@router.get("/projects/{project_id}/jobs/{job_id}/trace")
async def get_task_trace(project_id: str, job_id: str, session: SessionDep):
    project = await get_authorized_project(session, project_id)
    try:
        identifier = uuid.UUID(job_id)
    except ValueError as error:
        raise HTTPException(404, "job not found") from error
    result = await task_trace(session, project.id, identifier)
    if result is None:
        raise HTTPException(404, "job not found")
    from paperforge_api.config import get_settings

    result["currency"] = get_settings().llm_price_currency
    return result


@router.get("/projects/{project_id}/evidence/search")
async def search_evidence(
    project_id: str,
    session: SessionDep,
    q: Annotated[str, Query(min_length=2, max_length=500)],
    limit: Annotated[int, Query(ge=1, le=40)] = 12,
    mode: str = "hybrid",
):
    from retrieval.search import search

    project = await get_authorized_project(session, project_id)
    if mode not in {"lexical", "vector", "hybrid", "hybrid_rerank"}:
        raise HTTPException(422, "invalid search mode")
    return await search(session, project.id, q, limit=limit, mode=mode)


@router.post("/projects/{project_id}/evidence/index", status_code=202)
async def index_evidence(
    project_id: str,
    session: SessionDep,
    queue: Annotated[ArqRedis | None, Depends(get_queue)],
):
    from retrieval.models import enabled

    from paperforge_api.jobs import start_job
    from paperforge_api.routers.projects import _job_response

    project = await get_authorized_project(session, project_id)
    if not enabled():
        raise HTTPException(503, "local embedding model is not enabled")
    job = await start_job(
        session,
        queue,
        project_id=project.id,
        kind="evidence",
        function="run_evidence_index_pipeline",
    )
    return _job_response(job)
