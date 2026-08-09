from __future__ import annotations

import httpx

from llm_runtime.config import LLMConfig
from llm_runtime.providers import (
    LLMProvider,
    NoopLLMProvider,
    OpenAICompatibleLLMProvider,
    UnsupportedLLMProvider,
)
from llm_runtime.types import LLMError, LLMRequest

# 迁移自 DeepSearch services/orchestrator/app/llm/client.py。
# 改动：以 LLMConfig 显式注入取代 app.settings.Settings 全局依赖（方案 §5 解耦步）。

# Error codes that mean the provider endpoint is effectively unusable. Parse-shape
# codes (invalid_json/invalid_response) are deliberately absent: they prove the
# endpoint answered, which is all a reachability probe needs to know.
_PREFLIGHT_FATAL_ERROR_CODES = frozenset(
    {
        "timeout",
        "network_error",
        "auth_error",
        "rate_limited",
        "server_error",
        "http_error",
        "configuration_error",
        "unsupported_provider",
    }
)


def create_llm_provider(
    config: LLMConfig,
    *,
    client: httpx.Client | None = None,
) -> LLMProvider:
    normalized_provider = config.provider.strip().lower() or "noop"
    if normalized_provider == "noop":
        return NoopLLMProvider()
    if normalized_provider in {"openai", "openai-compatible", "openai_compatible"}:
        return OpenAICompatibleLLMProvider(
            base_url=config.base_url,
            api_key=config.api_key,
            model=config.model,
            timeout_seconds=config.timeout_seconds,
            max_retries=config.max_retries,
            client=client,
            trust_env_proxy=config.trust_env_proxy,
            total_deadline_seconds=config.total_deadline_seconds,
            retry_backoff_seconds=config.retry_backoff_seconds,
        )
    return UnsupportedLLMProvider(config.provider.strip())


def preflight_llm_provider(config: LLMConfig) -> str | None:
    """Probe the configured LLM once before committing to a long pipeline run.

    Returns a short error description when the endpoint is unreachable or
    rejects a minimal request (network, auth, quota, server errors), and None
    when the provider is healthy — or when no real provider is configured
    (disabled/noop), which callers treat as an intentional deterministic run.
    """
    if not config.enabled:
        return None
    normalized = config.provider.strip().lower() or "noop"
    if normalized == "noop":
        return None
    import dataclasses

    probe_config = dataclasses.replace(
        config,
        timeout_seconds=min(15.0, float(config.timeout_seconds or 15.0)),
        total_deadline_seconds=20.0,
        max_retries=1,
        retry_backoff_seconds=0.5,
    )
    provider = create_llm_provider(probe_config)
    request = LLMRequest(
        system_prompt="You are a connectivity probe. Reply with the single word OK.",
        user_prompt="ping",
        # 探活也要用真实会用到的模型（角色路由下 config.model 可能为空）。
        model=config.model or config.model_for_role("writer"),
        max_output_tokens=16,
        temperature=0.0,
        thinking_mode="disabled",
        metadata={"purpose": "preflight"},
    )
    try:
        provider.generate(request)
    except LLMError as error:
        if error.error_code in _PREFLIGHT_FATAL_ERROR_CODES:
            return f"{error.error_code}: {error}"
        return None
    except Exception as error:  # noqa: BLE001 - any other crash means not usable
        return f"{type(error).__name__}: {error}"
    return None
