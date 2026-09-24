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
from arq.jobs import Job as ArqJob
from arq.jobs import JobStatus
from db import JOB_RESUME_KEY, abandon_job, create_job, job_is_abandoned
from db.execution_profile import EXECUTION_PROFILE_KEY, EXECUTION_PROFILES, source_execution_profile
from db.models.paper import GenerationJob, JobDispatch, PaperProject
from fastapi import HTTPException
from observability import get_logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = get_logger(__name__)


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
    from paperforge_api.config import get_settings

    pinned_engine = (
        (checkpoint or {}).get("semantic_repair_engine", "legacy")
        if checkpoint
        else get_settings().semantic_repair_engine
    )
    ready = await require_queue(queue)
    await ensure_project_job_slot(session, project_id, ready)
    project = await session.get(PaperProject, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    intake = (getattr(project, "scope_json", None) or {}).get("intake")
    if intake and intake.get("status") != "ready" and function != "run_intake_pipeline":
        raise HTTPException(409, "请先完成研究方向理解或澄清")
    execution_profile = (checkpoint or {}).get(EXECUTION_PROFILE_KEY, project.execution_profile)
    if execution_profile not in EXECUTION_PROFILES:
        raise ValueError(f"unsupported execution_profile: {execution_profile}")
    job = await create_job(
        session,
        project_id=project_id,
        kind=kind,
        checkpoint={
            "semantic_repair_engine": pinned_engine,
            "writer_polish_policy": (
                (checkpoint or {}).get("writer_polish_policy", "legacy")
                if checkpoint
                else get_settings().writer_polish_policy
            ),
            "writer_polish_concurrency": (
                (checkpoint or {}).get("writer_polish_concurrency", 2)
                if checkpoint
                else get_settings().writer_polish_concurrency
            ),
            "evidence_retrieval_mode": (
                (checkpoint or {}).get("evidence_retrieval_mode", "legacy")
                if checkpoint
                else get_settings().evidence_retrieval_mode
            ),
            EXECUTION_PROFILE_KEY: execution_profile,
            **(checkpoint or {}),
            JOB_RESUME_KEY: {"function": function, "kwargs": kwargs},
        },
    )
    from paperforge_api.dispatch import record_dispatch

    await record_dispatch(session, job, function, kwargs, ready)
    return job


async def retry_profile_checkpoint(
    session: AsyncSession, project_id: uuid.UUID, retry_of: str | None
) -> dict[str, str] | None:
    """Pin a stage rerun to its source job, even if the project default changed."""
    if retry_of is None:
        return None
    try:
        source_id = uuid.UUID(retry_of)
    except ValueError as error:
        raise HTTPException(status_code=404, detail="source job not found") from error
    source = await session.get(GenerationJob, source_id)
    if source is None or source.project_id != project_id:
        raise HTTPException(status_code=404, detail="source job not found")
    return {EXECUTION_PROFILE_KEY: source_execution_profile(source)}


async def queue_knows_job(queue: ArqRedis | None, job_id: uuid.UUID) -> bool | None:
    """队列里还有没有这条任务。``None`` = 现在问不到，别下结论。

    问不到（Redis 不可用、或这条任务是旧版本入队的、没带我们的 job id）与「不存在」
    必须分开：把前者当成后者，等于在 Redis 抖一下的时候把正在排队的任务判死。
    """
    if queue is None:
        return None
    try:
        status = await ArqJob(str(job_id), queue).status()
    except Exception:  # noqa: BLE001 - 队列查询失败只让判定弃权
        logger.warning("arq job status lookup failed", exc_info=True)
        return None
    return status is not JobStatus.not_found


async def reconcile_abandoned_jobs(
    session: AsyncSession,
    queue: ArqRedis | None,
    jobs: list[GenerationJob],
) -> set[uuid.UUID]:
    """把「没有任何进程在跑」的任务收进终态，返回被收掉的 id。

    放在读路径上而不是做成后台清道夫：判据要用到这个部署自己的队列，而蓝绿发布期间
    同一个数据库前面站着两套队列——谁看到的队列，谁才有资格对一条任务下结论。
    """
    abandoned: set[uuid.UUID] = set()
    for job in jobs:
        if job.status not in {"queued", "running"}:
            continue
        # A new API must never declare another deployment's queued work lost.
        # Durable intents are repaired by dispatch; legacy queue ownership is unknown.
        if job.status == "queued":
            continue
        if await session.get(JobDispatch, job.id) is not None:
            continue
        knows = None
        if not job_is_abandoned(job, queue_knows=knows):
            continue
        await abandon_job(session, job)
        abandoned.add(job.id)
    return abandoned


async def ensure_project_job_slot(
    session: AsyncSession,
    project_id: uuid.UUID,
    queue: ArqRedis | None = None,
) -> None:
    """Lock a project and reject overlapping artifact-mutating jobs."""
    # Serialize the check on the project row.  Without this lock two HTTP
    # requests can both observe "no active job" and then concurrently clear
    # the evidence matrix or write competing latest documents.
    await session.scalar(
        select(PaperProject).where(PaperProject.id == project_id).with_for_update()
        .execution_options(populate_existing=True)
    )
    open_jobs = list(
        (
            await session.scalars(
                select(GenerationJob)
                .where(
                    GenerationJob.project_id == project_id,
                    GenerationJob.status.in_({"queued", "running"}),
                )
                .order_by(GenerationJob.created_at.desc())
            )
        ).all()
    )
    # 被硬杀掉的任务留下的行不该继续占着这个项目的名额——那是「项目被自己的鬼魂
    # 锁死」的那条路：用户此后点什么都是 409，而且没有任何界面能解释为什么。
    dead = await reconcile_abandoned_jobs(session, queue, open_jobs)
    active = next((job for job in open_jobs if job.id not in dead), None)
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
