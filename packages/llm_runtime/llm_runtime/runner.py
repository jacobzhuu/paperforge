"""角色路由 + 成本记账的唯一 LLM 调用入口（设计 §4.9）。

所有管线的 LLM 调用都必须经过 ``LLMRunner``：它按 role 解析模型、执行调用、
把 usage/延迟交给 ``on_call`` 回调（worker 据此写 ``llm_call_log``），
并对结构化输出统一做 JSON 净化与 R2 cite-key 白名单审计。

Draft-first：``generate_json`` 在 provider 不可用或输出不合法时返回 ``None``，
由调用方使用确定性回退，绝不把异常抛到管线上层。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Literal

from llm_runtime.client import create_llm_provider
from llm_runtime.config import LLMConfig, Role
from llm_runtime.json_utils import CiteKeyViolation, purify_llm_json
from llm_runtime.providers import LLMProvider, clamp_max_output_tokens
from llm_runtime.retry_policy import TruncationRetryPolicy
from llm_runtime.types import LLMError, LLMRequest, LLMResponse

DEFAULT_MAX_OUTPUT_TOKENS = 4096
#: 截断重试被跳过时记的错误码。它和 ``output_truncated`` 是两件事：前者说「模型
#: 的上限就在这儿，再问一遍也是同一个请求」，后者说「预算不够，加了还有救」。
#: 混成一个码，台账里就分不出「补救失败」和「压根没补救」。
TRUNCATED_AT_CEILING = "output_truncated_at_ceiling"


def _retry_budget(budget: int, *, model: str, policy: TruncationRetryPolicy) -> int:
    """提高之后**经模型上限 clamp** 的预算。"""
    return clamp_max_output_tokens(int(budget * policy.multiplier), model=model)


def _retry_can_help(budget: int, *, model: str, policy: TruncationRetryPolicy) -> bool:
    """重试预算经模型上限 clamp 之后，是否真的涨得够多。

    模型上限是硬的：预算被 clamp 回原值时，重试发出的是逐字相同的请求；只涨了
    零头时，一个写不完的输出也不会因为多这点 token 就写得完。两种情况都要花掉
    一整轮 40-80 秒的调用换同一个结果，所以两种都不发。
    """
    return _retry_budget(budget, model=model, policy=policy) >= clamp_max_output_tokens(
        budget, model=model
    ) * policy.min_growth_ratio


@dataclass(frozen=True)
class LLMCallRecord:
    """一次调用的记账记录（对应 ``llm_call_log`` 的列）。"""

    role: str
    model: str
    provider: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_estimate: float | None = None
    latency_ms: int | None = None
    error_code: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True)
class JsonResult:
    """结构化调用结果。``value is None`` 表示应走确定性回退。"""

    value: Any | None
    violations: tuple[CiteKeyViolation, ...] = ()
    raw_text: str | None = None
    error: str | None = None
    model: str | None = None

    @property
    def ok(self) -> bool:
        return self.value is not None


class LLMRunner:
    """按 role 路由的调用器；provider 可注入以便测试与确定性回退。"""

    def __init__(
        self,
        config: LLMConfig,
        *,
        provider: LLMProvider | None = None,
        on_call: Callable[[LLMCallRecord], None] | None = None,
    ) -> None:
        self._config = config
        self._provider = provider
        self._on_call = on_call

    @property
    def config(self) -> LLMConfig:
        return self._config

    @property
    def enabled(self) -> bool:
        """provider 是否可能产出真实内容（noop / 未启用时为 False）。"""
        if not self._config.enabled:
            return False
        return self._config.provider.strip().lower() not in {"", "noop"}

    def model_for(self, role: Role) -> str:
        return self._config.model_for_role(role)

    def _resolve_provider(self) -> LLMProvider:
        if self._provider is None:
            self._provider = create_llm_provider(self._config)
        return self._provider

    def generate(
        self,
        role: Role,
        *,
        system_prompt: str,
        user_prompt: str,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        temperature: float = 0.0,
        json_output: bool = False,
        metadata: dict[str, Any] | None = None,
        _truncation_attempt: int = 0,
    ) -> LLMResponse | None:
        """同步调用。失败返回 None 并记录一条带 error_code 的记账（draft-first）。

        推理型模型把思维链计入 max_tokens，预算不足会「零 content」返回；这种情况
        按角色策略提高预算重试，再失败才降级。**加倍之后被模型上限 clamp 回原值
        时不重试**：那是逐字相同的请求，只会再花一轮调用换同一个结果。
        """
        model = self.model_for(role)
        policy = self._config.retry_for_role(role)
        request = LLMRequest(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model=model,
            max_output_tokens=max_output_tokens,
            temperature=temperature,
            json_output=json_output,
            thinking_mode=self._config.thinking_for_role(role),
            metadata={"role": role, **(metadata or {})},
        )
        started = time.monotonic()
        try:
            response = self._resolve_provider().generate(request)
        except LLMError as error:
            can_retry = _truncation_attempt < policy.max_attempts and _retry_can_help(
                max_output_tokens, model=model, policy=policy
            )
            truncated = error.error_code == "output_truncated"
            # 记账要先算出最终的 code：一次**没有发生**的重试，不该在台账里和一次
            # 发生了又失败的重试长得一样。
            self._record(
                role=role,
                model=model,
                provider=self._config.provider,
                latency_ms=_elapsed_ms(started),
                error_code=(
                    TRUNCATED_AT_CEILING
                    if truncated and not can_retry
                    else error.error_code
                ),
                metadata=request.metadata,
            )
            if truncated and can_retry:
                retried = self.generate(
                    role,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    max_output_tokens=_retry_budget(
                        max_output_tokens, model=model, policy=policy
                    ),
                    temperature=temperature,
                    json_output=json_output,
                    metadata={**(metadata or {}), "retry": "output_truncated"},
                    _truncation_attempt=_truncation_attempt + 1,
                )
                return (
                    replace(
                        retried,
                        truncation_retries=retried.truncation_retries + 1,
                    )
                    if retried is not None
                    else None
                )
            return None
        except Exception as error:  # noqa: BLE001 - 任何 provider 崩溃都降级
            self._record(
                role=role,
                model=model,
                provider=self._config.provider,
                latency_ms=_elapsed_ms(started),
                error_code=type(error).__name__,
                metadata=request.metadata,
            )
            return None
        usage = response.usage or {}
        self._record(
            role=role,
            model=response.model or model,
            provider=response.provider or self._config.provider,
            latency_ms=_elapsed_ms(started),
            input_tokens=_int_or_none(usage.get("prompt_tokens") or usage.get("input_tokens")),
            output_tokens=_int_or_none(
                usage.get("completion_tokens") or usage.get("output_tokens")
            ),
            metadata=request.metadata,
        )
        return response

    async def agenerate(
        self,
        role: Role,
        *,
        system_prompt: str,
        user_prompt: str,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        temperature: float = 0.0,
        json_output: bool = False,
        metadata: dict[str, Any] | None = None,
        _truncation_attempt: int = 0,
    ) -> LLMResponse | None:
        """异步包装：provider 是同步 httpx 实现，放线程池执行以免阻塞事件循环。"""
        return await asyncio.to_thread(
            self.generate,
            role,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_output_tokens=max_output_tokens,
            temperature=temperature,
            json_output=json_output,
            metadata=metadata,
            _truncation_attempt=_truncation_attempt,
        )

    async def agenerate_json(
        self,
        role: Role,
        *,
        system_prompt: str,
        user_prompt: str,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        temperature: float = 0.0,
        allowed_cite_keys: set[str] | None = None,
        mode: Literal["report", "strip"] = "report",
        metadata: dict[str, Any] | None = None,
        _truncation_attempt: int = 0,
    ) -> JsonResult:
        """结构化输出调用：净化 JSON 并做 R2 cite-key 审计。

        ``allowed_cite_keys`` 非空时，``report`` 模式只报告越权 key（供重写一次），
        ``strip`` 模式直接删除（二次违规的兜底，见设计 §4.4.3 R2）。
        """
        if not self.enabled:
            return JsonResult(value=None, error="llm_disabled")
        response = await self.agenerate(
            role,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_output_tokens=max_output_tokens,
            temperature=temperature,
            json_output=True,
            metadata=metadata,
            _truncation_attempt=_truncation_attempt,
        )
        if response is None:
            return JsonResult(value=None, error="llm_call_failed")
        try:
            parsed, violations = purify_llm_json(
                response.text,
                allowed_cite_keys=allowed_cite_keys,
                mode=mode,
            )
        except (ValueError, TypeError) as error:
            # 推理型模型把思维链计入 max_tokens。预算刚好够开口、不够写完时，
            # content 非空但 JSON 在中途断掉：provider 层的「零 content」判定救不到
            # 这一档，截断的 JSON 只会解析失败，然后整条管线静默降级到确定性回退。
            # finish_reason='length' 是这里唯一可靠的信号。加不动预算时同样不重试
            # ——理由与 `generate()` 那一处相同。
            model = self.model_for(role)
            policy = self._config.retry_for_role(role)
            if (
                _truncation_attempt < policy.max_attempts
                and response.finish_reason == "length"
                and response.truncation_retries == 0
                and _retry_can_help(max_output_tokens, model=model, policy=policy)
            ):
                return await self.agenerate_json(
                    role,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    max_output_tokens=_retry_budget(
                        max_output_tokens, model=model, policy=policy
                    ),
                    temperature=temperature,
                    allowed_cite_keys=allowed_cite_keys,
                    mode=mode,
                    metadata={**(metadata or {}), "retry": "json_truncated"},
                    _truncation_attempt=_truncation_attempt + 1,
                )
            return JsonResult(
                value=None,
                raw_text=response.text,
                error=f"invalid_json: {type(error).__name__}",
                model=response.model,
            )
        return JsonResult(
            value=parsed,
            violations=tuple(violations),
            raw_text=response.text,
            model=response.model,
        )

    def _record(
        self,
        *,
        role: str,
        model: str,
        provider: str,
        latency_ms: int | None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        error_code: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if self._on_call is None:
            return
        self._on_call(
            LLMCallRecord(
                role=role,
                model=model,
                provider=provider,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                # 没配价格或没拿到用量时留 None。写 0.0 会让成本面板把"不知道"
                # 显示成"免费"，那正是这条记账线路此前形同虚设的原因。
                cost_estimate=self._config.estimate_cost(
                    model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                ),
                latency_ms=latency_ms,
                error_code=error_code,
                metadata=dict(metadata or {}),
            )
        )


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
