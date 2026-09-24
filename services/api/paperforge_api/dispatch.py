"""Commit-before-enqueue outbox with bounded admission and per-owner fairness."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime

from db.models.paper import GenerationJob, JobDispatch, PaperProject
from fastapi import HTTPException
from observability import get_logger
from sqlalchemy import func, select, text

from paperforge_api.config import get_settings

logger = get_logger(__name__)
# Short transaction locks, never held while an Agent executes.
_ADMISSION_LOCK = 70461001
_DISPATCH_LOCK = 70461002
_SHORT_FUNCTIONS = {"run_export_pipeline", "run_pdf_match_pipeline", "run_uploaded_pdf_pipeline"}


def lane(function):
    return "short" if get_settings().job_short_slots and function in _SHORT_FUNCTIONS else "long"


def queue_identity() -> str:
    return hashlib.sha256(get_settings().redis_url.encode()).hexdigest()


async def record_dispatch(session, job, function, kwargs, queue):
    settings = get_settings()
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _ADMISSION_LOCK})
    owner = await session.scalar(
        select(PaperProject.owner_id).where(PaperProject.id == job.project_id)
    )
    pending = (
        select(func.count())
        .select_from(GenerationJob)
        .where(GenerationJob.status.in_(["queued", "running"]), GenerationJob.id != job.id)
    )
    total = await session.scalar(pending.where(GenerationJob.status == "queued"))
    personal = await session.scalar(
        pending.join(PaperProject).where(PaperProject.owner_id == owner)
    )
    if total >= settings.job_pending_limit or personal >= settings.job_user_pending_limit:
        raise HTTPException(
            429,
            detail={
                "code": "job_capacity_exceeded",
                "message": "当前用户或系统的排队任务已达上限，请等待已有任务完成后重试",
            },
            headers={"Retry-After": "60"},
        )
    session.add(
        JobDispatch(
            job_id=job.id,
            owner_id=owner,
            queue_identity=queue_identity(),
            function=function,
            kwargs_json=kwargs,
        )
    )
    session.info["dispatch_queue"] = queue


async def dispatch_after_commit(session):
    queue = session.info.pop("dispatch_queue", None)
    if queue is not None:
        try:
            await dispatch_once(session, queue)
            await session.commit()
        except Exception:
            await session.rollback()
            logger.warning("dispatch deferred; committed intent retained", exc_info=True)


async def dispatch_once(session, queue):
    settings = get_settings()
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _DISPATCH_LOCK})
    active = list(
        (
            await session.execute(
                select(JobDispatch.owner_id, JobDispatch.queue_identity, JobDispatch.function)
                .join(GenerationJob, GenerationJob.id == JobDispatch.job_id)
                .where(
                    GenerationJob.status.in_(["queued", "running"]),
                    JobDispatch.dispatched_at.is_not(None),
                )
            )
        ).all()
    )
    busy = {row.owner_id for row in active}
    slots = {"long": settings.job_dispatch_slots, "short": settings.job_short_slots}
    for row in active:
        if row.queue_identity == queue_identity():
            slots[lane(row.function)] -= 1
    if max(slots.values()) <= 0:
        return 0
    candidates = list(
        (
            await session.scalars(
                select(JobDispatch)
                .join(GenerationJob, GenerationJob.id == JobDispatch.job_id)
                .where(
                    JobDispatch.queue_identity == queue_identity(),
                    JobDispatch.dispatched_at.is_(None),
                    GenerationJob.status == "queued",
                )
                .order_by(JobDispatch.created_at, JobDispatch.job_id)
                .limit(settings.job_pending_limit)
            )
        ).all()
    )
    # Owners with the oldest last grant go first; FIFO within each owner.
    grants = dict(
        (
            await session.execute(
                select(JobDispatch.owner_id, func.max(JobDispatch.dispatched_at))
                .where(JobDispatch.dispatched_at.is_not(None))
                .group_by(JobDispatch.owner_id)
            )
        ).all()
    )
    candidates.sort(key=lambda item: grants.get(item.owner_id) or datetime.min.replace(tzinfo=UTC))
    sent = 0
    for intent in candidates:
        selected_lane = lane(intent.function)
        if intent.owner_id in busy or slots[selected_lane] <= 0:
            continue
        job = await session.get(GenerationJob, intent.job_id)
        intent.attempts += 1
        try:
            # None means an earlier enqueue succeeded before its DB acknowledgement.
            await asyncio.wait_for(
                queue.enqueue_job(
                    intent.function,
                    str(job.project_id),
                    str(job.id),
                    _job_id=str(job.id),
                    **({"_queue_name": "arq:short"} if selected_lane == "short" else {}),
                    **intent.kwargs_json,
                ),
                timeout=5,
            )
        except Exception as error:
            intent.last_error = type(error).__name__
            break
        intent.dispatched_at = datetime.now(UTC)
        intent.last_error = None
        busy.add(intent.owner_id)
        sent += 1
        slots[selected_lane] -= 1
        if max(slots.values()) <= 0:
            break
    return sent


async def dispatch_loop(app):
    from paperforge_api.deps import create_arq_pool, get_session_factory

    if not get_settings().job_dispatch_enabled:
        return
    while True:
        try:
            if app.state.arq_pool is None:
                app.state.arq_pool = await create_arq_pool(get_settings())
            async with get_session_factory()() as session:
                await recover_dispatches(session, app.state.arq_pool)
                await session.commit()
                await dispatch_once(session, app.state.arq_pool)
                await session.commit()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("dispatch sweep failed", exc_info=True)
        await asyncio.sleep(2)


async def recover_dispatches(session, queue):
    """Only this deployment's durable records may be reconciled against its queue."""
    from arq.jobs import Job, JobStatus

    intents = list(
        (
            await session.scalars(
                select(JobDispatch)
                .join(GenerationJob, GenerationJob.id == JobDispatch.job_id)
                .where(
                    JobDispatch.queue_identity == queue_identity(),
                    GenerationJob.status.in_(["queued", "running"]),
                    JobDispatch.dispatched_at.is_not(None),
                )
            )
        ).all()
    )
    now = datetime.now(UTC)
    for candidate in intents:
        intent = await session.scalar(
            select(JobDispatch)
            .where(JobDispatch.job_id == candidate.job_id)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
        if intent is None:
            continue
        job = await session.get(GenerationJob, intent.job_id)
        if intent.executions and intent.lease_until is not None and intent.lease_until < now:
            # Invalidate first, then touch artifact/job rows: a stalled executor
            # may hold those rows while waiting to perform its commit fence.
            intent.execution_token = None
            await session.commit()
            from db.repositories.job_recovery import mark_execution_interrupted

            await mark_execution_interrupted(session, job, intent)
            continue
        if not intent.executions and (now - intent.dispatched_at).total_seconds() > 180:
            if (
                await asyncio.wait_for(
                    Job(
                        str(intent.job_id),
                        queue,
                        _queue_name="arq:short"
                        if lane(intent.function) == "short"
                        else "arq:queue",
                    ).status(),
                    3,
                )
                == JobStatus.not_found
            ):
                intent.dispatched_at = None
