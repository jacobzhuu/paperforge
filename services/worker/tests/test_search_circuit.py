from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from paperforge_worker.pipelines import search
from scholar_gateway.models import (
    ScholarlyDiscoveryError,
    ScholarlyDiscoveryResult,
)


async def test_arxiv_run_circuit_records_skips_and_keeps_other_providers(monkeypatch) -> None:
    calls: dict[str, list[str]] = {"arxiv": [], "openalex": []}

    class Adapter:
        def __init__(self, name: str) -> None:
            self.name = name

        def discover(self, query):
            calls[self.name].append(query.query_text)
            errors = (
                (
                    ScholarlyDiscoveryError(
                        provider_name=self.name,
                        error_code="arxiv_http_error",
                        message="HTTP 503",
                        status_code=503,
                        retryable=True,
                    ),
                )
                if self.name == "arxiv"
                else ()
            )
            return ScholarlyDiscoveryResult(
                provider_name=self.name,
                query=query,
                retrieved_at=datetime.now(UTC),
                errors=errors,
            )

    monkeypatch.setattr(search, "build_adapter", lambda name, **kwargs: Adapter(name))
    context = SimpleNamespace(
        http_client=None,
        scholar_cache=None,
        settings=SimpleNamespace(provider_config=lambda name: None),
    )
    results = await search._discover_all(
        context,
        provider_names=["arxiv", "openalex"],
        queries=["a", "b", "c", "d"],
        filters={},
        limit=10,
    )

    assert calls == {"arxiv": ["a", "b"], "openalex": ["a", "b", "c", "d"]}
    arxiv = [result for provider, _, result in results if provider == "arxiv"]
    assert [result.errors[0].error_code for result in arxiv] == [
        "arxiv_http_error",
        "arxiv_http_error",
        "provider_run_circuit_open",
        "provider_run_circuit_open",
    ]
    assert all(result.metadata["skipped"] for result in arxiv[2:])
    assert all("elapsed_ms" in result.metadata for _, _, result in results)


async def test_arxiv_success_resets_failure_streak(monkeypatch) -> None:
    calls: list[str] = []

    class Adapter:
        def discover(self, query):
            calls.append(query.query_text)
            failed = query.query_text in {"a", "c"}
            return ScholarlyDiscoveryResult(
                provider_name="arxiv",
                query=query,
                retrieved_at=datetime.now(UTC),
                errors=(ScholarlyDiscoveryError("arxiv", "timeout", "timeout", retryable=True),)
                if failed
                else (),
            )

    monkeypatch.setattr(search, "build_adapter", lambda name, **kwargs: Adapter())
    context = SimpleNamespace(
        http_client=None,
        scholar_cache=None,
        settings=SimpleNamespace(provider_config=lambda name: None),
    )
    await search._discover_all(
        context,
        provider_names=["arxiv"],
        queries=["a", "b", "c", "d"],
        filters={},
        limit=10,
    )
    assert calls == ["a", "b", "c", "d"]
