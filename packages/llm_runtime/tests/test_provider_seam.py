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
    # 2026-08-19：SCOPE 是管线的第一步，它退化会把糊掉的子问题清单传给后面每一步。
    # 生产实测两次 planner 调用第一次零 token 截断，重试才成功，白烧 44.7 秒。
    assert config.thinking_for_role("planner") == "disabled"


def test_a_deployment_that_omits_a_role_still_gets_its_default_policy() -> None:
    """线上的 ``LLM_ROLE_THINKING`` 是一份写死的映射，不会因为代码里多了角色而更新。

    2026-08-19 实测生产配置就是 ``{"extractor","reranker","verifier"}`` 三项，
    其余角色全靠这里的默认档位兜住——所以「默认能不能穿过部署配置」必须有用例锁。
    """
    config = LLMConfig(
        provider="openai",
        role_thinking={"extractor": "disabled", "reranker": "disabled", "verifier": "disabled"},
    )
    assert config.thinking_for_role("writer") == "disabled"
    assert config.thinking_for_role("planner") == "disabled"
    assert config.thinking_for_role("evidence_classifier") == "disabled"
    assert config.thinking_for_role("section_reviewer") == "disabled"


def test_evidence_classifier_inherits_deployed_deepseek_tiers() -> None:
    config = LLMConfig(
        provider="openai",
        role_models={
            "extractor": "deepseek-v4-flash",
            "planner": "deepseek-v4-pro",
        },
    )
    # 2026-08-15：证据判定从 flash 档换到 planner 档，并关掉思考。依据是真实候选集上的
    # 三臂对比——flash+思考开 25% 截断；flash+思考关会退化成「4 个问题 3 个返回 0 条」；
    # pro+思考关 0 截断、链接数翻倍且完全覆盖前者。
    assert config.model_for_role("evidence_classifier") == "deepseek-v4-pro"
    assert config.model_for_role("evidence_classifier_fallback") == "deepseek-v4-pro"
    assert config.thinking_for_role("evidence_classifier") == "disabled"


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


def _capture_payload(model: str, thinking_mode: str) -> dict:
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": model,
                "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAICompatibleLLMProvider(
            base_url="https://open.bigmodel.cn/api/paas/v4",
            api_key="k",
            model=model,
            timeout_seconds=5,
            max_retries=0,
            client=client,
        )
        provider.generate(
            LLMRequest(
                system_prompt="s",
                user_prompt="u",
                model=model,
                max_output_tokens=100,
                thinking_mode=thinking_mode,
            )
        )
    return payloads[0]


def test_glm_never_receives_the_disabled_thinking_deepseek_accepts():
    """GLM-5.3 系只接受 thinking.type=enabled，照搬 deepseek 的 disabled 会被服务商拒绝。

    角色档位里的 "disabled" 必须翻译成 reasoning_effort=low（降档），而不是原样透传。
    """
    payload = _capture_payload("glm-5.3-flash", "disabled")
    assert payload["thinking"] == {"type": "enabled"}
    assert payload["reasoning_effort"] == "low"


def test_glm_enabled_thinking_keeps_the_deep_reasoning_tier():
    payload = _capture_payload("glm-5.3-flash", "enabled")
    assert payload["thinking"] == {"type": "enabled"}
    assert payload["reasoning_effort"] == "max"


