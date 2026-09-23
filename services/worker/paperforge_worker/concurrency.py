"""有界并发工具：把「批次栅栏」换成「滑动窗口」，且不改变任何可观察顺序。

全管线实测（2026-09-07，job 1b6ba10a，65.7 分钟）：51.4 分钟里只有**一个** LLM 调用在飞，
所有调用延迟之和 65.6 分钟 ≈ 墙钟。提速的唯一杠杆是并发，而并发要落地必须先解决
三件事——它们正是这里的三条契约：

1. **结果按输入序**。下游有好几处按位置读结果（``qmatrix`` 的 ``diagnostics`` 顺序、
   ``bridge_sources`` 的插入序、``ReviewOutcome.verdicts``），完成序会把它们打乱。
2. **副作用串行**。``context.emit`` 要占一个 DB session，而连接池每进程只有 8 条；
   并发发事件既会打乱事件流，也会把池吃穿。所以回调按**前缀序**串行调用。
3. **已付费的调用不丢弃**。用户按停止时，在飞的 provider 调用已经花过钱了，
   必须让它们跑完并落库，只是不再**准入**新的。这是 ``cards.py`` 原本就承诺的不变量，
   这里把它从批粒度扩展到窗口粒度。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from paperforge_worker.context import JobStopped


class _NotRun:
    """占位：该项因为停止请求而**从未被准入**，一分钱都没花。

    与「跑了但抛异常」必须能区分开——后者要计入失败统计，前者不该计入任何统计。
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - 仅用于调试输出
        return "<NOT_RUN>"


NOT_RUN = _NotRun()


async def bounded_map[T, R](
    items: Sequence[T],
    fn: Callable[[T], Awaitable[R]],
    *,
    limit: int,
    on_ready: Callable[[int, T, Any], Awaitable[None]] | None = None,
    stop_check: Callable[[], Awaitable[None]] | None = None,
) -> list[Any]:
    """并发跑 ``fn(item)``，最多 ``limit`` 个在飞，结果按**输入序**返回。

    ``on_ready(index, item, result)`` 在 ``0..index`` 全部落地后**串行**调用一次；
    ``result`` 要么是 ``fn`` 的返回值，要么是它抛出的 ``Exception`` 实例——
    调用方自己决定把异常记成告警还是失败，**绝不会**因为一项抛错就牵连兄弟。
    从未准入的项不会触发 ``on_ready``，其结果槽是 :data:`NOT_RUN`。

    ``stop_check`` 通常就是 ``context.raise_if_stopped``：它在每次 ``on_ready`` 之后
    被调用，抛出即表示用户按了停止。此时**停止准入**新项，但在飞的项照常跑完、
    照常经过 ``on_ready`` 落库，全部收敛后才把那个异常重新抛出。

    注意准入会**跑在冲刷循环前面**：某项一完成就释放信号量、立刻放进下一项，
    不等 ``on_ready`` 被调用——不这样做就退化成批次栅栏了。所以停止请求落地时，
    实际已付费的项可能比「已冲刷的项 + 一个窗口」再多一点。这与今天
    ``cards.py`` 按批检查停止是同一量级（那里一批 6 个），不是新增的浪费。
    真正的保证是两条：**停止之后不再准入**，且**已付费的每一项都会经过
    ``on_ready`` 落库**。

    ``fn`` 自己抛 ``JobStopped`` 同样按停止处理（而不是记成这一项的失败）。
    """
    total = len(items)
    results: list[Any] = [NOT_RUN] * total
    if total == 0:
        return results

    window = max(1, int(limit))
    semaphore = asyncio.Semaphore(window)
    # 单元素列表而不是 nonlocal 布尔：闭包里只读它，赋值集中在主循环，读写都很显眼。
    admitting = [True]
    stop_error: BaseException | None = None

    async def _run(index: int) -> None:
        # 信号量在任务内获取：任务一次性全建好，靠信号量排队形成滑动窗口。
        # asyncio.Semaphore 的等待者是 FIFO 的，所以准入顺序≈输入顺序。
        async with semaphore:
            if not admitting[0]:
                # 停止请求已经来了，这一项还没开始——不花这笔钱。
                return
            try:
                results[index] = await fn(items[index])
            except JobStopped as error:
                # 停止不是这一项的失败，按停止语义走。
                results[index] = error
            except Exception as error:  # noqa: BLE001 - 每项隔离，异常交给调用方判读
                results[index] = error
            # CancelledError 等 BaseException 故意不接：那是真的要中断，不该被吞掉。

    tasks = [asyncio.create_task(_run(index)) for index in range(total)]

    try:
        for index in range(total):
            # 任务内部已经把异常收进 results，所以这里 await 不会抛（除非被取消）。
            await tasks[index]
            outcome = results[index]
            if outcome is NOT_RUN:
                continue
            if isinstance(outcome, JobStopped):
                if stop_error is None:
                    stop_error = outcome
                admitting[0] = False
                continue
            if on_ready is not None:
                await on_ready(index, items[index], outcome)
            if stop_check is not None and stop_error is None:
                try:
                    await stop_check()
                except Exception as error:  # noqa: BLE001 - 停止信号，不是缺陷
                    stop_error = error
                    admitting[0] = False
    finally:
        # 正常路径走到这里所有任务都已 done，这是空操作。
        # 异常路径（比如 on_ready 自己抛了）才用得上：停掉准入，把在飞的排空，
        # 既不再产生新费用，也不留下 "Task was destroyed but it is pending"。
        admitting[0] = False
        pending = [task for task in tasks if not task.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    if stop_error is not None:
        raise stop_error
    return results
