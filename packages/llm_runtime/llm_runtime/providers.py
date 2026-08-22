from __future__ import annotations

import random
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from json import JSONDecodeError
from typing import Any, Protocol

import httpx

from llm_runtime.types import LLMError, LLMRequest, LLMResponse

# 迁移自 DeepSearch services/orchestrator/app/llm/providers.py。
# 改动：仅替换 import 路径；NoopLLMProvider 的 DeepSearch OSINT 专属规划器输出
# 替换为通用空 JSON（PaperForge 各管线自带确定性回退，不依赖 noop 生成内容）。


class LLMProvider(Protocol):
    name: str

    def generate(self, request: LLMRequest) -> LLMResponse: ...


class NoopLLMProvider:
    """确定性空 provider：不调用外部 API，返回空 JSON 对象。

    用途：未配置真实 provider 时的占位（disabled/测试）。PaperForge 各写作/规划管线
    在拿到空响应时走各自的确定性回退，因此这里不再内嵌 DeepSearch 的规划器样板。
    """

    name = "noop"

    def generate(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(
            text="{}",
            model=request.model or "noop",
            provider=self.name,
            usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            raw_response_id="noop",
            finish_reason="stop",
        )


class UnsupportedLLMProvider:
    def __init__(self, provider_name: str) -> None:
        self.name = provider_name or "unsupported"

    def generate(self, request: LLMRequest) -> LLMResponse:
        del request
        raise LLMError(
            provider=self.name,
            error_code="unsupported_provider",
            message=f"unsupported LLM provider: {self.name}",
            retryable=False,
        )


class OpenAICompatibleLLMProvider:
    name = "openai-compatible"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float,
        max_retries: int,
        client: httpx.Client | None = None,
        trust_env_proxy: bool = False,
        total_deadline_seconds: float | None = None,
        retry_backoff_seconds: float = 1.0,
        retry_backoff_max_seconds: float = 30.0,
    ) -> None:
        self.base_url = sanitize_openai_compatible_base_url(base_url)
        self.chat_completions_url = build_chat_completions_url(self.base_url)
        self.api_key = api_key.strip()
        self.model = model.strip()
        self.timeout_seconds = timeout_seconds
        self.max_retries = max(0, max_retries)
        self.client = client
        self.trust_env_proxy = trust_env_proxy
        # httpx timeouts are per-operation (connect/read/write): a server that drips
        # chunks slower than the read timeout can keep one request alive indefinitely.
        # total_deadline_seconds is the wall-clock ceiling for the entire request.
        self.total_deadline_seconds = (
            float(total_deadline_seconds)
            if total_deadline_seconds is not None and float(total_deadline_seconds) > 0
            else None
        )
        self.retry_backoff_seconds = max(0.0, retry_backoff_seconds)
        self.retry_backoff_max_seconds = max(0.0, retry_backoff_max_seconds)

    def generate(self, request: LLMRequest) -> LLMResponse:
        # 按角色路由时模型来自请求（LLMConfig.role_models），配置级 model 只是回退，
        # 因此校验必须针对**生效模型**，否则只配 role_models 会被误判为缺配置。
        model_name = request.model or self.model
        self._validate_configuration(model_name)
        payload = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": request.system_prompt},
                {"role": "user", "content": request.user_prompt},
            ],
            "max_tokens": clamp_max_output_tokens(
                request.max_output_tokens,
                model=model_name,
            ),
            "temperature": request.temperature,
        }
        if request.json_output:
            payload["response_format"] = {"type": "json_object"}
        if request.thinking_mode in {"enabled", "disabled"} and _is_deepseek_v4(model_name):
            payload["thinking"] = {"type": request.thinking_mode}
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        last_error: LLMError | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self._post_chat_completions(payload=payload, headers=headers)
                return self._parse_response(response)
            except LLMError as error:
                last_error = error
                if not error.retryable or attempt >= self.max_retries:
                    raise
                self._sleep_before_retry(attempt)

        if last_error is not None:
            raise last_error
        raise LLMError(
            provider=self.name,
            error_code="request_failed",
            message="LLM request failed before receiving a response.",
            retryable=True,
        )

    def _validate_configuration(self, effective_model: str | None = None) -> None:
        missing = []
        if not self.base_url:
            missing.append("LLM_BASE_URL")
        if not (effective_model or self.model):
            missing.append("LLM_MODEL")
        if not self.api_key:
            missing.append("LLM_API_KEY")
        if missing:
            raise LLMError(
                provider=self.name,
                error_code="configuration_error",
                message=f"LLM provider is missing required configuration: {', '.join(missing)}",
                retryable=False,
            )

    def _sleep_before_retry(self, attempt: int) -> None:
        """Exponential backoff with jitter so 429/5xx retries do not hammer the provider."""
        base = self.retry_backoff_seconds
        if base <= 0:
            return
        delay = min(self.retry_backoff_max_seconds, base * (2**attempt))
        delay += random.uniform(0.0, delay * 0.25)
        time.sleep(delay)

    def _post_chat_completions(
        self,
        *,
        payload: dict[str, Any],
        headers: dict[str, str],
    ) -> httpx.Response:
        try:
            if self.client is not None:
                return self._send_with_deadline(
                    self.client,
                    payload=payload,
                    headers=headers,
                    owns_client=False,
                )
            with httpx.Client(
                timeout=self.timeout_seconds,
                trust_env=self.trust_env_proxy,
            ) as client:
                return self._send_with_deadline(
                    client,
                    payload=payload,
                    headers=headers,
                    owns_client=True,
                )
        except LLMError:
            raise
        except ImportError as error:
            raise LLMError(
                provider=self.name,
                error_code="network_error",
                message=_sanitize_message(str(error), self.api_key),
                retryable=True,
            ) from error
        except httpx.TimeoutException as error:
            raise LLMError(
                provider=self.name,
                error_code="timeout",
                message="LLM request timed out.",
                retryable=True,
            ) from error
        except httpx.HTTPError as error:
            raise LLMError(
                provider=self.name,
                error_code="network_error",
                message=_sanitize_message(str(error), self.api_key),
                retryable=True,
            ) from error

    def _send_with_deadline(
        self,
        client: httpx.Client,
        *,
        payload: dict[str, Any],
        headers: dict[str, str],
        owns_client: bool,
    ) -> httpx.Response:
        """POST with a hard wall-clock ceiling on the whole request.

        Runs the blocking httpx call on a helper thread and abandons it when the
        deadline passes. When this provider owns the client, the client is closed on
        deadline breach so the in-flight socket is torn down and the helper thread
        exits instead of leaking.
        """
        if self.total_deadline_seconds is None:
            return client.post(self.chat_completions_url, headers=headers, json=payload)
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="llm-post")
        try:
            future = executor.submit(
                client.post,
                self.chat_completions_url,
                headers=headers,
                json=payload,
            )
            try:
                return future.result(timeout=self.total_deadline_seconds)
            except FutureTimeoutError as error:
                future.cancel()
                if owns_client:
                    client.close()
                raise LLMError(
                    provider=self.name,
                    error_code="timeout",
                    message=(
                        "LLM request exceeded the total wall-clock deadline of "
                        f"{self.total_deadline_seconds:.0f}s (slow-chunk responses bypass "
                        "per-read timeouts)."
                    ),
                    retryable=True,
                ) from error
        finally:
            executor.shutdown(wait=False)

    def _parse_response(self, response: httpx.Response) -> LLMResponse:
        if response.status_code >= 400:
            error_code, retryable = _classify_http_status(response.status_code)
            raise LLMError(
                provider=self.name,
                error_code=error_code,
                status_code=response.status_code,
                message=_sanitize_message(_error_message(response), self.api_key),
                retryable=retryable,
            )

        try:
            payload = response.json()
        except (JSONDecodeError, ValueError) as error:
            raise LLMError(
                provider=self.name,
                error_code="invalid_json",
                status_code=response.status_code,
                message="LLM response was not valid JSON.",
                retryable=False,
            ) from error

        if not isinstance(payload, dict):
            raise LLMError(
                provider=self.name,
                error_code="invalid_response",
                status_code=response.status_code,
                message="LLM response JSON was not an object.",
                retryable=False,
            )

        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise LLMError(
                provider=self.name,
                error_code="invalid_response",
                status_code=response.status_code,
                message="LLM response did not include choices.",
                retryable=False,
            )
        first_choice = choices[0]
        if not isinstance(first_choice, dict):
            raise LLMError(
                provider=self.name,
                error_code="invalid_response",
                status_code=response.status_code,
                message="LLM response choice was not an object.",
                retryable=False,
            )
        message = first_choice.get("message")
        text = message.get("content") if isinstance(message, dict) else None
        if not isinstance(text, str) or not text.strip():
            # 推理型模型（deepseek-v4-*、o 系列等）把思维链算进 max_tokens：
            # 预算被推理吃光时 content 会是空的。这与「响应结构非法」是两回事——
            # 前者加预算重试就能救，因此给出独立的 error_code。
            finish_reason = first_choice.get("finish_reason")
            reasoning = message.get("reasoning_content") if isinstance(message, dict) else None
            if finish_reason == "length" or (isinstance(reasoning, str) and reasoning.strip()):
                raise LLMError(
                    provider=self.name,
                    error_code="output_truncated",
                    status_code=response.status_code,
                    message=(
                        "LLM produced no content before hitting max_tokens "
                        "(reasoning models spend the same budget on thinking)."
                    ),
                    # Repeating the same max_tokens budget deterministically
                    # truncates again, so provider-level retries are pure waste.
                    # LLMRunner owns the only useful response — raising the
                    # budget — and skips even that when the model ceiling is
                    # already reached.
                    retryable=False,
                    finish_reason=str(finish_reason) if finish_reason else None,
                )
            # 结构合法、正常终止、却一个 content 块都没有：这是服务商偶发的退化
            # 完成，不是「响应结构非法」。这次尝试没有产生任何持久副作用，重复它
            # 是安全的，所以标 retryable——`_parse_response` 在下面的重试循环内部，
            # 翻转这个 flag 就直接拿到已有的指数退避 + 抖动。
            raise LLMError(
                provider=self.name,
                error_code="empty_response",
                status_code=response.status_code,
                message="LLM completed normally but returned no content.",
                retryable=True,
                finish_reason=str(finish_reason) if finish_reason else None,
            )

        usage = payload.get("usage")
        return LLMResponse(
            text=text,
            model=str(payload.get("model") or self.model),
            provider=self.name,
            usage=usage if isinstance(usage, dict) else None,
            raw_response_id=str(payload.get("id")) if payload.get("id") is not None else None,
            finish_reason=(
                str(first_choice.get("finish_reason"))
                if first_choice.get("finish_reason") is not None
                else None
            ),
        )


