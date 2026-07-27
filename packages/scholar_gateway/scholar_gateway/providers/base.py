"""HTTP 检索适配器基类：缓存优先 → 限速 → 重试/退避 → 配额诊断。

迁移自 DeepSearch literature_review/adapters.py 的 ``_HttpScholarlyDiscoveryAdapter``
（设计 §3.1）。改动：occurrence 落账全部删除；缓存从「直吃 Session」改为注入
``HttpCacheBackend``；pacer/breaker 移入 ``scholar_gateway.runtime``；
限速与熔断参数由 ``ProviderConfig`` 构造注入，不读全局 settings。
"""

from __future__ import annotations

import json
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from json import JSONDecodeError
from typing import Any

import httpx

from scholar_gateway.cache import HttpCacheBackend, NullHttpCache
from scholar_gateway.models import (
    ScholarlyDiscoveryError,
    ScholarlyDiscoveryQuery,
    ScholarlyDiscoveryResult,
)
from scholar_gateway.runtime import (
    RETRY_AFTER_MAX_SECONDS,
    ProviderCircuitOpenError,
    daily_quota_exhausted_cooldown,
    openalex_quota_error_details,
    provider_circuit_breaker,
    provider_circuit_snapshot,
    provider_pacer,
    provider_request_lock,
    retry_after_seconds,
)


@dataclass(frozen=True)
class ProviderConfig:
    """provider 连接配置（构造注入，取代 DeepSearch 的全局 settings）。"""

    api_key: str | None = None
    user_agent: str | None = None
    contact_email: str | None = None
    timeout_seconds: float = 10.0
    rate_limit_delay: float = 1.0
    max_retries: int = 3
    cache_ttl_hours: float = 72.0
    base_url: str | None = None
    min_request_interval_seconds: float | None = None
    circuit_breaker_threshold: int | None = None
    circuit_breaker_cooldown_seconds: float | None = None
    serialize_requests: bool | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FetchedHttpResponse:
    status_code: int
    text: str
    content: bytes
    content_type: str | None
    cache_hit: bool
    request_url: str
    request_params: dict[str, Any]
    attempt_count: int = 1
    retry_exhausted: bool = False
    retry_after_seconds: float | None = None
    rate_limit_429_count: int = 0
    daily_quota_exhausted: bool = False


