"""成本记账的定价层（P1-4）。

在此之前 `LlmCallRecord.cost_estimate` 永远是 None，成本面板于是把「不知道花了
多少」显示成 `$0.00`。这组用例守住的核心区分就是这一条：**算不出来必须留 None，
绝不写 0.0**——否则未定价的模型会让账单看起来免费。
"""

from __future__ import annotations

import pytest
from llm_runtime import (
    LLMCallRecord,
    LLMConfig,
    LLMRequest,
    LLMResponse,
    LLMRunner,
    ModelPrice,
    parse_model_prices,
)

PRICES = {"deepseek-v4-pro": {"input": 0.27, "output": 1.10}}


def _config(**kwargs) -> LLMConfig:
    return LLMConfig(model_prices=parse_model_prices(PRICES), **kwargs)


def test_cost_is_computed_per_million_tokens():
    cost = _config().estimate_cost(
        "deepseek-v4-pro", input_tokens=1_000_000, output_tokens=500_000
    )

    assert cost == pytest.approx(0.27 + 0.55)


def test_an_unpriced_model_yields_none_not_zero():
    """这正是 P1-4 的要害：0.0 会被面板当成"免费"，None 才是"不知道"。"""
    assert _config().estimate_cost("gpt-4o", input_tokens=1000, output_tokens=1000) is None


def test_missing_usage_yields_none_even_for_a_priced_model():
    """provider 没回 usage 时同样算不出金额，不能拿 0 顶上。"""
    assert _config().estimate_cost("deepseek-v4-pro", input_tokens=None, output_tokens=None) is None


def test_one_missing_side_still_prices_the_other():
    cost = _config().estimate_cost("deepseek-v4-pro", input_tokens=1_000_000, output_tokens=None)

    assert cost == pytest.approx(0.27)


def test_a_version_suffix_still_matches_its_base_price():
    """服务商挂日期后缀是常态；否则每次发新快照成本面板就静默退回未定价。"""
    assert _config().price_for_model("deepseek-v4-pro-0711") == ModelPrice(0.27, 1.10)


def test_the_longest_prefix_wins():
    config = LLMConfig(
        model_prices=parse_model_prices(
            {
                "deepseek": {"input": 1.0, "output": 1.0},
                "deepseek-v4-pro": {"input": 0.27, "output": 1.10},
            }
        )
    )

    assert config.price_for_model("deepseek-v4-pro-0711") == ModelPrice(0.27, 1.10)
    assert config.price_for_model("deepseek-v4-flash") == ModelPrice(1.0, 1.0)


def test_model_lookup_is_case_insensitive_and_trimmed():
    assert _config().price_for_model("  DeepSeek-V4-Pro ") == ModelPrice(0.27, 1.10)


def test_an_unconfigured_deployment_prices_nothing():
    assert LLMConfig().price_for_model("deepseek-v4-pro") is None


def test_a_malformed_entry_is_skipped_without_dropping_the_rest():
    """漏配一个模型只该让那个模型未定价，不该把整份价目表一起关掉。"""
    prices = parse_model_prices(
        {
            "good": {"input": 1.0, "output": 2.0},
            "no-output": {"input": 1.0},
            "not-a-dict": 3.0,
            "negative": {"input": -1.0, "output": 1.0},
            "text": {"input": "free", "output": "free"},
            42: {"input": 1.0, "output": 1.0},
        }
    )

    assert set(prices) == {"good"}


def test_a_non_mapping_price_table_degrades_to_empty():
    assert parse_model_prices(None) == {}
    assert parse_model_prices("{}") == {}
    assert parse_model_prices([{"input": 1.0}]) == {}


def test_the_long_form_key_names_are_accepted():
    prices = parse_model_prices({"m": {"input_per_mtok": 0.5, "output_per_mtok": 1.5}})

    assert prices["m"] == ModelPrice(0.5, 1.5)


def test_a_zero_price_is_a_real_price_not_a_missing_one():
    """自托管模型可以真的是 0 元；那必须与"未定价"区分开。"""
    config = LLMConfig(model_prices=parse_model_prices({"local": {"input": 0.0, "output": 0.0}}))

    assert config.estimate_cost("local", input_tokens=1000, output_tokens=1000) == 0.0
    assert config.estimate_cost("other", input_tokens=1000, output_tokens=1000) is None


# --- runner 侧：记账真的写进了记录 ------------------------------------------


class _StubProvider:
    """按需返回 usage 的假 provider；有些真实 provider 确实不回。"""

    name = "stub"

    def __init__(self, usage: dict | None) -> None:
        self._usage = usage

    def generate(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(
            text="{}",
            provider=self.name,
            model=request.model,
            usage=self._usage,
        )


def _run(usage: dict | None, prices: dict) -> LLMCallRecord:
    records: list[LLMCallRecord] = []
    runner = LLMRunner(
        LLMConfig(
            provider="stub",
            role_models={"planner": "deepseek-v4-pro"},
            model_prices=parse_model_prices(prices),
        ),
        provider=_StubProvider(usage),
        on_call=records.append,
    )
    runner.generate("planner", system_prompt="s", user_prompt="u")
    assert len(records) == 1
    return records[0]


def test_the_runner_records_a_cost_for_a_priced_model():
    record = _run({"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000}, PRICES)

    assert record.input_tokens == 1_000_000
    assert record.cost_estimate == pytest.approx(0.27 + 1.10)


def test_the_runner_leaves_cost_none_for_an_unpriced_model():
    record = _run({"prompt_tokens": 1000, "completion_tokens": 10}, {})

    assert record.input_tokens == 1000
    assert record.cost_estimate is None


def test_the_runner_leaves_cost_none_when_the_provider_reports_no_usage():
    record = _run(None, PRICES)

    assert record.input_tokens is None
    assert record.cost_estimate is None
