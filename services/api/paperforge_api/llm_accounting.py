"""请求作用域的 LLM 调用记账。

Worker 侧的每次调用都经 ``JobContext.llm_runner()``，它带着 ``on_call`` 回调，
所以都会落进 ``llm_call_log``。API 侧的五个路由各自 ``LLMRunner(...)`` 裸构造，
不传回调——那些调用**完全不进台账**：成本面板少算，排查问题时看不见。

``llm_call_log.job_id`` 与 ``project_id`` 都可空，所以请求作用域的调用只带
``project_id`` 落库即可。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from db import record_llm_call
from llm_runtime import LLMCallRecord, LLMConfig
from observability import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    # 运行时故意不导入：构造点在函数体里查模块属性，好让既有的测试替身
    # `monkeypatch.setattr(llm_runtime, "LLMRunner", ...)` 仍然生效。
    from llm_runtime import LLMRunner

logger = get_logger(__name__)


@asynccontextmanager
async def accounted_runner(
    session: AsyncSession,
    config: LLMConfig,
    *,
    project_id: uuid.UUID | None,
) -> AsyncIterator[LLMRunner]:
    """构造带记账回调的 runner，退出时把缓冲的记录写进 ``llm_call_log``。

    记账在 ``finally`` 里落库，所以路由抛异常时那次已经花掉的调用仍然留痕——
    失败的调用同样花钱，而且正是最需要在台账里看见的那些。

    落库失败只记日志：一条记不下来的账不该把一个本来能用的接口变成 500。

    :param session: 路由自己的 DB 会话。
    :param config: 解析好的 LLM 配置。
    :param project_id: 归属项目；``None`` 表示不属于任何项目的调用。
    :returns: 一个异步上下文，产出已接好回调的 ``LLMRunner``。
    """
    # 在函数体内查模块属性，而不是在模块顶部绑定类：仓库里既有的测试替身是
    # `monkeypatch.setattr(llm_runtime, "LLMRunner", StubRunner)`，顶部绑定会
    # 把那个缝焊死。整个代码库因此只保留一个替换点。
    import llm_runtime

    records: list[LLMCallRecord] = []
    try:
        yield llm_runtime.LLMRunner(config, on_call=records.append)
    finally:
        for record in records:
            try:
                await record_llm_call(
                    session,
                    project_id=project_id,
                    job_id=None,
                    role=record.role,
                    model=record.model,
                    provider=record.provider,
                    input_tokens=record.input_tokens,
                    output_tokens=record.output_tokens,
                    cost_estimate=record.cost_estimate,
                    latency_ms=record.latency_ms,
                    error_code=record.error_code,
                    max_output_tokens=record.max_output_tokens,
                    finish_reason=record.finish_reason,
                    prompt_sha256=record.prompt_sha256,
                    prompt_chars=record.prompt_chars,
                    output_chars=record.output_chars,
                    metadata=record.metadata,
                    occurred_at=record.occurred_at,
                )
            except Exception as error:  # noqa: BLE001 - 记账失败不该拖垮请求
                logger.warning(
                    "llm_call_log write failed",
                    extra={"role": record.role, "error": type(error).__name__},
                )


__all__ = ["accounted_runner"]
