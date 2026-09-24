from types import SimpleNamespace

from observability import tracing
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


def test_metadata_only_parentage_and_failed_status(monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider(id_generator=tracing.DomainIdGenerator())
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(tracing, "_provider", provider)
    base = {"trace_id": "trace", "span_id": "parent", "name": "write", "kind": "writer"}
    tracing.export_span(
        {**base, "prompt": "PRIVATE", "api_key": "SECRET"}, started_ns=10, ended_ns=20
    )
    tracing.export_span(
        {**base, "span_id": "child", "parent_span_id": "parent", "status": "failed"},
        started_ns=11,
        ended_ns=18,
    )
    spans = exporter.get_finished_spans()
    assert len(spans) == 2
    assert spans[1].context.trace_id == spans[0].context.trace_id
    assert spans[1].parent.span_id == spans[0].context.span_id
    assert spans[1].status.is_ok is False
    assert "PRIVATE" not in str(spans[0].attributes)
    assert "SECRET" not in str(spans[0].attributes)
    provider.shutdown()


def test_exporter_failure_cannot_fail_generation(monkeypatch):
    monkeypatch.setattr(tracing, "_provider", SimpleNamespace())
    tracing.export_span({"trace_id": "a", "span_id": "b"}, started_ns=1, ended_ns=2)
