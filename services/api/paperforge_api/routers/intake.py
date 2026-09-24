"""Recoverable intent interpretation for new projects."""

from __future__ import annotations

import uuid
from typing import Annotated

from arq.connections import ArqRedis
from db import list_assets
from db.intake import INTAKE_KEY, material_issues, type_is_locked
from db.models.paper import GenerationJob, PaperProject
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from paperforge_api.deps import authorize_project_request, get_queue, get_session
from paperforge_api.deps import get_authorized_project as require_project
from paperforge_api.jobs import start_job
from paperforge_api.routers.projects import _job_response
from paperforge_api.schemas import IntakeRequest, JobResponse

router = APIRouter(
    prefix="/api/v1", tags=["intake"], dependencies=[Depends(authorize_project_request)]
)
SessionDep = Annotated[AsyncSession, Depends(get_session, scope="function")]
QueueDep = Annotated[ArqRedis | None, Depends(get_queue)]


@router.get("/projects/{project_id}/intake")
async def get_intake(project_id: str, session: SessionDep) -> dict:
    project = await require_project(session, project_id)
    state = dict((project.scope_json or {}).get(INTAKE_KEY) or {})
    if state.get("job_id") and state.get("status") == "running":
        job = await session.get(GenerationJob, uuid.UUID(state["job_id"]))
        if job and job.status not in {"running", "queued"}:
            state.update(status="failed", error="需求理解已中断，项目和材料已保留，可重新尝试。")
    if state.get("status") == "ready" and project.paper_type == "original":
        state["material_issues"] = material_issues(await list_assets(session, project.id))
    return {**state, "type_locked": await type_is_locked(session, project.id)}


@router.post("/projects/{project_id}/intake", response_model=JobResponse, status_code=202)
async def submit_intake(
    project_id: str,
    request: IntakeRequest,
    session: SessionDep,
    queue: QueueDep,
) -> JobResponse:
    project = await require_project(session, project_id)
    project = await session.scalar(
        select(PaperProject)
        .where(PaperProject.id == project.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    state = dict((project.scope_json or {}).get(INTAKE_KEY) or {})
    if not state:
        raise HTTPException(409, "该项目使用原有研究流程")
    # A lost HTTP response may be retried with the same base version. Return the
    # durable job, rather than paying twice or starting a second full pipeline.
    previous = (
        await session.get(GenerationJob, uuid.UUID(state["job_id"]))
        if state.get("job_id")
        else None
    )
    submitted = request.model_dump(mode="json")
    if (
        previous
        and previous.status in {"queued", "running"}
        and state.get("last_request") == submitted
    ):
        return _job_response(previous)
    if request.version != state.get("version", 0):
        raise HTTPException(409, "研究需求已更新，请刷新后再试")
    if previous and previous.status in {"queued", "running"}:
        raise HTTPException(409, "需求任务仍在运行，请等待完成或停止后修改")
    locked = await type_is_locked(session, project.id)
    overrides = {**state.get("overrides", {}), **request.overrides.model_dump(exclude_none=True)}
    if locked:
        if overrides.get("paper_type", project.paper_type) != project.paper_type:
            raise HTTPException(409, "检索或写作已经开始，不能改变论文类型；请新建项目")
        overrides["paper_type"] = project.paper_type
    topic = (
        request.topic if request.topic is not None else (project.scope_json or {}).get("topic", "")
    )
    answers = [*state.get("answers", [])]
    if request.answer and request.answer.strip():
        answers.append(request.answer.strip())
    version = state.get("version", 0) + 1
    job = await start_job(
        session,
        queue,
        project_id=project.id,
        kind="intake",
        function="run_intake_pipeline",
        version=version,
        auto_continue=not locked,
        checkpoint={"intake_version": version},
    )
    project.scope_json = {
        **(project.scope_json or {}),
        "topic": topic,
        INTAKE_KEY: {
            **state,
            "version": version,
            "status": "running",
            "overrides": overrides,
            "answers": answers,
            "job_id": str(job.id),
            "last_request": submitted,
            "error": None,
            "questions": [],
        },
    }
    await session.flush()
    return _job_response(job)