def test_glm_5_2_still_gets_the_real_disabled_switch():
    """5.2 **接受** thinking.type=disabled，5.3 才不接受。两代不能共用一个分支。

    把 5.2 的 disabled 也翻成 reasoning_effort=low，等于让一个本可以真正关掉推理的
    模型继续拿输出预算去想——正是 `_apply_thinking_controls()` 要防的那类静默降级。
    """
    payload = _capture_payload("glm-5.2", "disabled")
    assert payload["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in payload


def test_glm_output_budget_is_not_clamped_to_the_deepseek_ceiling():
    """8192 是 deepseek 的上限。套在 GLM 上会让截断重试误判为「加预算没余量」。"""
    assert clamp_max_output_tokens(999_999, model="glm-5.3-flash") == 65_536
    assert clamp_max_output_tokens(999_999, model="glm-5.3") == 65_536
    # 未列名的 GLM-5 快照同样不该掉回 8192。
    assert clamp_max_output_tokens(999_999, model="glm-5.3-flash-250901") == 65_536
    # 管线自己请求的预算不受影响。
    assert clamp_max_output_tokens(8_000, model="glm-5.3-flash") == 8_000


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
    for call in calls:
        events = call.metadata["provider_admission_events"]
        assert [e["event"] for e in events] == ["http_attempt_started", "http_attempt_finished"]
        assert events[0]["timestamp_ms"] <= events[1]["timestamp_ms"]
    assert {k: v for k, v in calls[0].metadata.items() if k != "provider_admission_events"} == {
        "role": "planner",
        "stage": "scope",
    }
    assert {k: v for k, v in calls[1].metadata.items() if k != "provider_admission_events"} == {
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
    # 证据判定同样吃掉了预算：生产 77 次调用 39 次 output_truncated，截断后只能回落到
    # 词汇匹配，而那条路只输出 supports——全库 429 条链接零条 contradicts。
    assert deployed.thinking_for_role("evidence_classifier") == "disabled"


def test_an_explicit_deployment_override_still_wins():
    config = LLMConfig(provider="openai", role_thinking={"experiment_extractor": "enabled"})

    assert config.thinking_for_role("experiment_extractor") == "enabled"


# ---------------------------------------------------------------------------
# 截断重试：一次「注定无效」的重试不该被发出去
# ---------------------------------------------------------------------------


def _truncated_response() -> httpx.Response:
    """推理型模型烧光预算后的返回：正常 HTTP，零 content。"""
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


def _truncating_runner(model: str):
    """把某个角色路由到 ``model``，并让它每次都截断。返回 (runner, budgets, calls)。"""
    from llm_runtime import LLMRunner
    from llm_runtime.providers import OpenAICompatibleLLMProvider

    budgets: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        budgets.append(json.loads(request.content)["max_tokens"])
        return _truncated_response()

    provider = OpenAICompatibleLLMProvider(
        base_url="http://stub/v1",
        api_key="k",
        model=model,
        timeout_seconds=5.0,
        max_retries=0,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    calls: list = []
    runner = LLMRunner(
        LLMConfig(
            provider="openai",
            base_url="http://stub/v1",
            api_key="k",
            role_models={"writer": model},
        ),
        provider=provider,
        on_call=calls.append,
    )
    return runner, budgets, calls


def test_a_retry_that_cannot_grow_the_budget_is_never_sent():
    """8000 上的 deepseek 加倍后被 clamp 到 8192——多 192 token，写不完的还是写不完。

    生产实测：writer 60 次这样的重试里 21 次再次截断，每次白烧一整轮 40-80 秒
    的调用。判定是按模型算的，所以这里断言的是**请求根本没发出去**。
    """
    runner, budgets, calls = _truncating_runner("deepseek-v4-pro")

    result = runner.generate("writer", system_prompt="s", user_prompt="u", max_output_tokens=8000)

    assert result is None
    assert budgets == [8000], "重试不可能改变结果时，第二次请求不该发出"
    assert [c.error_code for c in calls] == ["output_truncated_at_ceiling"]


def test_the_same_budget_does_retry_on_a_model_with_real_headroom():
    """同样的 8000，换到上限 32768 的模型就该重试——上限来自模型，不是常量。"""
    runner, budgets, _ = _truncating_runner("gpt-4.1")

    runner.generate("writer", system_prompt="s", user_prompt="u", max_output_tokens=8000)

    assert budgets == [8000, 16000]


def test_writer_regains_its_truncation_retry_on_glm():
    """writer 在 GLM 上请求 16000（`writing.SECTION_MAX_OUTPUT_TOKENS`）。

    deepseek 的 8192 上限让这次重试永远发不出去；GLM 的 65536 让 16000 → 32000 是
    实打实的 2 倍，于是截断终于有一次补救，紧凑档退居第三道防线。
    """
    runner, budgets, _ = _truncating_runner("glm-5.3-flash")

    runner.generate("writer", system_prompt="s", user_prompt="u", max_output_tokens=16000)

    assert budgets == [16000, 32000]


def test_a_budget_with_headroom_still_retries():
    """verifier 请求 2400，加倍到 4800（2.0 倍）——这类重试救回过 17 次，必须保留。"""
    runner, budgets, calls = _truncating_runner("deepseek-v4-flash")

    runner.generate("writer", system_prompt="s", user_prompt="u", max_output_tokens=2400)

    assert budgets == [2400, 4800]
    # 两次都截断：第一次是真截断，第二次 4800→8192 只涨 1.71 倍…仍在阈值之上，
    # 但 max_attempts 已经用完，所以停在这里而不是继续加。
    assert [c.error_code for c in calls] == ["output_truncated", "output_truncated_at_ceiling"]


def test_a_role_can_switch_the_retry_off_from_configuration():
    from llm_runtime import LLMRunner, TruncationRetryPolicy
    from llm_runtime.providers import OpenAICompatibleLLMProvider

    budgets: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        budgets.append(json.loads(request.content)["max_tokens"])
        return _truncated_response()

    provider = OpenAICompatibleLLMProvider(
        base_url="http://stub/v1",
        api_key="k",
        model="gpt-4.1",
        timeout_seconds=5.0,
        max_retries=0,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    runner = LLMRunner(
        LLMConfig(
            provider="openai",
            base_url="http://stub/v1",
            api_key="k",
            role_models={"writer": "gpt-4.1"},
            role_retry={"writer": TruncationRetryPolicy(max_attempts=0)},
        ),
        provider=provider,
    )

    runner.generate("writer", system_prompt="s", user_prompt="u", max_output_tokens=8000)

    # 预算明明有余量，但部署说了不重试。
    assert budgets == [8000]


def test_a_degenerate_empty_completion_is_retried_by_the_provider():
    """正常终止、零 content、没有 reasoning：服务商的退化完成，重复它是安全的。

    这次尝试没有产生任何持久副作用，所以走 provider 自己的退避重试，而不是像
    ``invalid_response`` 那样一次性放弃。
    """
    from llm_runtime.providers import OpenAICompatibleLLMProvider

    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"finish_reason": "stop", "message": {"role": "assistant", "content": ""}}
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}
                ]
            },
        )

    provider = OpenAICompatibleLLMProvider(
        base_url="http://stub/v1",
        api_key="k",
        model="deepseek-v4-pro",
        timeout_seconds=5.0,
        max_retries=2,
        retry_backoff_seconds=0,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    response = provider.generate(
        LLMRequest(
            system_prompt="s", user_prompt="u", model="deepseek-v4-pro", max_output_tokens=100
        )
    )

    assert response.text == "ok"
    assert calls == 2


def test_an_empty_completion_that_never_recovers_reports_its_own_code():
    from llm_runtime.providers import OpenAICompatibleLLMProvider

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"finish_reason": "stop", "message": {"role": "assistant", "content": ""}}
                ]
            },
        )

    provider = OpenAICompatibleLLMProvider(
        base_url="http://stub/v1",
        api_key="k",
        model="deepseek-v4-pro",
        timeout_seconds=5.0,
        max_retries=1,
        retry_backoff_seconds=0,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(LLMError) as excinfo:
        provider.generate(
            LLMRequest(
                system_prompt="s", user_prompt="u", model="deepseek-v4-pro", max_output_tokens=100
            )
        )
    # 与 `invalid_response`（响应结构坏了）区分开：这一条说的是「结构没问题，
    # 就是什么都没生成」。
    assert excinfo.value.error_code == "empty_response"
    assert excinfo.value.retryable is True


# ---------------------------------------------------------------------------
# 策略解析：配错要吵
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        {"multiplier": 0.5},
        {"multiplier": 99},
        {"multiplier": "2"},
        {"max_attempts": -1},
        {"max_attempts": 99},
        {"max_attempts": 1.5},
        {"min_growth_ratio": 0.5},
        {"min_growth_ratio": 99},
        {"attempts": 1},
        [],
    ],
)
def test_a_bad_retry_policy_is_rejected_at_parse_time(raw):
    """静默退回默认策略会让部署以为自己调过参数，而生产行为一如既往。"""
    from llm_runtime import resolve_truncation_policy

    with pytest.raises(ValueError):
        resolve_truncation_policy(raw, path="LLM_ROLE_RETRY.writer")


def test_a_bad_entry_fails_the_whole_role_retry_map():
    from llm_runtime import parse_role_retry

    with pytest.raises(ValueError, match="LLM_ROLE_RETRY.writer"):
        parse_role_retry({"verifier": {"max_attempts": 1}, "writer": {"max_attempts": 9}})


def test_retry_policy_resolution_prefers_the_deployment_then_the_default():
    from llm_runtime import DEFAULT_TRUNCATION_RETRY_POLICY, TruncationRetryPolicy

    configured = TruncationRetryPolicy(multiplier=3.0, max_attempts=2)
    config = LLMConfig(provider="noop", role_retry={"writer": configured})

    assert config.retry_for_role("writer") is configured
    # 与 thinking_for_role 一样，**不**沿用 ROLE_MODEL_FALLBACKS：共用模型档位的
    # 两个角色输出规模可以完全不同，重试策略必须各自决定。
    assert config.retry_for_role("section_reviewer") == DEFAULT_TRUNCATION_RETRY_POLICY