def _classify_http_status(status_code: int) -> tuple[str, bool]:
    if status_code in {401, 403}:
        return "auth_error", False
    if status_code == 429:
        return "rate_limited", True
    if status_code >= 500:
        return "server_error", True
    return "http_error", False


def _is_deepseek_v4(model: str) -> bool:
    return model.strip().lower().startswith("deepseek-v4-")


def sanitize_openai_compatible_base_url(base_url: str) -> str:
    return base_url.strip().rstrip("/")


def build_chat_completions_url(base_url: str) -> str:
    sanitized = sanitize_openai_compatible_base_url(base_url)
    if sanitized.endswith("/chat/completions"):
        return sanitized
    return f"{sanitized}/chat/completions"


# Conservative completion caps. Prefer under-requesting over provider 400s when
# operator env sets an oversized max output tokens.
_DEFAULT_MAX_OUTPUT_TOKENS_CAP = 8192
_MODEL_MAX_OUTPUT_TOKENS_CAPS: dict[str, int] = {
    "deepseek-chat": 8192,
    "deepseek-reasoner": 8192,
    "gpt-4o": 16384,
    "gpt-4o-mini": 16384,
    "gpt-4.1": 32768,
    "gpt-4.1-mini": 32768,
    "o4-mini": 100000,
}


