import json

import httpx
import pytest
from llm_runtime import (
    LLMConfig,
    LLMError,
    LLMRequest,
    OpenAICompatibleLLMProvider,
    clamp_max_output_tokens,
    create_llm_provider,
)
from llm_runtime.config import DEFAULT_ROLE_MODELS


def test_noop_provider_returns_empty_json():
    provider = create_llm_provider(LLMConfig(provider="noop"))
    resp = provider.generate(
        LLMRequest(system_prompt="s", user_prompt="u", model="", max_output_tokens=16)
    )
    assert resp.provider == "noop"
    assert resp.text == "{}"


def test_role_routing_prefers_override_then_default():
    config = LLMConfig(provider="noop", model="fallback", role_models={"writer": "custom-writer"})
    assert config.model_for_role("writer") == "custom-writer"
    assert config.model_for_role("planner") == DEFAULT_ROLE_MODELS["planner"]
    assert config.model_for_role("unknown-role") == "fallback"


def _request() -> LLMRequest:
    return LLMRequest(
        system_prompt="system",
        user_prompt="user",
        model="gpt-4o-mini",
        max_output_tokens=32,
    )


def _provider(client: httpx.Client, *, max_retries: int = 0):
    return OpenAICompatibleLLMProvider(
        base_url="https://llm.example/v1",
        api_key="top-secret",
        model="gpt-4o-mini",
        timeout_seconds=1,
        max_retries=max_retries,
        client=client,
        retry_backoff_seconds=0,
    )


def test_provider_retries_429_then_succeeds():
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, json={"error": {"message": "slow down"}})
        return httpx.Response(
            200,
            json={
                "id": "r1",
                "model": "gpt-4o-mini",
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert _provider(client, max_retries=1).generate(_request()).text == "ok"
    assert calls == 2


@pytest.mark.parametrize(
    ("status", "error_code", "retryable"),
    [(401, "auth_error", False), (429, "rate_limited", True), (503, "server_error", True)],
)
def test_provider_classifies_http_failures(status, error_code, retryable):
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(status, json={"error": {"message": "failed"}})
    )
    with httpx.Client(transport=transport) as client:
        with pytest.raises(LLMError) as caught:
            _provider(client).generate(_request())
    assert caught.value.error_code == error_code
    assert caught.value.retryable is retryable


def test_provider_redacts_api_key_from_error_message():
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            400,
            json={"error": {"message": "rejected top-secret credential"}},
        )
    )
    with httpx.Client(transport=transport) as client:
        with pytest.raises(LLMError) as caught:
            _provider(client).generate(_request())
    assert "top-secret" not in str(caught.value)
    assert "[redacted]" in str(caught.value)


def test_max_output_tokens_are_clamped_by_model():
    assert clamp_max_output_tokens(999_999, model="gpt-4o-mini") == 16_384
    assert clamp_max_output_tokens(999_999, model="unknown") == 8_192


def test_empty_content_with_reasoning_is_reported_as_truncation():
    """推理型模型把思维链计入 max_tokens：预算耗尽时 content 为空。

    这与「响应结构非法」是两回事——前者加预算重试就能救，必须区分开。
    """
    import httpx
    from llm_runtime.providers import OpenAICompatibleLLMProvider
    from llm_runtime.types import LLMError, LLMRequest

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "reasoning_content": "思考了很久但没来得及输出正文",
                        },
                    }
                ]
            },
        )

    provider = OpenAICompatibleLLMProvider(
        base_url="http://stub/v1",
        api_key="k",
        model="deepseek-v4-pro",
        timeout_seconds=5.0,
        max_retries=0,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    try:
        provider.generate(
            LLMRequest(
                system_prompt="s",
                user_prompt="u",
                model="deepseek-v4-pro",
                max_output_tokens=100,
            )
        )
    except LLMError as error:
        assert error.error_code == "output_truncated"
        assert error.retryable is True
    else:
        raise AssertionError("expected LLMError")


def test_runner_retries_once_with_doubled_budget_on_truncation():
    import httpx
    from llm_runtime import LLMConfig, LLMRunner

    budgets: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        budgets.append(payload["max_tokens"])
        if len(budgets) == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "length",
                            "message": {
                                "role": "assistant",
                                "content": "",
                                "reasoning_content": "…",
                            },
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "ok"},
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )

    from llm_runtime.providers import OpenAICompatibleLLMProvider

    provider = OpenAICompatibleLLMProvider(
        base_url="http://stub/v1",
        api_key="k",
        model="deepseek-v4-pro",
        timeout_seconds=5.0,
        max_retries=0,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    calls = []
    runner = LLMRunner(
        LLMConfig(provider="openai", base_url="http://stub/v1", api_key="k"),
        provider=provider,
        on_call=calls.append,
    )
    response = runner.generate(
        "planner", system_prompt="s", user_prompt="u", max_output_tokens=1000
    )
    assert response is not None and response.text == "ok"
    assert budgets == [1000, 2000]
    # 两次调用都记账：失败那次带 error_code，成本面板才看得见。
    assert [c.error_code for c in calls] == ["output_truncated", None]


def test_runner_does_not_retry_forever_on_truncation():
    import httpx
    from llm_runtime import LLMConfig, LLMRunner
    from llm_runtime.providers import OpenAICompatibleLLMProvider

    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {"role": "assistant", "content": "", "reasoning_content": "…"},
                    }
                ]
            },
        )

    provider = OpenAICompatibleLLMProvider(
        base_url="http://stub/v1",
        api_key="k",
        model="deepseek-v4-pro",
        timeout_seconds=5.0,
        max_retries=0,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    runner = LLMRunner(
        LLMConfig(provider="openai", base_url="http://stub/v1", api_key="k"),
        provider=provider,
    )
    result = runner.generate(
        "planner", system_prompt="s", user_prompt="u", max_output_tokens=1000
    )
    assert result is None
    # 最多重试一次：绝不无界重试烧钱。
    assert len(attempts) == 2
