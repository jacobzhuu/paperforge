"""Typed decision runtime for System One providers such as TypeSafe Jev.

This module deliberately does not share the chat-completions provider seam.  Jev
returns typed answers under a caller-defined question map, so treating it as an
``LLMResponse`` would lose validation and make fallback behavior ambiguous.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Any

import httpx

from llm_runtime.config import ModelPrice
from llm_runtime.runner import LLMCallRecord
from llm_runtime.types import LLMRequest

DecisionBeforeCall = Callable[[LLMRequest], Awaitable[dict[str, Any] | None]]
DecisionOnCall = Callable[[LLMCallRecord], Awaitable[None]]

_CIRCUIT_LOCK = threading.Lock()
_CIRCUITS: dict[str, dict[str, float | int | bool]] = {}


@dataclass(frozen=True)
class DecisionResult:
    """A validated System One response or a recoverable failure."""

    answers: dict[str, dict[str, Any]] | None = None
    model: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    status_code: int | None = None
    request_id: str | None = None
    cache_hit: bool = False
    latency_ms: int | None = None
    raw_payload: Any = field(default=None, repr=False)

    @property
    def ok(self) -> bool:
        return self.answers is not None and self.error is None


def _json_bytes(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def decision_cache_key(*, model: str, state: Any, questions: dict[str, Any], version: str) -> str:
    """Return a stable, secret-free key for a job-local decision cache."""

    payload = _json_bytes(
        {"version": version, "model": model, "state": state, "questions": questions}
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _validate_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and 0.0 <= float(value) <= 1.0
    )


def _valid_distribution(probabilities: Any, keys: set[str]) -> bool:
    # The API rounds probabilities to two decimals. Preserve the original numbers;
    # allow only the maximum rounding error, never silently renormalize them.
    return (
        isinstance(probabilities, dict)
        and set(probabilities) == keys
        and bool(keys)
        and all(_validate_number(value) for value in probabilities.values())
        and abs(math.fsum(probabilities.values()) - 1.0) <= 0.005 * len(keys) + 1e-6
    )


def _validate_answer(question: Any, answer: Any) -> dict[str, Any] | None:
    if not isinstance(question, dict) or not isinstance(answer, dict):
        return None
    kind = question.get("type")
    if answer.get("type") != kind:
        return None
    if kind == "noul":
        if not _validate_number(answer.get("noul")):
            return None
    elif kind == "choice":
        criteria = question.get("criteria")
        choice = answer.get("choice")
        probabilities = answer.get("probabilities")
        if not isinstance(criteria, dict) or not isinstance(choice, str) or choice not in criteria:
            return None
        if not isinstance(probabilities, dict) or not _validate_number(answer.get("confidence")):
            return None
        if not _valid_distribution(probabilities, set(criteria)):
            return None
    elif kind == "score":
        criteria = question.get("criteria")
        if not isinstance(criteria, list) or len(criteria) < 2 or len(criteria) > 10:
            return None
        score = answer.get("score")
        confidence = answer.get("confidence")
        probabilities = answer.get("probabilities")
        legend = answer.get("legend")
        keys = {str(i) for i in range(len(criteria))}
        if (
            not isinstance(score, (int, float))
            or isinstance(score, bool)
            or not 0.0 <= float(score) <= len(criteria) - 1
            or not _validate_number(confidence)
            or not _valid_distribution(probabilities, keys)
            or not isinstance(legend, dict)
            or set(legend) != keys
            or any(not isinstance(value, str) for value in legend.values())
            or any(
                isinstance(criterion, str) and legend[str(i)] != criterion
                for i, criterion in enumerate(criteria)
            )
        ):
            return None
    else:
        return None
    return dict(answer)


def validate_decision_response(
    payload: Any,
    *,
    questions: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]] | None, str | None, dict[str, Any], str | None]:
    """Validate the complete response; partial answers are not cacheable."""

    if not isinstance(payload, dict):
        return None, None, {}, "invalid_response"
    answers = payload.get("answers")
    if not isinstance(answers, dict) or set(answers) != set(questions):
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        return None, None, usage, "invalid_answers"
    normalized: dict[str, dict[str, Any]] = {}
    for key, question in questions.items():
        answer = _validate_answer(question, answers.get(key))
        if answer is None:
            usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
            return None, None, usage, f"invalid_answer:{key}"
        normalized[key] = answer
    usage = payload.get("usage")
    if usage is not None and not isinstance(usage, dict):
        usage = {}
    return normalized, str(payload.get("model") or "") or None, usage or {}, None


class DecisionRunner:
    """Small async client for a typed decision endpoint.

    ``before_call`` and ``on_call`` are async because the worker uses them to
    share durable call budgets and the existing LLM ledger.  HTTP failures never
    raise into a pipeline; callers decide whether to use the fallback path.
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.typesafe.ai/v1/systemone",
        model: str = "jev-1.13.0",
        timeout_seconds: float = 2.0,
        max_concurrency: int = 2,
        failure_threshold: int = 5,
        cooldown_seconds: float = 60.0,
        cache: dict[str, DecisionResult] | None = None,
        cache_enabled: bool = True,
        cache_version: str = "jev-v2",
        model_prices: dict[str, ModelPrice] | None = None,
        before_call: DecisionBeforeCall | None = None,
        on_call: DecisionOnCall | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.api_key = api_key.strip()
        self.base_url = base_url.strip()
        self.model = model.strip() or "jev-1.13.0"
        self.timeout_seconds = max(0.1, float(timeout_seconds))
        self.failure_threshold = max(1, int(failure_threshold))
        self.cooldown_seconds = max(0.1, float(cooldown_seconds))
        self.cache = cache if cache is not None else {}
        self.cache_enabled = cache_enabled
        self.cache_version = cache_version
        self.model_prices = model_prices or {}
        self.before_call = before_call
        self.on_call = on_call
        self.transport = transport
        self._semaphore = asyncio.Semaphore(max(1, int(max_concurrency)))
        self._circuit_key = hashlib.sha256(
            f"{self.base_url}\n{self.model}\n{hashlib.sha256(self.api_key.encode()).hexdigest()}".encode()
        ).hexdigest()

    @property
    def enabled(self) -> bool:
        return bool(self.api_key and self.base_url and self.model)

    async def decide(
        self,
        *,
        state: Any,
        questions: dict[str, Any],
        metadata: dict[str, Any] | None = None,
        cache_key: str | None = None,
    ) -> DecisionResult:
        if not self.enabled:
            return DecisionResult(error="disabled")
        if not isinstance(questions, dict) or not questions:
            return DecisionResult(error="invalid_questions")
        key = cache_key or decision_cache_key(
            model=self.model, state=state, questions=questions, version=self.cache_version
        )
        cached = self.cache.get(key) if self.cache_enabled else None
        if cached is not None and cached.ok:
            return DecisionResult(
                answers=cached.answers,
                model=cached.model,
                usage=cached.usage,
                request_id=cached.request_id,
                cache_hit=True,
                raw_payload=cached.raw_payload,
            )
        if not self._circuit_admit():
            return DecisionResult(error="circuit_open")

        # The existing durable budget counts decision calls too.  Use the same
        # request shape only for admission/accounting; it is never sent to Jev.
        request = LLMRequest(
            system_prompt=_json_bytes(questions),
            user_prompt=_json_bytes(state),
            model=self.model,
            max_output_tokens=0,
            metadata={"role": "decision", **(metadata or {})},
        )
        admission: dict[str, Any] = {}
        if self.before_call is not None:
            try:
                result = await self.before_call(request)
            except BaseException:
                self._circuit_release()
                raise
            if result is None:
                self._circuit_release()
                return DecisionResult(error="budget_exhausted")
            admission = result

        started = time.monotonic()
        result: DecisionResult
        try:
            result = await asyncio.wait_for(
                self._request(state=state, questions=questions), timeout=self.timeout_seconds
            )
        except TimeoutError:
            result = DecisionResult(error="timeout")
        except asyncio.CancelledError:
            self._circuit_release()
            raise
        except Exception as error:  # noqa: BLE001 - provider failures are fallback signals
            result = DecisionResult(error=type(error).__name__)

        latency_ms = int((time.monotonic() - started) * 1000)
        result = replace(result, latency_ms=latency_ms)
        price = self.model_prices.get(result.model or self.model)
        input_tokens = _int_or_none(result.usage.get("input_tokens"))
        output_tokens = _int_or_none(result.usage.get("output_tokens"))
        record = LLMCallRecord(
            role="decision",
            model=result.model or self.model,
            provider="typesafe",
            input_tokens=_int_or_none(result.usage.get("input_tokens")),
            output_tokens=_int_or_none(result.usage.get("output_tokens")),
            latency_ms=latency_ms,
            cost_estimate=(
                price.estimate(input_tokens=input_tokens, output_tokens=output_tokens)
                if price
                and input_tokens is not None
                and (output_tokens is not None or price.output_per_mtok == 0)
                else None
            ),
            error_code=result.error,
            prompt_sha256=hashlib.sha256(
                (_json_bytes(questions) + "\n" + _json_bytes(state)).encode()
            ).hexdigest(),
            prompt_chars=len(_json_bytes(questions)) + len(_json_bytes(state)),
            metadata={
                **(metadata or {}),
                **admission,
                "decision": True,
                "request_id": result.request_id,
                "cache_version": self.cache_version,
                "price_per_mtok": (
                    {"input": price.input_per_mtok, "output": price.output_per_mtok}
                    if price
                    else None
                ),
            },
        )
        if self.on_call is not None:
            try:
                await self.on_call(record)
            except Exception:
                # Ledger failure must not turn a valid typed answer into a
                # different business decision; the worker flushes its outbox.
                pass
        self._circuit_complete(result.ok)
        if result.ok and self.cache_enabled:
            self.cache[key] = result
        return result

    def _circuit_admit(self) -> bool:
        now = time.monotonic()
        with _CIRCUIT_LOCK:
            state = _CIRCUITS.setdefault(
                self._circuit_key,
                {"failures": 0, "opened_at": 0.0, "probe_in_flight": False},
            )
            opened_at = float(state["opened_at"])
            if opened_at <= 0:
                return True
            if now - opened_at < self.cooldown_seconds:
                return False
            if bool(state["probe_in_flight"]):
                return False
            state["probe_in_flight"] = True
            return True

    def _circuit_complete(self, success: bool) -> None:
        now = time.monotonic()
        with _CIRCUIT_LOCK:
            state = _CIRCUITS.setdefault(
                self._circuit_key,
                {"failures": 0, "opened_at": 0.0, "probe_in_flight": False},
            )
            if success:
                state["failures"] = 0
                state["opened_at"] = 0.0
                state["probe_in_flight"] = False
                return
            failures = int(state["failures"]) + 1
            state["failures"] = failures
            state["probe_in_flight"] = False
            if failures >= self.failure_threshold:
                state["opened_at"] = now

    def _circuit_release(self) -> None:
        """Release a half-open probe without treating cancellation as provider failure."""

        with _CIRCUIT_LOCK:
            state = _CIRCUITS.get(self._circuit_key)
            if state is not None:
                state["probe_in_flight"] = False

    async def _request(self, *, state: Any, questions: dict[str, Any]) -> DecisionResult:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        body = {"model": self.model, "state": state, "questions": questions}
        timeout = httpx.Timeout(self.timeout_seconds)
        async with self._semaphore:
            async with httpx.AsyncClient(timeout=timeout, transport=self.transport) as client:
                response = await client.post(self.base_url, headers=headers, json=body)
        request_id = response.headers.get("x-typesafe-request-id")
        if response.status_code != 200:
            return DecisionResult(
                error=f"http_{response.status_code}",
                status_code=response.status_code,
                request_id=request_id,
            )
        try:
            payload = response.json()
        except ValueError:
            return DecisionResult(
                error="invalid_json",
                status_code=response.status_code,
                request_id=request_id,
            )
        answers, model, usage, error = validate_decision_response(payload, questions=questions)
        if error:
            return DecisionResult(
                error=error,
                model=model,
                usage=usage,
                status_code=response.status_code,
                request_id=request_id,
                raw_payload=payload,
            )
        return DecisionResult(
            answers=answers,
            model=model or self.model,
            usage=usage,
            status_code=response.status_code,
            request_id=request_id,
            raw_payload=payload,
        )
