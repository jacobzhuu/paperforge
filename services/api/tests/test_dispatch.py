from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from db import create_project, create_user
from db.models.paper import GenerationJob, JobDispatch
from fastapi import HTTPException
from paperforge_api import config
from paperforge_api.dispatch import dispatch_after_commit, dispatch_once, recover_dispatches
from paperforge_api.jobs import reconcile_abandoned_jobs, start_job
from paperforge_worker.execution import ExecutionLost, _current, fence_commit, guarded
from sqlalchemy import select


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    monkeypatch.setattr(config, "_settings", config.Settings(_env_file=None))


async def project(factory, owner=None):
    async with factory() as session:
        if owner is None:
            user = await create_user(
                session, email=f"{uuid.uuid4()}@test.invalid", password_hash="!"
            )
            owner = user.id
        item = await create_project(session, title="capacity", paper_type="review", owner_id=owner)
        await session.commit()
        return item.id, owner


class Queue:
    def __init__(self, factory, fail=False):
        self.factory = factory
        self.fail = fail
        self.ids = []

    async def enqueue_job(self, function, project_id, job_id, **kwargs):
        if self.fail:
            raise ConnectionError("injected")
        async with self.factory() as session:
            assert await session.get(GenerationJob, uuid.UUID(job_id)) is not None
            assert await session.get(JobDispatch, uuid.UUID(job_id)) is not None
        self.ids.append(job_id)
        return SimpleNamespace(job_id=job_id)


async def submit(factory, queue, project_id):
    async with factory() as session:
        job = await start_job(session, queue, project_id=project_id, kind="full", function="test")
        await session.commit()
        await dispatch_after_commit(session)
        return job.id


async def test_commit_before_visibility_and_retry_after_outage(session_factory):
    identifier, _ = await project(session_factory)
    queue = Queue(session_factory, fail=True)
    job_id = await submit(session_factory, queue, identifier)
    async with session_factory() as session:
        intent = await session.get(JobDispatch, job_id)
        assert intent.dispatched_at is None
        assert intent.last_error == "ConnectionError"
        assert (await session.get(GenerationJob, job_id)).status == "queued"
        queue.fail = False
        assert await dispatch_once(session, queue) == 1
        await session.commit()
    assert queue.ids == [str(job_id)]


async def test_rollback_does_not_publish(session_factory):
    identifier, _ = await project(session_factory)
    queue = Queue(session_factory)
    async with session_factory() as session:
        await start_job(session, queue, project_id=identifier, kind="full", function="test")
        await session.rollback()
    assert not queue.ids
    async with session_factory() as session:
        assert not list(await session.scalars(select(JobDispatch)))


async def test_one_owner_cannot_fill_all_slots(session_factory):
    first, owner = await project(session_factory)
    second, _ = await project(session_factory, owner)
    third, _ = await project(session_factory)
    queue = Queue(session_factory)
    a = await submit(session_factory, queue, first)
    b = await submit(session_factory, queue, second)
    c = await submit(session_factory, queue, third)
    assert queue.ids == [str(a), str(c)]
    async with session_factory() as session:
        (await session.get(GenerationJob, a)).status = "succeeded"
        await session.commit()
        await dispatch_once(session, queue)
        await session.commit()
    assert queue.ids[-1] == str(b)


async def test_user_admission_limit(session_factory):
    first, owner = await project(session_factory)
    queue = Queue(session_factory)
    await submit(session_factory, queue, first)
    for _ in range(2):
        identifier, _ = await project(session_factory, owner)
        await submit(session_factory, queue, identifier)
    identifier, _ = await project(session_factory, owner)
    with pytest.raises(HTTPException) as caught:
        await submit(session_factory, queue, identifier)
    assert caught.value.status_code == 429


async def test_legacy_queue_is_not_judged_by_new_deployment(session_factory):
    identifier, _ = await project(session_factory)
    async with session_factory() as session:
        job = GenerationJob(
            project_id=identifier,
            kind="full",
            status="queued",
            created_at=datetime.now(UTC) - timedelta(days=1),
        )
        session.add(job)
        await session.flush()
        assert await reconcile_abandoned_jobs(session, None, [job]) == set()


async def test_duplicate_execution_is_suppressed(session_factory):
    identifier, _ = await project(session_factory)
    job_id = await submit(session_factory, Queue(session_factory), identifier)
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []

    @guarded
    async def work(ctx, project_id, job_id):
        calls.append(job_id)
        entered.set()
        await release.wait()

    ctx = {"session_factory": session_factory}
    task = asyncio.create_task(work(ctx, str(identifier), str(job_id)))
    await entered.wait()
    await work(ctx, str(identifier), str(job_id))
    release.set()
    await task
    assert len(calls) == 1


async def test_expired_executor_cannot_commit_and_recovers_to_pause(session_factory):
    identifier, _ = await project(session_factory)
    job_id = await submit(session_factory, Queue(session_factory), identifier)
    token = uuid.uuid4()
    async with session_factory() as session:
        intent = await session.get(JobDispatch, job_id)
        intent.execution_token = token
        intent.executions = 1
        intent.lease_until = datetime.now(UTC) - timedelta(seconds=1)
        (await session.get(GenerationJob, job_id)).status = "running"
        await session.commit()
    marker = _current.set((job_id, token))
    try:
        async with session_factory() as session:
            job = await session.get(GenerationJob, job_id)
            job.progress = 0.9
            with pytest.raises(ExecutionLost):
                await fence_commit(session)
    finally:
        _current.reset(marker)
    async with session_factory() as session:
        await recover_dispatches(session, Queue(session_factory))
        await session.commit()
        job = await session.get(GenerationJob, job_id)
        assert job.status == "paused"
        assert job.progress == 0


async def test_recovery_does_not_overwrite_a_terminal_job(session_factory):
    from db.repositories.job_recovery import mark_execution_interrupted

    identifier, _ = await project(session_factory)
    job_id = await submit(session_factory, Queue(session_factory), identifier)
    async with session_factory() as session:
        job = await session.get(GenerationJob, job_id)
        job.status = "succeeded"
        await session.commit()
        intent = await session.get(JobDispatch, job_id)
        assert not await mark_execution_interrupted(session, job, intent)
        await session.commit()
        await session.refresh(job)
        assert job.status == "succeeded"


async def test_cancellation_keeps_execution_renewal_until_paid_work_drains(
    session_factory, monkeypatch
):
    identifier, _ = await project(session_factory)
    job_id = await submit(session_factory, Queue(session_factory), identifier)
    started, draining, release, stopped = (asyncio.Event() for _ in range(4))

    async def renewal(*args):
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr("paperforge_worker.execution.renew", renewal)

    @guarded
    async def work(ctx, project_id, job_id):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            draining.set()
            await release.wait()
            assert not stopped.is_set()
            async with session_factory() as session:
                await fence_commit(session)
                await session.commit()
            raise

    task = asyncio.create_task(work({"session_factory": session_factory}, identifier, job_id))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    await asyncio.wait_for(draining.wait(), 5)
    assert not task.done()
    assert not stopped.is_set()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    assert stopped.is_set()
