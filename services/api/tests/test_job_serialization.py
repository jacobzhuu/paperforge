from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from arq.jobs import JobStatus
from db.models.paper import GenerationJob
from db.repositories.jobs import JOB_ABANDONED_CODE, JOB_STALE_AFTER_SECONDS
from fastapi import HTTPException
from paperforge_api.jobs import ensure_project_job_slot


class _Session:
    """够用的假会话：锁项目行走 ``scalar``，取任务行走 ``scalars``。"""

    def __init__(self, jobs: list[GenerationJob] | None = None) -> None:
        self.project_locked = 0
        self.jobs = jobs or []
        self.flushes = 0

    async def scalar(self, statement):
        self.project_locked += 1
        return uuid4()

    async def scalars(self, statement):
        return SimpleNamespace(all=lambda: list(self.jobs))

    async def flush(self) -> None:
        self.flushes += 1


def _job(*, status: str, heartbeat_ago: float | None, kind: str = "full") -> GenerationJob:
    now = datetime.now(UTC)
    return GenerationJob(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        kind=kind,
        status=status,
        progress=0.5,
        created_at=now - timedelta(seconds=100_000),
        heartbeat_at=(None if heartbeat_ago is None else now - timedelta(seconds=heartbeat_ago)),
    )


async def test_project_job_slot_rejects_an_active_mutating_job() -> None:
    project_id = uuid4()
    active = _job(status="running", heartbeat_ago=5)
    session = _Session([active])

    with pytest.raises(HTTPException) as caught:
        await ensure_project_job_slot(session, project_id)  # type: ignore[arg-type]

    assert caught.value.status_code == 409
    assert caught.value.detail == {
        "code": "project_job_active",
        "message": "该项目已有会修改研究产物的任务正在运行",
        "job_id": str(active.id),
        "kind": "full",
        "status": "running",
    }


async def test_project_job_slot_allows_terminal_history() -> None:
    session = _Session([])
    await ensure_project_job_slot(session, uuid4())  # type: ignore[arg-type]
    assert session.project_locked == 1


async def test_a_job_whose_process_is_gone_does_not_hold_the_project_hostage() -> None:
    """僵尸行曾经把项目锁死：用户此后点什么都是 409，界面上也没有任何解释。

    实测 2026-08-15 的 6a6bbf18：12 条永远停在 queued 的行，把这个项目锁了四天半。
    """
    dead = _job(status="running", heartbeat_ago=JOB_STALE_AFTER_SECONDS + 60)
    session = _Session([dead])

    await ensure_project_job_slot(session, uuid4())  # type: ignore[arg-type]

    assert dead.status == "failed"
    assert dead.error_json is not None
    assert dead.error_json["code"] == JOB_ABANDONED_CODE
    assert dead.finished_at is not None


async def test_a_live_job_still_holds_the_slot_even_next_to_a_dead_one() -> None:
    """收尸不能顺手把还在跑的那条也收了。"""
    dead = _job(status="running", heartbeat_ago=JOB_STALE_AFTER_SECONDS + 60)
    live = _job(status="running", heartbeat_ago=1)
    session = _Session([live, dead])

    with pytest.raises(HTTPException) as caught:
        await ensure_project_job_slot(session, uuid4())  # type: ignore[arg-type]

    assert caught.value.detail["job_id"] == str(live.id)
    assert live.status == "running"
    assert dead.status == "failed"


class _Queue:
    def __init__(self, status: JobStatus) -> None:
        self.status = status
        self.asked: list[str] = []


async def test_a_queued_job_is_judged_by_the_queue_not_by_the_clock(monkeypatch) -> None:
    """排队等待多久都不算缺陷，队列里没有它才算。"""
    import paperforge_api.jobs as module

    waiting = _job(status="queued", heartbeat_ago=None)

    class _ArqJob:
        def __init__(self, job_id: str, redis) -> None:
            self.job_id = job_id

        async def status(self) -> JobStatus:
            return redis_status

    monkeypatch.setattr(module, "ArqJob", _ArqJob)

    redis_status = JobStatus.queued
    session = _Session([waiting])
    with pytest.raises(HTTPException):
        await ensure_project_job_slot(session, uuid4(), object())  # type: ignore[arg-type]
    assert waiting.status == "queued"

    redis_status = JobStatus.not_found
    session = _Session([waiting])
    await ensure_project_job_slot(session, uuid4(), object())  # type: ignore[arg-type]
    assert waiting.status == "failed"
    assert waiting.error_json["code"] == JOB_ABANDONED_CODE


async def test_a_queue_that_cannot_be_asked_leaves_the_job_alone(monkeypatch) -> None:
    """Redis 抖动时弃权：问不到不等于不存在。"""
    import paperforge_api.jobs as module

    class _Exploding:
        def __init__(self, job_id: str, redis) -> None:
            pass

        async def status(self) -> JobStatus:
            raise RuntimeError("redis down")

    monkeypatch.setattr(module, "ArqJob", _Exploding)
    waiting = _job(status="queued", heartbeat_ago=None)
    session = _Session([waiting])
    with pytest.raises(HTTPException):
        await ensure_project_job_slot(session, uuid4(), object())  # type: ignore[arg-type]
    assert waiting.status == "queued"
