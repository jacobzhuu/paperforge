"""任务入队的公共出口。

所有长任务都必须经由 ``start_job`` 建档 + 入队，而不是各路由自己 create_job 一次、
enqueue_job 一次。原因是「继续」：暂停的任务要能被重放，就得知道当初跑的是哪个管线、
带了什么参数。这份信息只有入队的那一刻知道，事后无从推断——所以在建 job 时就
一并写进 ``checkpoint_json[JOB_RESUME_KEY]``。
"""

from __future__ import annotations

import uuid
from typing import Any

from arq.connections import ArqRedis
from db import JOB_RESUME_KEY, create_job
from db.models.paper import GenerationJob, PaperProject
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


async def require_queue(queue: ArqRedis | None) -> ArqRedis:
    """Redis 不可用时 API 仍然启动（main.py 的 lifespan 容忍），到这里才 503。"""
    if queue is None:
        raise HTTPException(
            status_code=503,
            detail="task queue unavailable: worker Redis is not reachable",
        )
    return queue


async def start_job(
    session: AsyncSession,
    queue: ArqRedis | None,
    *,
    project_id: uuid.UUID,
    kind: str,
    function: str,
    checkpoint: dict[str, Any] | None = None,
    **kwargs: Any,
) -> GenerationJob:
    """建 generation_job 并入队，同时记下重放所需的 function/kwargs。"""
    ready = await require_queue(queue)
    await ensure_project_job_slot(session, project_id)
    job = await create_job(
        session,
        project_id=project_id,
        kind=kind,
        checkpoint={
            **(checkpoint or {}),
            JOB_RESUME_KEY: {"function": function, "kwargs": kwargs},
        },
    )
    await ready.enqueue_job(function, str(project_id), str(job.id), **kwargs)
    return job


async def ensure_project_job_slot(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> None:
    """Lock a project and reject overlapping artifact-mutating jobs."""
    # Serialize the check on the project row.  Without this lock two HTTP
    # requests can both observe "no active job" and then concurrently clear
    # the evidence matrix or write competing latest documents.
    await session.scalar(
        select(PaperProject.id).where(PaperProject.id == project_id).with_for_update()
    )
    active = await session.scalar(
        select(GenerationJob)
        .where(
            GenerationJob.project_id == project_id,
            GenerationJob.status.in_({"queued", "running"}),
        )
        .order_by(GenerationJob.created_at.desc())
        .limit(1)
    )
    if active is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "project_job_active",
                "message": "该项目已有会修改研究产物的任务正在运行",
                "job_id": str(active.id),
                "kind": active.kind,
                "status": active.status,
            },
        )
