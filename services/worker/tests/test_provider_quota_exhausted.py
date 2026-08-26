"""账户余额/配额耗尽必须说自己是余额问题。

真实样本（生产任务 b667a934，2026-08-22）：DeepSeek 账户欠费，104 次 writer 调用
全部失败，每次不到一秒。当时的分类把 402 归进通用 ``http_error``，任务落在
``needs_input``，用户读到的是「11 个章节在重试与自动修复之后仍然没有正文」——
一份关于内容的诊断，而真正该做的事（充值）一个字都没提。
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from llm_runtime import (
    QUOTA_EXHAUSTED,
    LLMCallRecord,
    LLMRequest,
    OpenAICompatibleLLMProvider,
    is_quota_exhausted,
)
from llm_runtime.types import LLMError

#: 生产里 DeepSeek 实际返回的正文，逐字。
REAL_402_BODY = {
    "error": {
        "message": "Insufficient Balance",
        "type": "unknown_error",
        "param": None,
        "code": "invalid_request_error",
    }
}


def _provider(handler, *, max_retries: int = 2) -> OpenAICompatibleLLMProvider:
    return OpenAICompatibleLLMProvider(
        base_url="http://stub/v1",
        api_key="k",
        model="deepseek-v4-pro",
        timeout_seconds=5.0,
        max_retries=max_retries,
        retry_backoff_seconds=0,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def _request() -> LLMRequest:
    return LLMRequest(
        system_prompt="s", user_prompt="u", model="deepseek-v4-pro", max_output_tokens=100
    )


# --- 分类 --------------------------------------------------------------------


def test_the_real_production_402_is_classified_as_a_quota_failure():
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(402, json=REAL_402_BODY)

    with pytest.raises(LLMError) as excinfo:
        _provider(handler).generate(_request())

    error = excinfo.value
    assert error.error_code == QUOTA_EXHAUSTED
    assert error.status_code == 402
    # 行为不变：充值之前重试多少次都是同一个回答，所以一次就够。
    assert error.retryable is False
    assert calls == 1


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (402, "anything at all"),
        (429, "Insufficient Balance"),
        (400, "You exceeded your current quota"),
        (403, "billing hard limit reached"),
        (400, "Your credits are exhausted"),
        (400, "out of credits"),
        (400, "账户余额不足，请充值后重试"),
        (400, "配额不足"),
    ],
)
def test_wording_and_status_are_both_recognised(status, message):
    assert is_quota_exhausted(status, message) is True


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (429, "Rate limit reached for requests"),
        (401, "Invalid API key"),
        (500, "internal server error"),
        (400, "max_tokens must be a positive integer"),
        (None, ""),
    ],
)
def test_ordinary_failures_are_not_mistaken_for_a_billing_problem(status, message):
    assert is_quota_exhausted(status, message) is False


def test_a_rate_limit_that_is_really_a_balance_problem_is_not_retried():
    """正文写着「余额不足」的 429 是余额耗尽，不是限流。

    按限流去重试只会一直撞同一堵墙——服务商用哪个状态码表达欠费并不统一。
    """
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, json={"error": {"message": "Insufficient Balance"}})

    with pytest.raises(LLMError) as excinfo:
        _provider(handler).generate(_request())

    assert excinfo.value.error_code == QUOTA_EXHAUSTED
    assert calls == 1, "余额问题不该消耗限流重试预算"


def test_a_genuine_rate_limit_still_retries():
    """放宽分类不能把真正的限流也变成不可重试。"""
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, json={"error": {"message": "Rate limit reached"}})
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}
                ]
            },
        )

    assert _provider(handler).generate(_request()).text == "ok"
    assert calls == 2


def test_the_api_key_never_reaches_the_error_message():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(402, json={"error": {"message": "Insufficient Balance for key k"}})

    with pytest.raises(LLMError) as excinfo:
        _provider(handler).generate(_request())
    assert "k" not in str(excinfo.value).replace("key", "").replace("Insufficient", "")


# --- 收尾：任务状态与用户可见诊断 ----------------------------------------------


def _record(error_code: str | None, role: str = "writer") -> LLMCallRecord:
    return LLMCallRecord(
        role=role,
        model="deepseek-v4-pro",
        provider="openai-compatible",
        error_code=error_code,
        occurred_at=datetime.now(UTC),
    )


def _context(records: list[LLMCallRecord]):
    from paperforge_worker.context import JobContext

    context = JobContext(
        project_id=uuid.uuid4(),
        job_id=None,  # job_id=None 时 emit 只打日志，不必造 generation_job
        settings=SimpleNamespace(),  # type: ignore[arg-type]
        session_factory=None,  # type: ignore[arg-type]
        http_client=None,  # type: ignore[arg-type]
        scholar_cache=None,
    )
    context.llm_calls.extend(records)
    return context


def test_a_run_with_no_billing_failure_reports_nothing():
    context = _context([_record(None), _record("output_truncated"), _record("timeout")])
    assert context.provider_quota_failure() is None


def test_a_billing_failure_is_found_and_counted():
    context = _context(
        [_record(None), _record(QUOTA_EXHAUSTED), _record(QUOTA_EXHAUSTED, role="planner")]
    )
    quota = context.provider_quota_failure()
    assert quota is not None
    assert quota["affected_calls"] == 2
    # 第一条的角色，用来说清楚是哪一步先撞上的。
    assert quota["role"] == "writer"
    assert quota["provider"] == "openai-compatible"


async def test_an_empty_manuscript_caused_by_billing_says_so_instead_of_blaming_the_sections():
    """这一条守的就是生产任务 b667a934 报错的那句话。"""
    from paperforge_worker.worker import _finish_incomplete

    emitted: list[tuple[str, dict]] = []
    context = _context([_record(QUOTA_EXHAUSTED)] * 104)

    async def _emit(event, payload, **_kw):
        emitted.append((event, payload))

    context.emit = _emit  # type: ignore[assignment]

    write_outcome = SimpleNamespace(
        incomplete_sections=[{"section": f"s{i}", "defects": ["not_generated"]} for i in range(11)]
    )
    await _finish_incomplete(context, write_outcome, None)

    assert len(emitted) == 1
    event, payload = emitted[0]
    assert event == "job.failed"
    assert payload["status"] == "failed", "余额问题不是 needs_input：修复入口此刻必然再失败"
    assert payload["readiness_status"] == "provider_quota_exhausted"
    blocker = payload["blockers"][0]
    assert blocker["code"] == "provider_quota_exhausted"
    assert blocker["failed_calls"] == 104
    assert "余额" in blocker["message"] or "配额" in blocker["message"]
    assert "章节" not in blocker["message"], "不要再把账单问题说成章节写不出来"
    assert payload["failure"]["error_code"] == QUOTA_EXHAUSTED


async def test_an_incomplete_manuscript_without_a_billing_failure_reports_as_before():
    """放宽只作用于余额那一支；内容原因造成的缺章仍然按原样报告。"""
    from paperforge_worker.worker import _finish_incomplete

    emitted: list[tuple[str, dict]] = []
    context = _context([_record(None), _record("output_truncated")])

    async def _emit(event, payload, **_kw):
        emitted.append((event, payload))

    context.emit = _emit  # type: ignore[assignment]

    write_outcome = SimpleNamespace(
        incomplete_sections=[{"section": "s1", "defects": ["not_generated"]}]
    )
    await _finish_incomplete(context, write_outcome, None)

    event, payload = emitted[0]
    assert event == "job.needs_input"
    assert payload["status"] == "needs_input"
    assert payload["blockers"][0]["code"] == "section_not_generated"


def test_the_blocker_message_survives_the_json_round_trip_the_ui_reads():
    """前端渲染的是 blocker.message；它要能原样穿过 error_json。"""
    payload = {
        "blockers": [
            {"code": "provider_quota_exhausted", "message": "模型服务商账户余额或配额已耗尽（x）"}
        ]
    }
    restored = json.loads(json.dumps(payload, ensure_ascii=False))
    assert restored["blockers"][0]["message"].startswith("模型服务商账户余额")


async def test_a_pre_write_evidence_gate_blocked_by_billing_says_so():
    """写作前的证据闸门也要认出余额问题。

    真实样本（job 44641362，2026-08-26）：账户欠费 → SCOPE 拿不到英文检索词 →
    用中文原句检索、arXiv 拒收 CJK → 一篇都没选中 → 报「可回答问题的全文证据不足
    两个独立文献来源」。那是降级的后果，不是原因。
    """
    from paperforge_worker.worker import _finish_needs_input

    emitted: list[tuple[str, dict]] = []
    context = _context([_record(QUOTA_EXHAUSTED, role="planner")] * 3)

    async def _emit(event, payload, **_kw):
        emitted.append((event, payload))

    context.emit = _emit  # type: ignore[assignment]

    readiness = SimpleNamespace(
        readiness_status="evidence_insufficient",
        blockers=[{"code": "evidence_source_diversity_low", "message": "可回答问题的全文证据不足"}],
        report_id=None,
    )
    await _finish_needs_input(context, readiness)

    event, payload = emitted[0]
    assert event == "job.failed"
    assert payload["status"] == "failed"
    assert payload["blockers"][0]["code"] == "provider_quota_exhausted"
    assert "证据" not in payload["blockers"][0]["message"], "不要把账单问题说成证据不足"


async def test_a_genuine_evidence_shortfall_still_reports_as_needs_input():
    """没有余额问题时，证据不足照旧按原样报告。"""
    from paperforge_worker.worker import _finish_needs_input

    emitted: list[tuple[str, dict]] = []
    context = _context([_record(None), _record("output_truncated")])

    async def _emit(event, payload, **_kw):
        emitted.append((event, payload))

    context.emit = _emit  # type: ignore[assignment]

    readiness = SimpleNamespace(
        readiness_status="evidence_insufficient",
        blockers=[{"code": "evidence_source_diversity_low", "message": "证据不足"}],
        report_id=None,
    )
    await _finish_needs_input(context, readiness)

    event, payload = emitted[0]
    assert event == "job.needs_input"
    assert payload["status"] == "needs_input"
    assert payload["blockers"][0]["code"] == "evidence_source_diversity_low"
