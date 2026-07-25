"""Provider 限速 / 熔断 / 失败诊断运行时。

迁移自 DeepSearch literature_review/adapters.py 的 pacing + circuit breaker 段落
（设计 §3.1「含限速/熔断/缓存」）。改动：从 adapters.py 拆出为独立模块，
配置由构造注入而非全局 settings；不再写 occurrence 账本。

礼貌性机制，绝非绕过：熔断打开期间**一个请求都不发**（设计 §1.4 合规获取）。
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from json import JSONDecodeError
from typing import Any

import httpx

RETRY_AFTER_MAX_SECONDS = 30.0

# Semantic Scholar 请求节流。匿名池被所有匿名客户端共享，远比 keyed 的 1 rps 严格，
# 因此匿名访问保守 pacing，而不是撞 429 墙后爆发重试。
SEMANTIC_SCHOLAR_KEYED_MIN_INTERVAL_SECONDS = 1.1
SEMANTIC_SCHOLAR_ANONYMOUS_MIN_INTERVAL_SECONDS = 3.1

# Semantic Scholar 熔断阈值（两种凭据档位通用）：连续这么多次 429 后打开熔断，
# 冷却窗口内直接快速失败而不再打扰 provider。
SEMANTIC_SCHOLAR_CIRCUIT_BREAKER_THRESHOLD = 5
SEMANTIC_SCHOLAR_CIRCUIT_BREAKER_COOLDOWN_SECONDS = 300.0

# OpenAlex 熔断阈值。OpenAlex 已切换为按量计费（keyless $0.10/天，免费 key $1/天，
# search 按次计费），连续 429 通常意味着当日预算耗尽，会持续到 UTC 午夜。
OPENALEX_CIRCUIT_BREAKER_THRESHOLD = 5
OPENALEX_CIRCUIT_BREAKER_COOLDOWN_SECONDS = 300.0

# 命中「日配额耗尽」签名（X-RateLimit-Remaining: 0）时的冷却上下界。
_DAILY_QUOTA_MIN_COOLDOWN_SECONDS = 60.0
_DAILY_QUOTA_FALLBACK_COOLDOWN_SECONDS = 3600.0


def semantic_scholar_min_request_interval(
    *,
    api_key: str | None,
    configured_interval_seconds: float | None = None,
) -> float:
    """按凭据档位解析 S2 有效 pacing 间隔；显式正值配置永远优先。"""
    if configured_interval_seconds is not None and configured_interval_seconds > 0:
        return float(configured_interval_seconds)
    if api_key:
        return SEMANTIC_SCHOLAR_KEYED_MIN_INTERVAL_SECONDS
    return SEMANTIC_SCHOLAR_ANONYMOUS_MIN_INTERVAL_SECONDS


class ProviderRequestPacer:
    """进程级最小间隔节流，供所有 adapter 实例共享。

    检索预检、按 provider 的并行采集 worker、雪球扩展各自会构造 adapter 实例，
    因此按实例节流保护不了共享配额。pacer 在锁下发放预约槽位，并发调用者礼貌排队。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next_allowed_at = 0.0

    def wait(
        self,
        interval_seconds: float,
        *,
        sleep_fn: Callable[[float], None],
        now_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        if interval_seconds <= 0:
            return
        with self._lock:
            now = now_fn()
            delay = max(0.0, self._next_allowed_at - now)
            self._next_allowed_at = max(now, self._next_allowed_at) + interval_seconds
        if delay > 0:
            sleep_fn(delay)


class ProviderCircuitOpenError(Exception):
    """熔断打开期间抛出，代替发送请求。"""

    def __init__(
        self,
        provider_name: str,
        *,
        remaining_seconds: float,
        consecutive_429_count: int,
    ) -> None:
        self.provider_name = provider_name
        self.remaining_seconds = remaining_seconds
        self.consecutive_429_count = consecutive_429_count
        super().__init__(
            f"{provider_name} circuit breaker is open for another "
            f"{remaining_seconds:.0f}s after {consecutive_429_count} consecutive "
            "HTTP 429 responses."
        )


class ProviderCircuitBreaker:
    """连续 429 熔断器，跨 adapter 实例共享。

    Closed：请求正常发送，每次 429 累加计数，任一成功清零。
    Open：达到阈值，冷却结束前 allow() 直接抛错；冷却后半开
    （计数保留，因此再来一次 429 立刻重新打开；一次成功则完全关闭）。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._consecutive_429 = 0
        self._open_until = 0.0

    def allow(
        self,
        provider_name: str,
        *,
        now_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        with self._lock:
            remaining = self._open_until - now_fn()
            if remaining > 0:
                raise ProviderCircuitOpenError(
                    provider_name,
                    remaining_seconds=remaining,
                    consecutive_429_count=self._consecutive_429,
                )

    def record_429(
        self,
        *,
        threshold: int,
        cooldown_seconds: float,
        now_fn: Callable[[], float] = time.monotonic,
    ) -> int:
        with self._lock:
            self._consecutive_429 += 1
            if threshold > 0 and self._consecutive_429 >= threshold:
                self._open_until = now_fn() + max(0.0, cooldown_seconds)
            return self._consecutive_429

    def record_success(self) -> None:
        with self._lock:
            self._consecutive_429 = 0
            self._open_until = 0.0

    def snapshot(self, *, now_fn: Callable[[], float] = time.monotonic) -> dict[str, Any]:
        with self._lock:
            remaining = max(0.0, self._open_until - now_fn())
            return {
                "state": "open" if remaining > 0 else "closed",
                "consecutive_429_count": self._consecutive_429,
                "cooldown_remaining_seconds": round(remaining, 1) or 0.0,
            }


_PROVIDER_PACERS: dict[str, ProviderRequestPacer] = {}
_PROVIDER_PACERS_LOCK = threading.Lock()
_PROVIDER_CIRCUIT_BREAKERS: dict[str, ProviderCircuitBreaker] = {}
_PROVIDER_CIRCUIT_BREAKERS_LOCK = threading.Lock()
_PROVIDER_REQUEST_LOCKS: dict[str, threading.Lock] = {}
_PROVIDER_REQUEST_LOCKS_LOCK = threading.Lock()


def provider_pacer(provider_name: str) -> ProviderRequestPacer:
    with _PROVIDER_PACERS_LOCK:
        pacer = _PROVIDER_PACERS.get(provider_name)
        if pacer is None:
            pacer = ProviderRequestPacer()
            _PROVIDER_PACERS[provider_name] = pacer
        return pacer


def provider_circuit_breaker(provider_name: str) -> ProviderCircuitBreaker:
    with _PROVIDER_CIRCUIT_BREAKERS_LOCK:
        breaker = _PROVIDER_CIRCUIT_BREAKERS.get(provider_name)
        if breaker is None:
            breaker = ProviderCircuitBreaker()
            _PROVIDER_CIRCUIT_BREAKERS[provider_name] = breaker
        return breaker


def provider_request_lock(provider_name: str) -> threading.Lock:
    """严格单飞锁，供共享匿名配额（并发=1）使用。"""
    with _PROVIDER_REQUEST_LOCKS_LOCK:
        lock = _PROVIDER_REQUEST_LOCKS.get(provider_name)
        if lock is None:
            lock = threading.Lock()
            _PROVIDER_REQUEST_LOCKS[provider_name] = lock
        return lock


def provider_circuit_snapshot(provider_name: str) -> dict[str, Any]:
    return provider_circuit_breaker(provider_name).snapshot()


def reset_provider_rate_limit_state() -> None:
    """测试钩子：清空共享 pacer、单飞锁与熔断器。"""
    with _PROVIDER_PACERS_LOCK:
        _PROVIDER_PACERS.clear()
    with _PROVIDER_CIRCUIT_BREAKERS_LOCK:
        _PROVIDER_CIRCUIT_BREAKERS.clear()
    with _PROVIDER_REQUEST_LOCKS_LOCK:
        _PROVIDER_REQUEST_LOCKS.clear()


def retry_after_seconds(response: httpx.Response | None) -> float | None:
    if response is None:
        return None
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        seconds = float(raw.strip())
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    return min(seconds, RETRY_AFTER_MAX_SECONDS)


def daily_quota_exhausted_cooldown(response: httpx.Response | None) -> float | None:
    """识别「当日预算耗尽」型 429 并返回到重置为止的冷却秒数。

    OpenAlex 早期用 ``X-RateLimit-Remaining: 0`` + ``X-RateLimit-Reset`` 暴露；
    当前按量计费 API 可能只在 JSON 429 体里给出同样的证据（``Insufficient budget``
    + ``retryAfter``）。两种形态下在重置前重试都无意义，因此调用方快速失败并
    对剩余窗口打开 provider 熔断。
    """
    if response is None:
        return None
    remaining_raw = response.headers.get("X-RateLimit-Remaining")
    quota_proven = False
    reset_seconds: float | None = None
    if remaining_raw is not None:
        try:
            remaining = float(remaining_raw.strip())
        except (TypeError, ValueError):
            remaining = None
        if remaining is not None and remaining <= 0:
            quota_proven = True
            reset_raw = response.headers.get("X-RateLimit-Reset")
            try:
                reset_seconds = float(reset_raw.strip()) if reset_raw is not None else None
            except (TypeError, ValueError):
                reset_seconds = None
    body_details = openalex_quota_error_details(response.text)
    if body_details:
        quota_proven = True
        if reset_seconds is None:
            reset_seconds = body_details.get("retry_after_seconds")
    if not quota_proven:
        return None
    if reset_seconds is None or reset_seconds <= 0:
        reset_seconds = _DAILY_QUOTA_FALLBACK_COOLDOWN_SECONDS
    return max(_DAILY_QUOTA_MIN_COOLDOWN_SECONDS, reset_seconds)


def openalex_quota_error_details(body: str) -> dict[str, float]:
    """返回 OpenAlex「仅响应体」型预算耗尽 429 的安全诊断字段。"""
    try:
        payload = json.loads(body)
    except (JSONDecodeError, TypeError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    message = str(payload.get("message") or "").lower()
    if "insufficient budget" not in message:
        return {}
    fields = {
        "retry_after_seconds": payload.get("retryAfter"),
        "request_cost_usd": payload.get("costUsd"),
        "daily_remaining_usd": payload.get("dailyRemainingUsd"),
        "prepaid_remaining_usd": payload.get("prepaidRemainingUsd"),
    }
    details: dict[str, float] = {}
    for key, value in fields.items():
        if isinstance(value, bool):
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number < 0:
            continue
        details[key] = number
    return details
