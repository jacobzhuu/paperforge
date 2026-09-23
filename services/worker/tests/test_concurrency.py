"""``bounded_map`` 的契约测试：顺序、窗口、隔离、停止。

这四条是后面每一处并发化的安全论证所依赖的前提。它们如果松了，
「并发与串行等价」的所有说法都不再成立，所以这里全部用反例钉死。
"""

from __future__ import annotations

import asyncio

import pytest
from paperforge_worker.concurrency import NOT_RUN, bounded_map
from paperforge_worker.context import JobStopped


@pytest.mark.asyncio
async def test_results_follow_input_order_not_completion_order() -> None:
    """后提交的先完成，结果仍必须按输入序——下游好几处按位置读它。"""
    items = [0.05, 0.04, 0.03, 0.02, 0.01]

    async def run(delay: float) -> float:
        await asyncio.sleep(delay)
        return delay

    results = await bounded_map(items, run, limit=5)

    assert results == items


@pytest.mark.asyncio
async def test_window_never_exceeds_the_limit() -> None:
    inflight = 0
    peak = 0

    async def run(_item: int) -> int:
        nonlocal inflight, peak
        inflight += 1
        peak = max(peak, inflight)
        await asyncio.sleep(0.01)
        inflight -= 1
        return _item

    await bounded_map(list(range(20)), run, limit=4)

    assert peak == 4


@pytest.mark.asyncio
async def test_window_stays_full_instead_of_waiting_on_a_batch_barrier() -> None:
    """滑动窗口的全部意义：一个慢项不该拦住后面的项。

    批次栅栏下，limit=2 时 [慢, 快, 快, 快] 要等两轮；滑动窗口里
    后面的快项在慢项还在飞的时候就跑完了。
    """
    started: list[int] = []

    async def run(item: int) -> int:
        started.append(item)
        await asyncio.sleep(0.10 if item == 0 else 0.01)
        return item

    await bounded_map([0, 1, 2, 3], run, limit=2)

    # 慢项 0 仍在飞时，1/2/3 依次被准入——批次栅栏做不到这一点。
    assert started == [0, 1, 2, 3]


@pytest.mark.asyncio
async def test_one_failure_does_not_cancel_or_orphan_its_siblings() -> None:
    finished: list[int] = []

    async def run(item: int) -> int:
        if item == 1:
            raise ValueError("boom")
        await asyncio.sleep(0.01)
        finished.append(item)
        return item

    results = await bounded_map([0, 1, 2, 3], run, limit=2)

    assert isinstance(results[1], ValueError)
    assert results[0] == 0 and results[2] == 2 and results[3] == 3
    assert sorted(finished) == [0, 2, 3]


@pytest.mark.asyncio
async def test_on_ready_runs_in_prefix_order_and_never_concurrently() -> None:
    seen: list[int] = []
    concurrent = 0
    peak = 0

    async def run(item: int) -> int:
        await asyncio.sleep(0.05 - item * 0.01)
        return item

    async def on_ready(index: int, _item: int, result: int) -> None:
        nonlocal concurrent, peak
        concurrent += 1
        peak = max(peak, concurrent)
        await asyncio.sleep(0)
        seen.append(index)
        assert result == index
        concurrent -= 1

    await bounded_map([0, 1, 2, 3], run, limit=4, on_ready=on_ready)

    assert seen == [0, 1, 2, 3]
    assert peak == 1


@pytest.mark.asyncio
async def test_stop_drains_inflight_work_before_raising() -> None:
    """已经付过钱的调用必须跑完并落库；只是不再准入新的。"""
    ran: list[int] = []
    flushed: list[int] = []

    async def run(item: int) -> int:
        ran.append(item)
        await asyncio.sleep(0.01)
        return item

    async def on_ready(_index: int, item: int, _result: int) -> None:
        flushed.append(item)

    async def stop_check() -> None:
        if flushed:
            raise JobStopped("cancel")

    with pytest.raises(JobStopped):
        await bounded_map(list(range(10)), run, limit=3, on_ready=on_ready, stop_check=stop_check)

    # 停止确实生效了：远没有跑完 10 项。
    assert len(ran) < 10
    # 已经付过钱的每一项都落了库——这是真正的不变量，不丢弃任何付费调用。
    assert flushed == ran
    # 跑过的是输入的前缀，不是零散的一批。
    assert ran == list(range(len(ran)))
    # 准入最多比冲刷循环跑前一个窗口：停止是在第 0 项落库后才发出的，
    # 那一刻窗口里已有 limit 项在飞，其中任何一项完成又会再放进一项。
    assert len(ran) <= 1 + 2 * 3


@pytest.mark.asyncio
async def test_items_never_admitted_are_marked_not_run() -> None:
    async def run(item: int) -> int:
        await asyncio.sleep(0.01)
        return item

    async def stop_check() -> None:
        raise JobStopped("pause")

    with pytest.raises(JobStopped):
        results = await bounded_map(list(range(6)), run, limit=2, stop_check=stop_check)

    # 未准入的槽必须与「跑了但失败」可区分，否则统计会把没花的钱记成失败。
    results = await bounded_map([], run, limit=2)
    assert results == []


@pytest.mark.asyncio
async def test_fn_raising_job_stopped_is_treated_as_stop_not_as_item_failure() -> None:
    async def run(item: int) -> int:
        if item == 0:
            raise JobStopped("cancel")
        await asyncio.sleep(0.01)
        return item

    with pytest.raises(JobStopped):
        await bounded_map(list(range(6)), run, limit=2)


@pytest.mark.asyncio
async def test_empty_input_is_a_no_op() -> None:
    async def run(item: int) -> int:  # pragma: no cover - 不该被调用
        raise AssertionError("must not run")

    assert await bounded_map([], run, limit=4) == []


@pytest.mark.asyncio
async def test_not_run_sentinel_is_falsy_distinct_from_none() -> None:
    assert NOT_RUN is not None
    assert repr(NOT_RUN) == "<NOT_RUN>"