def clamp_max_output_tokens(requested: int, *, model: str | None) -> int:
    """Clamp requested max_tokens to a known-safe per-model ceiling."""
    try:
        value = int(requested)
    except (TypeError, ValueError):
        value = _DEFAULT_MAX_OUTPUT_TOKENS_CAP
    if value <= 0:
        value = _DEFAULT_MAX_OUTPUT_TOKENS_CAP
    model_key = (model or "").strip().lower()
    cap = _MODEL_MAX_OUTPUT_TOKENS_CAPS.get(model_key, _DEFAULT_MAX_OUTPUT_TOKENS_CAP)
    # Unknown OpenAI-compatible models: keep a conservative default unless the
    # request is already below it.
    if model_key not in _MODEL_MAX_OUTPUT_TOKENS_CAPS and "gpt-4.1" in model_key:
        cap = 32768
    elif model_key not in _MODEL_MAX_OUTPUT_TOKENS_CAPS and model_key.startswith("gpt-4o"):
        cap = 16384
    elif model_key not in _MODEL_MAX_OUTPUT_TOKENS_CAPS and model_key.startswith("deepseek"):
        cap = 8192
    return min(value, cap)


def _error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except (JSONDecodeError, ValueError):
        return f"LLM provider returned HTTP {response.status_code}."

    if isinstance(payload, dict):
        error_payload = payload.get("error")
        if isinstance(error_payload, dict):
            message = error_payload.get("message")
            if isinstance(message, str) and message.strip():
                return message.strip()
        message = payload.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
    return f"LLM provider returned HTTP {response.status_code}."


def _sanitize_message(message: str, api_key: str) -> str:
    sanitized = message
    if api_key:
        sanitized = sanitized.replace(api_key, "[redacted]")
    return sanitized
