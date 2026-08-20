"""任务的活着判据：分清「跑得慢」和「已经没人在跑」。

被硬杀掉的进程不会留下任何收尾代码——蓝绿发布换掉 worker 容器、OOM、SIGKILL 都是
这一类。此前这种行永远停在 running：前端进度卡一直走，`ensure_project_job_slot`
也一直用它把项目锁着。这些用例锁的就是那条判据本身。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from db.models.paper import GenerationJob
from db.repositories.jobs import (
    JOB_DISPATCH_GRACE_SECONDS,
    JOB_STALE_AFTER_SECONDS,
    job_is_abandoned,
    job_last_seen,
)

NOW = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)


def _job(*, status: str, created_ago: float, heartbeat_ago: float | None = None) -> GenerationJob:
    return GenerationJob(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        kind="full",
        status=status,
        progress=0.5,
        created_at=NOW - timedelta(seconds=created_ago),
        heartbeat_at=(None if heartbeat_ago is None else NOW - timedelta(seconds=heartbeat_ago)),
    )


def test_a_running_job_that_keeps_stamping_is_alive() -> None:
    job = _job(status="running", created_ago=6_000, heartbeat_ago=5)
    assert not job_is_abandoned(job, now=NOW)


def test_a_running_job_whose_process_stopped_stamping_is_abandoned() -> None:
    """运行中的任务一定在盖章，所以心跳停了就是进程没了——队列怎么说不作数。

    arq 的 in-progress 记录在容器被删掉之后依然留在 Redis 里，拿它当活着的证据
    等于永远判不出来。
    """
    job = _job(status="running", created_ago=100_000, heartbeat_ago=JOB_STALE_AFTER_SECONDS + 1)
    assert job_is_abandoned(job, now=NOW)
    assert job_is_abandoned(job, queue_knows=True, now=NOW)


def test_a_job_just_picked_up_is_not_judged_before_it_can_stamp() -> None:
    job = _job(status="running", created_ago=JOB_DISPATCH_GRACE_SECONDS - 1)
    assert not job_is_abandoned(job, now=NOW)


def test_a_queued_job_still_in_the_queue_is_alive_however_long_it_waits() -> None:
    """排队本身不是缺陷：没人盖章是因为还没人持有它，唯一可信的是队列里有没有它。"""
    job = _job(status="queued", created_ago=100_000)
    assert not job_is_abandoned(job, queue_knows=True, now=NOW)


def test_a_queued_job_the_queue_never_heard_of_is_abandoned() -> None:
    """行建好了、队列里没有——入队失败，或者入队的那套队列已经不在了。"""
    job = _job(status="queued", created_ago=JOB_DISPATCH_GRACE_SECONDS + 1)
    assert job_is_abandoned(job, queue_knows=False, now=NOW)


def test_a_queue_that_cannot_be_asked_produces_no_verdict() -> None:
    """Redis 抖一下不该把正在排队的任务判死：问不到不等于不存在。"""
    job = _job(status="queued", created_ago=100_000)
    assert not job_is_abandoned(job, queue_knows=None, now=NOW)


@pytest.mark.parametrize("status", ["succeeded", "failed", "cancelled", "paused", "needs_input"])
def test_a_finished_job_is_never_reopened(status: str) -> None:
    job = _job(status=status, created_ago=100_000)
    assert not job_is_abandoned(job, queue_knows=False, now=NOW)


def test_a_row_written_before_heartbeats_existed_ages_from_its_creation() -> None:
    """老行没有心跳列。回退到 created_at，而不是当成「刚刚还活着」。"""
    job = _job(status="running", created_ago=400_000)
    assert job_last_seen(job) == job.created_at
    assert job_is_abandoned(job, now=NOW)
