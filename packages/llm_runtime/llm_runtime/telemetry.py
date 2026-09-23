import time
from contextlib import contextmanager
from contextvars import ContextVar

_metrics: ContextVar[dict | None] = ContextVar("llm_attempt_metrics", default=None)


@contextmanager
def request_metrics(metadata):
    token = _metrics.set(metadata)
    try:
        yield
    finally:
        _metrics.reset(token)


def add_timing(name: str, milliseconds: int) -> None:
    metrics = _metrics.get()
    if metrics is not None:
        metrics[name] = metrics.get(name, 0) + milliseconds


def request_metadata() -> dict:
    """Current thread's request metadata, populated by trusted runtime admission."""
    return _metrics.get() or {}


def admission_event(event: str, **fields) -> None:
    metadata = _metrics.get()
    if metadata is not None:
        metadata.setdefault("provider_admission_events", []).append(
            {"event": event, "timestamp_ms": time.time_ns() // 1_000_000, **fields}
        )


_cancel: ContextVar[object | None] = ContextVar("llm_admission_cancel", default=None)


@contextmanager
def admission_cancellation(event):
    token = _cancel.set(event)
    try:
        yield
    finally:
        _cancel.reset(token)


def check_admission_cancelled():
    event = _cancel.get()
    if event is not None and event.is_set():
        from concurrent.futures import CancelledError

        raise CancelledError("LLM admission cancelled before HTTP request")
