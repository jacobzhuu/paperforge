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
    assert config.thinking_for_role("extractor") == "disabled"
    assert config.thinking_for_role("reranker") == "disabled"
    assert config.thinking_for_role("writer") == "disabled"


def test_evidence_classifier_inherits_deployed_deepseek_tiers() -> None:
    config = LLMConfig(
        provider="openai",
        role_models={
            "extractor": "deepseek-v4-flash",
            "planner": "deepseek-v4-pro",
        },
    )
    assert config.model_for_role("evidence_classifier") == "deepseek-v4-flash"
    assert config.model_for_role("evidence_classifier_fallback") == "deepseek-v4-pro"
    assert config.thinking_for_role("evidence_classifier") is None


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


def test_empty_content_with_reasoning_skips_same_budget_provider_retries():
    """推理型模型把思维链计入 max_tokens：预算耗尽时 content 为空。

    这与「响应结构非法」是两回事——前者加预算重试就能救，必须区分开。
    """
    import httpx
    from llm_runtime.providers import OpenAICompatibleLLMProvider
    from llm_runtime.types import LLMError, LLMRequest

    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
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
        max_retries=2,
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
        assert error.retryable is False
    else:
        raise AssertionError("expected LLMError")
    assert calls == 1


def test_deepseek_structured_request_sets_json_and_disables_thinking():
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": "deepseek-v4-flash",
                "choices": [
                    {
                        "message": {"content": '{"ok":true}'},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAICompatibleLLMProvider(
            base_url="https://api.deepseek.com",
            api_key="k",
            model="deepseek-v4-flash",
            timeout_seconds=5,
            max_retries=0,
            client=client,
        )
        response = provider.generate(
            LLMRequest(
                system_prompt="return JSON",
                user_prompt="extract",
                model="deepseek-v4-flash",
                max_output_tokens=100,
                json_output=True,
                thinking_mode="disabled",
            )
        )
    assert response.text == '{"ok":true}'
    assert payloads == [
        {
            "model": "deepseek-v4-flash",
            "messages": [
                {"role": "system", "content": "return JSON"},
                {"role": "user", "content": "extract"},
            ],
            "max_tokens": 100,
            "temperature": 0.0,
            "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"},
        }
    ]


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
        "planner",
        system_prompt="s",
        user_prompt="u",
        max_output_tokens=1000,
        metadata={"stage": "scope"},
    )
    assert response is not None and response.text == "ok"
    assert budgets == [1000, 2000]
    # 两次调用都记账：失败那次带 error_code，成本面板才看得见。
    assert [c.error_code for c in calls] == ["output_truncated", None]
    assert [c.provider for c in calls] == ["openai", "openai-compatible"]
    assert calls[0].metadata == {"role": "planner", "stage": "scope"}
    assert calls[1].metadata == {
        "role": "planner",
        "stage": "scope",
        "retry": "output_truncated",
    }
    assert calls[0].occurred_at <= calls[1].occurred_at


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
    result = runner.generate("planner", system_prompt="s", user_prompt="u", max_output_tokens=1000)
    assert result is None
    # 最多重试一次：绝不无界重试烧钱。
    assert len(attempts) == 2


@pytest.mark.asyncio
async def test_json_truncation_recovery_has_one_shared_retry_budget():
    """JSON parsing must not start a second retry sequence after ``generate`` already retried."""
    import httpx
    from llm_runtime import LLMConfig, LLMRunner
    from llm_runtime.providers import OpenAICompatibleLLMProvider

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
                        "finish_reason": "length",
                        "message": {"role": "assistant", "content": '{"items": ['},
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

    result = await runner.agenerate_json(
        "planner",
        system_prompt="s",
        user_prompt="u",
        max_output_tokens=1000,
    )

    assert result.ok is False
    assert result.error == "invalid_json: JSONDecodeError"
    assert budgets == [1000, 2000]


def test_a_new_role_inherits_the_built_in_thinking_policy():
    """部署的 LLM_ROLE_THINKING 是写死的全量映射，不会因为代码新增角色而更新。

    生产实测：`experiment_extractor` 因此一直开着思考跑，26 次调用里 15 次
    output_truncated，13 篇论文有 6 篇一条结果都没抽出来。
    """
    deployed = LLMConfig(
        provider="openai",
        role_thinking={"extractor": "disabled", "reranker": "disabled"},
    )

    assert deployed.thinking_for_role("experiment_extractor") == "disabled"
    # 写作同理：线上那份映射只列了 extractor/reranker/verifier，writer 于是一直
    # 开着思考跑，21 次调用 11 次零内容返回，5 节正文因此从未经过模型。
    assert deployed.thinking_for_role("writer") == "disabled"
    # 综合是推理，不该被顺手关掉。
    assert deployed.thinking_for_role("synthesizer") is None
    # qmatrix 分类与卡片抽取共用模型档位但思考策略分开，这一条不能被回退改掉。
    assert deployed.thinking_for_role("evidence_classifier") is None


def test_an_explicit_deployment_override_still_wins():
    config = LLMConfig(provider="openai", role_thinking={"experiment_extractor": "enabled"})

    assert config.thinking_for_role("experiment_extractor") == "enabled"
