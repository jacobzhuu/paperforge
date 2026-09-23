"""Best-effort, metadata-only OTLP export. The SQL ledger remains authoritative."""

from __future__ import annotations

import hashlib
import logging
import os
import threading
from collections.abc import Mapping
from contextvars import ContextVar
from datetime import datetime
from typing import Any

from opentelemetry.sdk.trace.id_generator import RandomIdGenerator

logger = logging.getLogger(__name__)
_lock = threading.Lock()
_provider: Any = None
_forced_ids: ContextVar[tuple[int, int] | None] = ContextVar("otel_domain_ids", default=None)


class DomainIdGenerator(RandomIdGenerator):
    def generate_trace_id(self):
        ids = _forced_ids.get()
        return ids[0] if ids else super().generate_trace_id()

    def generate_span_id(self):
        ids = _forced_ids.get()
        return ids[1] if ids else super().generate_span_id()


# Never forward arbitrary caller metadata, exception messages, prompts or document text.
_ALLOWED = frozenset(
    {
        "project_id",
        "job_id",
        "trace_id",
        "span_id",
        "parent_span_id",
        "kind",
        "name",
        "node_id",
        "attempt",
        "role",
        "model",
        "provider",
        "status",
        "error_type",
        "error_code",
        "prompt_sha256",
        "input_tokens",
        "output_tokens",
        "cost_estimate",
        "latency_ms",
        "local_queue_wait_ms",
        "provider_slot_wait_ms",
        "rate_limit_wait_ms",
        "elapsed_ms",
        "review_version",
        "engine",
        "version",
    }
)


def safe_attributes(fields: Mapping[str, Any]) -> dict[str, str | int | float | bool]:
    return {
        f"paperforge.{key}": value[:256] if isinstance(value, str) else value
        for key, value in fields.items()
        if key in _ALLOWED and isinstance(value, (str, int, float, bool))
    }


def configure_tracing(service: str) -> Any:
    """Explicit opt-in; SDK batching keeps collector I/O outside generation threads."""
    global _provider
    if os.getenv("PAPERFORGE_OTEL_ENABLED", "false").lower() != "true":
        return None
    with _lock:
        if _provider is not None:
            return _provider
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            provider = TracerProvider(
                resource=Resource.create({"service.name": service}),
                id_generator=DomainIdGenerator(),
            )
            provider.add_span_processor(
                BatchSpanProcessor(
                    OTLPSpanExporter(timeout=3),
                    max_queue_size=2048,
                    max_export_batch_size=128,
                )
            )
            _provider = provider
        except Exception:
            logger.warning("OTLP initialization unavailable")
    return _provider


def _id(value: str, size: int) -> int:
    return int.from_bytes(hashlib.sha256(value.encode()).digest()[:size], "big") or 1


def export_span(fields: Mapping[str, Any], *, started_ns: int, ended_ns: int) -> None:
    """Export persisted domain spans using stable IDs, including across resumed jobs."""
    if _provider is None:
        return
    try:
        from opentelemetry.context import Context
        from opentelemetry.trace import (
            NonRecordingSpan,
            SpanContext,
            Status,
            StatusCode,
            TraceFlags,
            set_span_in_context,
        )

        trace_id = _id(str(fields["trace_id"]), 16)
        span_id = _id(str(fields["span_id"]), 8)
        parent = fields.get("parent_span_id")
        parent_context = (
            SpanContext(trace_id, _id(str(parent), 8), False, TraceFlags(1)) if parent else None
        )
        attributes = safe_attributes(fields)
        attributes["langfuse.observation.type"] = (
            "generation" if fields.get("kind") == "llm" else "span"
        )
        if fields.get("model"):
            attributes["gen_ai.request.model"] = str(fields["model"])
        for source, target in (
            ("input_tokens", "gen_ai.usage.input_tokens"),
            ("output_tokens", "gen_ai.usage.output_tokens"),
        ):
            if fields.get(source) is not None:
                attributes[target] = int(fields[source])
        context = (
            set_span_in_context(NonRecordingSpan(parent_context), Context())
            if parent_context
            else Context()
        )
        token = _forced_ids.set((trace_id, span_id))
        try:
            span = _provider.get_tracer("paperforge.domain").start_span(
                str(fields.get("name") or fields.get("role") or "agent"),
                context=context,
                attributes=attributes,
                start_time=started_ns,
            )
        finally:
            _forced_ids.reset(token)
        if fields.get("error_code") or fields.get("status") in {"failed", "interrupted"}:
            span.set_status(Status(StatusCode.ERROR))
        span.end(end_time=max(ended_ns, started_ns))
    except Exception:
        logger.warning("OTLP span dropped")


def export_call(record: Any, *, project_id: str, job_id: str | None) -> None:
    fields = {**record.metadata, "kind": "llm", "project_id": project_id, "job_id": job_id}
    for key in (
        "role",
        "model",
        "provider",
        "input_tokens",
        "output_tokens",
        "cost_estimate",
        "latency_ms",
        "error_code",
        "prompt_sha256",
    ):
        fields[key] = getattr(record, key, None)
    occurred: datetime = record.occurred_at
    end = int(occurred.timestamp() * 1_000_000_000)
    export_span(fields, started_ns=end - int(record.latency_ms or 0) * 1_000_000, ended_ns=end)


def shutdown_tracing() -> None:
    global _provider
    if _provider is not None:
        try:
            _provider.force_flush(timeout_millis=3000)
            _provider.shutdown()
        except Exception:
            logger.warning("OTLP shutdown incomplete")
        finally:
            _provider = None
