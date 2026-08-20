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

    计时从**进入管线之后**才开始：`job_context` 的进入阶段要跑两次数据库往返，
    此前固定 0.05 秒的窗口经常在那之前就到点，测的就成了另一条路径（见下一个用例），
    数据库稍慢一点这个用例就红。
    """
    project_id, job_id = await _project_and_job(session_factory)
    entered = asyncio.Event()

    async def _pipeline() -> None:
        async with job_context(**_kwargs(project_id, job_id, session_factory)):
            entered.set()
            await asyncio.sleep(30)

    task = asyncio.ensure_future(_pipeline())
    await asyncio.wait_for(entered.wait(), timeout=10)

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(task, timeout=0.05)

    # 收尾在后台 task 里，给它几个循环把话说完。
    for _ in range(50):
        job = await _job_status(session_factory, job_id)
        if job.status != "running":
            break
        await asyncio.sleep(0.02)
    assert job.status == "failed"
    assert job.error_json["reason"] == "interrupted"


async def test_cancel_while_entering_the_context_also_marks_job_failed(session_factory) -> None:
    """取消落在**进入阶段**同样要收尾。

    `job_context` 在 yield 之前还要读项目与 checkpoint，两次数据库往返。worker 停机时
    的 task.cancel() 可能正好落在这个窗口里；此前那一段完全没有收尾代码，任务就永远
    停在 running——前端的「进行中」再也不消失，正是本模块要防的那种僵尸。
    """
    project_id, job_id = await _project_and_job(session_factory)

    async def _pipeline() -> None:
        async with job_context(**_kwargs(project_id, job_id, session_factory)):
            pytest.fail("进入阶段就该被取消，管线体不应执行")

    task = asyncio.ensure_future(_pipeline())
    # 让协程跑到第一个 await（进入阶段的数据库查询）上，再掐。
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

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


async def test_a_failed_job_records_the_stage_failure_it_already_reported(session_factory) -> None:
    """20 of 23 production failures had an empty ``error_json`` while the cause sat in the event
    stream: ``update_job`` skips a ``None`` error, and ``_finish`` passed None whenever the job
    had no warnings. The diagnosis existed and was dropped."""
    from paperforge_worker.worker import _finish

    project_id, job_id = await _project_and_job(session_factory)

    async with job_context(**_kwargs(project_id, job_id, session_factory)) as context:
        await context.emit(
            "visual_generate.completed",
            {"status": "failed", "error_code": "network_timeout"},
            stage="visual",
        )
        await _finish(context, delivered=False)

    job = await _job_status(session_factory, job_id)
    assert job.status == "failed"
    assert job.error_json is not None
    assert job.error_json["reason"] == "not_delivered"
    assert job.error_json["failure"]["error_code"] == "network_timeout"
    assert job.error_json["failure"]["event_type"] == "visual_generate.completed"
    assert job.error_json["failure"]["stage"] == "visual"


async def test_the_generic_terminal_event_does_not_overwrite_the_stage_failure(
    session_factory,
) -> None:
    """``job.finished`` also carries status="failed" but no cause; it must not win."""
    from paperforge_worker.worker import _finish

    project_id, job_id = await _project_and_job(session_factory)

    async with job_context(**_kwargs(project_id, job_id, session_factory)) as context:
        await context.emit(
            "search.completed",
            {"status": "failed", "error_code": "provider_unavailable"},
            stage="search",
        )
        await _finish(context, delivered=False)

    job = await _job_status(session_factory, job_id)
    assert job.error_json["failure"]["error_code"] == "provider_unavailable"


async def test_a_delivered_job_without_warnings_still_records_no_error(session_factory) -> None:
    """Success must stay clean: only non-delivery gets an explanatory payload."""
    from paperforge_worker.worker import _finish

    project_id, job_id = await _project_and_job(session_factory)

    async with job_context(**_kwargs(project_id, job_id, session_factory)) as context:
        await _finish(context, delivered=True)

    job = await _job_status(session_factory, job_id)
    assert job.status == "succeeded"
    assert job.error_json is None


async def test_a_running_job_keeps_stamping_that_its_process_is_alive(session_factory) -> None:
    """上面三条路都要求进程还能执行代码。硬杀不给这个机会。

    蓝绿发布把 worker 容器整个换掉、OOM、SIGKILL——收尾代码一行都不会跑，行就永远
    停在 running。心跳是那种情况下唯一还留下的证据：**跑着的时候**一直盖章，于是
    「盖章停了」才能被读到的人当成「没人在跑了」。
    """
    project_id, job_id = await _project_and_job(session_factory)

    before = await _job_status(session_factory, job_id)
    assert before.heartbeat_at is None

    async with job_context(**_kwargs(project_id, job_id, session_factory)):
        for _ in range(100):
            stamped = await _job_status(session_factory, job_id)
            if stamped.heartbeat_at is not None:
                break
            await asyncio.sleep(0.02)

    assert stamped.heartbeat_at is not None
    # 心跳只写它自己那一列：阶段和进度是别的写路径在管的，不能被顺手覆盖。
    assert stamped.stage == "write"
    assert stamped.progress == pytest.approx(0.65)