class HttpScholarlyDiscoveryAdapter:
    name: str = ""
    provider_name: str = ""
    base_url: str = ""

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        config: ProviderConfig | None = None,
        cache: HttpCacheBackend | None = None,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        config = config or ProviderConfig()
        self._config = config
        self._client = client
        self._timeout_seconds = config.timeout_seconds
        if config.base_url:
            self.base_url = config.base_url.rstrip("/")
        self._api_key = (config.api_key or "").strip() or None
        self._user_agent = config.user_agent
        self._rate_limit_delay = config.rate_limit_delay
        self._max_retries = config.max_retries
        self._last_request_time = 0.0
        self._cache: HttpCacheBackend = cache or NullHttpCache()
        self._cache_ttl_hours = config.cache_ttl_hours
        self._sleep = sleep_fn or time.sleep
        # 跨实例 pacing：0/None 关闭本适配器的共享 pacer。
        self._min_request_interval_seconds = float(
            self.default_min_request_interval(config)
            if config.min_request_interval_seconds is None
            else config.min_request_interval_seconds
        )
        threshold = (
            self.default_circuit_breaker_threshold()
            if config.circuit_breaker_threshold is None
            else config.circuit_breaker_threshold
        )
        cooldown = (
            self.default_circuit_breaker_cooldown_seconds()
            if config.circuit_breaker_cooldown_seconds is None
            else config.circuit_breaker_cooldown_seconds
        )
        self._circuit_breaker_threshold = int(threshold or 0)
        self._circuit_breaker_cooldown_seconds = float(cooldown or 0.0)
        self._serialize_requests = bool(
            self.default_serialize_requests(config)
            if config.serialize_requests is None
            else config.serialize_requests
        )

    # --- 每个 provider 可覆盖的默认限速/熔断策略 ---

    def default_min_request_interval(self, config: ProviderConfig) -> float:
        return 0.0

    def default_circuit_breaker_threshold(self) -> int:
        return 0

    def default_circuit_breaker_cooldown_seconds(self) -> float:
        return 0.0

    def default_serialize_requests(self, config: ProviderConfig) -> bool:
        return False

    # --- 子类契约 ---

    def _request(self, query: ScholarlyDiscoveryQuery) -> tuple[str, dict[str, Any]]:
        raise NotImplementedError

    def map_payload(
        self,
        *,
        query: ScholarlyDiscoveryQuery,
        payload: dict[str, Any],
        retrieved_at: datetime | None = None,
        request_metadata: dict[str, Any] | None = None,
    ) -> ScholarlyDiscoveryResult:
        raise NotImplementedError

    # --- 公共检索流程 ---

    def discover(self, query: ScholarlyDiscoveryQuery) -> ScholarlyDiscoveryResult:
        retrieved_at = datetime.now(UTC)
        if self._client is None:
            return self._not_configured(query, retrieved_at)

        request_url, params = self._request(query)
        headers = self._request_headers()
        fetched, fetch_error = self._fetch_http(request_url, params, headers)
        if fetched is None:
            return self._fetch_failure_result(
                query=query,
                retrieved_at=retrieved_at,
                request_url=request_url,
                params=params,
                fetch_error=fetch_error,
            )
        if fetched.status_code >= 400:
            return self._http_error_result(
                query=query,
                retrieved_at=retrieved_at,
                request_url=request_url,
                params=params,
                fetched=fetched,
            )

        try:
            payload = json.loads(fetched.text)
        except (JSONDecodeError, ValueError) as error:
            return self._error_result(
                query=query,
                retrieved_at=retrieved_at,
                error_code="invalid_json",
                message=f"{self.provider_name} response was not valid JSON.",
                status_code=fetched.status_code,
                retryable=False,
                metadata={
                    "request_url": request_url,
                    "request_params": params,
                    "error": str(error),
                    "cache_hit": fetched.cache_hit,
                },
            )
        if not isinstance(payload, dict):
            return self._error_result(
                query=query,
                retrieved_at=retrieved_at,
                error_code="invalid_response",
                message=f"{self.provider_name} response JSON was not an object.",
                status_code=fetched.status_code,
                retryable=False,
                metadata={
                    "request_url": request_url,
                    "request_params": params,
                    "cache_hit": fetched.cache_hit,
                },
            )
        return self.map_payload(
            query=query,
            payload=payload,
            retrieved_at=retrieved_at,
            request_metadata={
                "request_url": request_url,
                "request_params": params,
                "cache_hit": fetched.cache_hit,
            },
        )

    def _not_configured(
        self,
        query: ScholarlyDiscoveryQuery,
        retrieved_at: datetime,
    ) -> ScholarlyDiscoveryResult:
        return ScholarlyDiscoveryResult(
            provider_name=self.provider_name,
            query=query,
            retrieved_at=retrieved_at,
            errors=(
                ScholarlyDiscoveryError(
                    provider_name=self.provider_name,
                    error_code="adapter_not_configured",
                    message=(
                        f"{self.provider_name} discovery adapter requires an injected "
                        "httpx.Client for network use."
                    ),
                    retryable=False,
                ),
            ),
            metadata={"network_required": False, "configured": False},
        )

    def _fetch_failure_result(
        self,
        *,
        query: ScholarlyDiscoveryQuery,
        retrieved_at: datetime,
        request_url: str,
        params: dict[str, Any],
        fetch_error: Exception | None,
    ) -> ScholarlyDiscoveryResult:
        if isinstance(fetch_error, ProviderCircuitOpenError):
            return self._error_result(
                query=query,
                retrieved_at=retrieved_at,
                error_code="circuit_open",
                message=str(fetch_error),
                status_code=429,
                retryable=True,
                metadata={
                    "request_url": request_url,
                    "request_params": params,
                    "cache_hit": False,
                    "rate_limited": True,
                    "fallback_reason": "circuit_open",
                    "circuit_cooldown_remaining_seconds": round(fetch_error.remaining_seconds, 1),
                    "consecutive_429_count": fetch_error.consecutive_429_count,
                },
            )
        is_timeout = isinstance(fetch_error, httpx.TimeoutException)
        return self._error_result(
            query=query,
            retrieved_at=retrieved_at,
            error_code="timeout" if is_timeout else "request_error",
            message=str(fetch_error),
            retryable=True,
            metadata={
                "request_url": request_url,
                "request_params": params,
                "error": str(fetch_error),
                "cache_hit": False,
            },
        )

    def _http_error_result(
        self,
        *,
        query: ScholarlyDiscoveryQuery,
        retrieved_at: datetime,
        request_url: str,
        params: dict[str, Any],
        fetched: FetchedHttpResponse,
    ) -> ScholarlyDiscoveryResult:
        metadata: dict[str, Any] = {
            "request_url": request_url,
            "request_params": params,
            "body_preview": fetched.text[:500],
            "cache_hit": fetched.cache_hit,
        }
        if fetched.status_code == 429:
            metadata["rate_limited"] = True
            metadata["attempt_count"] = fetched.attempt_count
            metadata["retry_exhausted"] = fetched.retry_exhausted
            metadata["retry_after_seconds"] = fetched.retry_after_seconds or RETRY_AFTER_MAX_SECONDS
            metadata["rate_limit_429_count"] = fetched.rate_limit_429_count
            if fetched.daily_quota_exhausted:
                metadata["daily_quota_exhausted"] = True
                metadata.update(openalex_quota_error_details(fetched.text))
            if self._circuit_breaker_threshold > 0:
                metadata["circuit"] = provider_circuit_snapshot(self.provider_name)
            hint = self.rate_limit_hint(fetched)
            if hint:
                metadata["hint"] = hint
        return self._error_result(
            query=query,
            retrieved_at=retrieved_at,
            error_code="http_error",
            message=f"{self.provider_name} returned HTTP {fetched.status_code}.",
            status_code=fetched.status_code,
            retryable=fetched.status_code in {408, 429} or fetched.status_code >= 500,
            metadata=metadata,
        )

    def rate_limit_hint(self, fetched: FetchedHttpResponse) -> str | None:
        return None

    def _request_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self._user_agent:
            headers["User-Agent"] = self._user_agent
        return headers

    def _auth_params(self) -> dict[str, str]:
        """凭据仅在发请求时合并，确保 API key 不进缓存键/诊断/事件负载。"""
        return {}

    def _fetch_http(
        self,
        request_url: str,
        params: dict[str, Any],
        headers: dict[str, str],
    ) -> tuple[FetchedHttpResponse | None, Exception | None]:
        """共享 GET：缓存优先 → 限速 → 重试/退避。"""
        assert self._client is not None
        cached = self._cache.get(
            url=request_url,
            params=params,
            ttl_hours=self._cache_ttl_hours,
        )
        if cached is not None:
            return (
                FetchedHttpResponse(
                    status_code=cached.status_code,
                    text=cached.text,
                    content=cached.body,
                    content_type=cached.content_type,
                    cache_hit=True,
                    request_url=request_url,
                    request_params=params,
                ),
                None,
            )

        breaker = (
            provider_circuit_breaker(self.provider_name)
            if self._circuit_breaker_threshold > 0
            else None
        )
        if breaker is not None:
            try:
                breaker.allow(self.provider_name)
            except ProviderCircuitOpenError as error:
                return None, error

        elapsed = time.time() - self._last_request_time
        if elapsed < self._rate_limit_delay:
            self._sleep(self._rate_limit_delay - elapsed)

        request_lock = (
            provider_request_lock(self.provider_name) if self._serialize_requests else None
        )
        if request_lock is not None:
            request_lock.acquire()
        try:
            response = None
            last_error: Exception | None = None
            last_retry_after: float | None = None
            attempt = 0
            rate_limit_429_count = 0
            daily_quota_exhausted = False
            while attempt <= self._max_retries:
                if breaker is not None:
                    try:
                        breaker.allow(self.provider_name)
                    except ProviderCircuitOpenError as error:
                        if response is not None:
                            break
                        return None, error
                if self._min_request_interval_seconds > 0:
                    provider_pacer(self.provider_name).wait(
                        self._min_request_interval_seconds,
                        sleep_fn=self._sleep,
                    )
                self._last_request_time = time.time()
                try:
                    auth_params = self._auth_params()
                    response = self._client.get(
                        request_url,
                        params={**params, **auth_params} if auth_params else params,
                        headers=headers,
                        timeout=self._timeout_seconds,
                    )
                    if response.status_code >= 400:
                        retryable = (
                            response.status_code in {408, 429} or response.status_code >= 500
                        )
                        if not retryable:
                            break
                        response.raise_for_status()
                    if breaker is not None:
                        breaker.record_success()
                    break
                except httpx.HTTPError as error:
                    last_error = error
                    is_retryable = True
                    retry_after: float | None = None
                    if isinstance(error, httpx.HTTPStatusError):
                        status_code = error.response.status_code
                        is_retryable = status_code in {408, 429} or status_code >= 500
                        retry_after = retry_after_seconds(error.response)
                        if retry_after is not None:
                            last_retry_after = retry_after
                        if status_code == 429:
                            rate_limit_429_count += 1
                            quota_cooldown = daily_quota_exhausted_cooldown(error.response)
                            if quota_cooldown is not None:
                                # 当日预算耗尽：重置前重试无意义。立刻失败本请求，
                                # 并对剩余窗口打开共享熔断。
                                daily_quota_exhausted = True
                                is_retryable = False
                                last_retry_after = quota_cooldown
                                if breaker is not None:
                                    breaker.record_429(
                                        threshold=1,
                                        cooldown_seconds=quota_cooldown,
                                    )
                            elif breaker is not None:
                                breaker.record_429(
                                    threshold=self._circuit_breaker_threshold,
                                    cooldown_seconds=self._circuit_breaker_cooldown_seconds,
                                )
                    if not is_retryable:
                        break
                    attempt += 1
                    if attempt > self._max_retries:
                        break
                    delay = 2.0 * (2 ** (attempt - 1)) + random.uniform(0.1, 1.0)
                    if retry_after is not None:
                        delay = max(delay, retry_after)
                    self._sleep(delay)
        finally:
            if request_lock is not None:
                request_lock.release()

        if response is None:
            return None, last_error

        content_type = response.headers.get("content-type")
        body = response.content
        if response.status_code == 200:
            self._cache.put(
                url=request_url,
                params=params,
                status_code=response.status_code,
                content_type=content_type,
                body=body,
                provider_name=self.provider_name,
            )
        return (
            FetchedHttpResponse(
                status_code=response.status_code,
                text=response.text,
                content=body,
                content_type=content_type,
                cache_hit=False,
                request_url=request_url,
                request_params=params,
                attempt_count=min(attempt + 1, self._max_retries + 1),
                retry_exhausted=bool(
                    response.status_code in {408, 429} or response.status_code >= 500
                )
                and attempt > self._max_retries,
                retry_after_seconds=last_retry_after,
                rate_limit_429_count=rate_limit_429_count,
                daily_quota_exhausted=daily_quota_exhausted,
            ),
            None,
        )

    def _error_result(
        self,
        *,
        query: ScholarlyDiscoveryQuery,
        retrieved_at: datetime,
        error_code: str,
        message: str,
        status_code: int | None = None,
        retryable: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> ScholarlyDiscoveryResult:
        return ScholarlyDiscoveryResult(
            provider_name=self.provider_name,
            query=query,
            retrieved_at=retrieved_at,
            errors=(
                ScholarlyDiscoveryError(
                    provider_name=self.provider_name,
                    error_code=error_code,
                    message=message,
                    status_code=status_code,
                    retryable=retryable,
                    metadata=metadata or {},
                ),
            ),
        )
