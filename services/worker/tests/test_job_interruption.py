"""任务被掐断时的收尾（真实 Postgres）。

`_finish` 只在正常路径上跑。arq 的 job_timeout 走 `asyncio.wait_for`、worker 停机走
task.cancel()，两条路都绕开了 `_finish`——此前 generation_job 会永远停在 running：
前端的「进行中」不消失，用户也分不清是还在跑还是早就被掐了。
"""

from __future__ import annotations

import asyncio
import uuid

import httpx
import pytest
from db import create_job, create_project, create_user, list_job_events, update_job
from db.models.paper import GenerationJob
from paperforge_worker.config import WorkerSettings
from paperforge_worker.context import job_context
from scholar_gateway import InMemoryHttpCache


async def _project_and_job(session_factory) -> tuple[uuid.UUID, uuid.UUID]:
    async with session_factory() as session:
        owner = await create_user(
            session,
            email=f"{uuid.uuid4()}@example.test",
            password_hash="!test-only",
            verified=True,
        )
        project = await create_project(
            session,
            title="Interrupted run",
            paper_type="review",
            language="en",
            owner_id=owner.id,
        )
        job = await create_job(session, project_id=project.id, kind="full")
        await update_job(session, job, status="running", stage="write", progress=0.65)
        await session.commit()
        return project.id, job.id


def _kwargs(project_id: uuid.UUID, job_id: uuid.UUID, session_factory) -> dict:
    return {
        "project_id": project_id,
        "job_id": job_id,
        "settings": WorkerSettings(),
        "session_factory": session_factory,
        "http_client": httpx.Client(),
        "scholar_cache": InMemoryHttpCache(),
    }


async def _job_status(session_factory, job_id: uuid.UUID) -> GenerationJob:
    async with session_factory() as session:
        job = await session.get(GenerationJob, job_id)
        assert job is not None
        return job


async def test_cancelled_job_is_marked_failed(session_factory) -> None:
    """超时/停机（CancelledError）必须把任务落到 failed 并留下解释。"""
    project_id, job_id = await _project_and_job(session_factory)

    with pytest.raises(asyncio.CancelledError):
        async with job_context(**_kwargs(project_id, job_id, session_factory)):
            raise asyncio.CancelledError

    job = await _job_status(session_factory, job_id)
    assert job.status == "failed"
    assert job.error_json is not None
    assert job.error_json["reason"] == "interrupted"
    assert job.error_json["exception"] == "CancelledError"

    async with session_factory() as session:
        events = await list_job_events(session, job_id)
    assert [event.event_type for event in events] == ["job.interrupted"]


async def test_arq_job_timeout_path_marks_job_failed(session_factory) -> None:
    """走真实的超时路径：外部 cancel 而不是自己 raise。

    arq 用 `asyncio.wait_for` 掐任务，异常是从外面扔进来的——收尾代码此时跑在一个
    正在被取消的协程里，和 `raise CancelledError` 那条路径并不等价。
    """
    project_id, job_id = await _project_and_job(session_factory)

    async def _pipeline() -> None:
        async with job_context(**_kwargs(project_id, job_id, session_factory)):
            await asyncio.sleep(30)

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(_pipeline(), timeout=0.05)

    # 收尾在后台 task 里，给它几个循环把话说完。
    for _ in range(50):
        job = await _job_status(session_factory, job_id)
        if job.status != "running":
            break
        await asyncio.sleep(0.02)
    assert job.status == "failed"
    assert job.error_json["reason"] == "interrupted"


async def test_unexpected_exception_is_also_recorded(session_factory) -> None:
    """管线抛未捕获异常时同样收尾，不留 running 僵尸任务。"""
    project_id, job_id = await _project_and_job(session_factory)

    with pytest.raises(RuntimeError):
        async with job_context(**_kwargs(project_id, job_id, session_factory)):
            raise RuntimeError("boom")

    job = await _job_status(session_factory, job_id)
    assert job.status == "failed"
    assert job.error_json["exception"] == "RuntimeError"


async def test_already_finished_job_is_left_alone(session_factory) -> None:
    """已收过尾的任务不被改写。

    run_full_pipeline 里文献管线先跑完自己那段；若后续阶段抛异常就把一个已经
    succeeded 的任务翻成 failed，用户会以为已交付的文献库也没了。
    """
    project_id, job_id = await _project_and_job(session_factory)
    async with session_factory() as session:
        job = await session.get(GenerationJob, job_id)
        await update_job(session, job, status="succeeded")
        await session.commit()

    with pytest.raises(asyncio.CancelledError):
        async with job_context(**_kwargs(project_id, job_id, session_factory)):
            raise asyncio.CancelledError

    job = await _job_status(session_factory, job_id)
    assert job.status == "succeeded"
    assert job.error_json is None


async def test_normal_exit_does_not_touch_job_status(session_factory) -> None:
    """正常退出仍由 `_finish` 说了算。"""
    project_id, job_id = await _project_and_job(session_factory)

    async with job_context(**_kwargs(project_id, job_id, session_factory)) as context:
        assert context.job_id == job_id

    job = await _job_status(session_factory, job_id)
    assert job.status == "running"
    assert job.error_json is None
