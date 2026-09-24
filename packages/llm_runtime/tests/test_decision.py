from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from llm_runtime import DecisionResult, DecisionRunner, validate_decision_response
from llm_runtime.config import ModelPrice


def _questions() -> dict[str, dict]:
    return {
        "relevance": {
            "type": "score",
            "instructions": "Rate semantic support.",
            "criteria": ["none", "weak", "partial", "mostly", "direct"],
        }
    }


def _payload(score: float = 4.0, confidence: float = 0.95) -> dict:
    return {
        "model": "jev-1.13.0",
        "answers": {
            "relevance": {
                "type": "score",
                "score": score,
                "legend": {str(i): s for i, s in enumerate(_questions()["relevance"]["criteria"])},
                "probabilities": {
                    "0": 0.0,
                    "1": 0.0,
                    "2": 0.0,
                    "3": 0.05,
                    "4": 0.95,
                },
                "confidence": confidence,
            }
        },
        "usage": {"input_tokens": 100, "output_tokens": 12},
    }


def test_validate_decision_response_rejects_partial_or_wrong_typed_answers() -> None:
    answers, model, usage, error = validate_decision_response(
        {"model": "jev-1.13.0", "answers": {}, "usage": {}}, questions=_questions()
    )
    assert answers is None and model is None and usage == {} and error == "invalid_answers"


def test_decision_runner_batches_typed_call_and_hits_job_local_cache() -> None:
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        assert request.headers["authorization"] == "Bearer secret"
        return httpx.Response(200, json=_payload(), headers={"x-typesafe-request-id": "req_1"})

    records = []

    async def on_call(record):
        records.append(record)

    runner = DecisionRunner(
        api_key="secret",
        timeout_seconds=1,
        cache={},
        on_call=on_call,
        transport=httpx.MockTransport(handler),
    )

    async def run() -> tuple[DecisionResult, DecisionResult]:
        first = await runner.decide(
            state={"context": "claim", "evidence": "excerpt"},
            questions=_questions(),
            metadata={"stage": "soft_check"},
        )
        second = await runner.decide(
            state={"context": "claim", "evidence": "excerpt"},
            questions=_questions(),
            metadata={"stage": "soft_check"},
        )
        return first, second

    first, second = asyncio.run(run())
    assert first.ok and first.answers["relevance"]["score"] == 4.0
    assert second.ok and second.cache_hit
    assert len(calls) == 1
    assert len(records) == 1
    assert records[0].provider == "typesafe"
    assert records[0].input_tokens == 100
    assert records[0].metadata["decision"] is True


def test_decision_runner_turns_http_errors_into_fallback_results() -> None:
    runner = DecisionRunner(
        api_key="secret",
        timeout_seconds=1,
        transport=httpx.MockTransport(lambda _request: httpx.Response(429)),
    )

    result = asyncio.run(
        runner.decide(state="state", questions={"q": {"type": "noul", "instructions": "yes?"}})
    )
    assert not result.ok
    assert result.error == "http_429"


def test_decision_runner_opens_circuit_after_repeated_provider_failures() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(529)

    runner = DecisionRunner(
        api_key="circuit-secret",
        model="circuit-test",
        failure_threshold=2,
        cooldown_seconds=60,
        transport=httpx.MockTransport(handler),
    )

    async def run():
        questions = {"q": {"type": "noul", "instructions": "yes?"}}
        first = await runner.decide(state="state-1", questions=questions)
        second = await runner.decide(state="state-2", questions=questions)
        third = await runner.decide(state="state-3", questions=questions)
        return first, second, third

    first, second, third = asyncio.run(run())
    assert first.error == "http_529"
    assert second.error == "http_529"
    assert third.error == "circuit_open"
    assert calls == 2


@pytest.mark.parametrize(
    "field,value",
    [
        ("confidence", None),
        ("confidence", True),
        ("confidence", float("nan")),
        ("confidence", float("inf")),
        ("score", float("nan")),
        ("score", False),
        ("legend", None),
        ("legend", {"0": "none"}),
        ("probabilities", {"0": 0.1, "1": 0.1, "2": 0.1, "3": 0.1, "4": 0.1}),
        ("probabilities", {"0": float("nan"), "1": 0, "2": 0, "3": 0, "4": 1}),
    ],
)
def test_invalid_contract_is_rejected(field, value):
    payload = _payload()
    payload["answers"]["relevance"][field] = value
    assert validate_decision_response(payload, questions=_questions())[-1] is not None


def test_zero_confidence_and_rounded_distribution_are_preserved():
    payload = _payload(confidence=0)
    payload["answers"]["relevance"]["probabilities"] = {
        "0": 0.28,
        "1": 0.17,
        "2": 0.27,
        "3": 0.11,
        "4": 0.16,
    }
    answers, _, _, error = validate_decision_response(payload, questions=_questions())
    assert error is None
    assert answers == payload["answers"]


async def test_disabled_cache_does_not_read_or_write_and_accounts_actual_calls():
    records = []

    async def record(value):
        records.append(value)

    runner = DecisionRunner(
        api_key="cache-off",
        cache_enabled=False,
        on_call=record,
        model_prices={"jev-1.13.0": ModelPrice(0.042, 0)},
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=_payload(confidence=0))),
    )
    a = await runner.decide(state="x", questions=_questions())
    b = await runner.decide(state="x", questions=_questions())
    assert a.ok and b.ok and not b.cache_hit
    assert a.raw_payload["answers"]["relevance"]["confidence"] == 0
    assert runner.cache == {}
    assert len(records) == 2
    assert records[0].cost_estimate == pytest.approx(100 * 0.042 / 1_000_000)
    assert "raw_payload" not in records[0].metadata


async def test_timeout_falls_back_and_cancellation_propagates():
    async def slow(_):
        await asyncio.sleep(5)
        return httpx.Response(200, json=_payload())

    runner = DecisionRunner(
        api_key="timeout-check", timeout_seconds=0.1, transport=httpx.MockTransport(slow)
    )
    assert (await runner.decide(state="x", questions=_questions())).error == "timeout"
    task = asyncio.create_task(runner.decide(state="y", questions=_questions()))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
