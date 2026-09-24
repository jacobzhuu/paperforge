"""Execution leases and transactional fencing, independent of ARQ delivery."""

from __future__ import annotations

import asyncio
import uuid
from contextlib import suppress
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from functools import wraps

from db.models.paper import GenerationJob, JobDispatch
from sqlalchemy import func, select, update

_current: ContextVar[tuple | None] = ContextVar("execution_fence", default=None)
LEASE_SECONDS = 120


class ExecutionLost(BaseException):
    """Must escape pipeline exception-to-degradation handling."""


async def fence_commit(session):
    current = _current.get()
    if current is None:
        return
    job_id, token = current
    # Shared row lock allows concurrent stage transactions but fences lease takeover
    # until this transaction has committed. No lock is held during LLM calls.
    actual = await session.scalar(
        select(JobDispatch.execution_token)
        .where(JobDispatch.job_id == job_id, JobDispatch.lease_until > func.clock_timestamp())
        .with_for_update(read=True)
    )
    if actual != token:
        await session.rollback()
        raise ExecutionLost("execution lease expired or replaced")


async def renew(factory, job_id, token):
    while True:
        await asyncio.sleep(20)
        async with factory() as session:
            result = await session.execute(
                update(JobDispatch)
                .where(
                    JobDispatch.job_id == job_id,
                    JobDispatch.execution_token == token,
                    JobDispatch.lease_until > func.clock_timestamp(),
                )
                .values(lease_until=func.now() + timedelta(seconds=LEASE_SECONDS))
            )
            await session.commit()
            if not result.rowcount:
                raise ExecutionLost("cannot renew expired execution")


def guarded(function):
    @wraps(function)
    async def run(ctx, project_id, job_id, *args, **kwargs):
        factory = ctx["session_factory"]
        identifier = uuid.UUID(str(job_id))
        token = uuid.uuid4()
        async with factory() as session:
            intent = await session.scalar(
                select(JobDispatch).where(JobDispatch.job_id == identifier).with_for_update()
            )
            if intent is None:  # Legacy messages retain their original execution contract.
                managed = False
            else:
                managed = True
                job = await session.get(GenerationJob, identifier)
                if job is None or job.status not in {"queued", "running"}:
                    return
                now = datetime.now(UTC)
                if intent.lease_until is not None and intent.lease_until > now:
                    return  # Another delivery owns execution; never run the side effects twice.
                if intent.executions:
                    # Stage replay is not universally idempotent (especially PDF ingestion).
                    # Preserve checkpoints; require the existing explicit resume/retry action.
                    intent.execution_token = None
                    await session.commit()
                    from db.repositories.job_recovery import mark_execution_interrupted

                    await mark_execution_interrupted(session, job, intent)
                    await session.commit()
                    return
                intent.execution_token = token
                intent.lease_until = now + timedelta(seconds=LEASE_SECONDS)
                from observability.metrics import record_job_wait

                record_job_wait((now - job.created_at).total_seconds())
                intent.started_at = now
                intent.executions += 1
                await session.commit()
        if not managed:
            return await function(ctx, project_id, job_id, *args, **kwargs)
        marker = _current.set((identifier, token))
        renewal = asyncio.create_task(renew(factory, identifier, token))
        task = asyncio.create_task(function(ctx, project_id, job_id, *args, **kwargs))
        try:
            done, _ = await asyncio.wait({renewal, task}, return_when=asyncio.FIRST_COMPLETED)
            if renewal in done:
                task.cancel()
                await renewal
            return await task
        finally:
            task.cancel()
            # Paid calls drain and commit their ledger before relinquishing the fence.
            while not task.done():
                with suppress(asyncio.CancelledError, ExecutionLost, Exception):
                    await asyncio.shield(task)
            renewal.cancel()
            with suppress(asyncio.CancelledError, ExecutionLost, Exception):
                await renewal
            _current.reset(marker)
            async with factory() as session:
                await session.execute(
                    update(JobDispatch)
                    .where(
                        JobDispatch.job_id == identifier,
                        JobDispatch.execution_token == token,
                    )
                    .values(lease_until=func.now())
                )
                await session.commit()

    return run
