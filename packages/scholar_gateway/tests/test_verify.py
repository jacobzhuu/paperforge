"""R1 入库核验测试（设计 §4.4.3）。

反例优先：编造的 DOI、编造的标题、作者/年份不吻合的近似标题都必须被拦下。
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from scholar_gateway import (
    InMemoryHttpCache,
    VerificationRequest,
    reset_provider_rate_limit_state,
    title_similarity,
    verify_reference,
)
from scholar_gateway.providers import ProviderConfig

REAL_TITLE = "Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks"


@pytest.fixture(autouse=True)
def _reset_runtime() -> None:
    reset_provider_rate_limit_state()
    yield
    reset_provider_rate_limit_state()


def _config() -> ProviderConfig:
    return ProviderConfig(
        user_agent="PaperForge/0.1 (mailto:test@example.com)",
        contact_email="test@example.com",
        timeout_seconds=1.0,
        rate_limit_delay=0.0,
        max_retries=0,
        min_request_interval_seconds=0.0,
    )


def _crossref_work(**overrides: Any) -> dict[str, Any]:
    item = {
        "DOI": "10.5555/rag",
        "title": [REAL_TITLE],
        "container-title": ["NeurIPS"],
        "type": "proceedings-article",
        "issued": {"date-parts": [[2020, 12, 6]]},
        "author": [{"given": "Patrick", "family": "Lewis"}],
        "URL": "https://doi.org/10.5555/rag",
    }
    item.update(overrides)
    return item


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_doi_lookup_verifies_against_crossref() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "api.crossref.org/works/10.5555/rag" in str(request.url):
            return httpx.Response(200, json={"message": _crossref_work()})
        return httpx.Response(404, json={})

    result = verify_reference(
        VerificationRequest(doi="https://doi.org/10.5555/RAG"),
        client=_client(handler),
        config=_config(),
        cache=InMemoryHttpCache(),
    )
    assert result.verified
    assert result.matched_by == "doi_crossref"
    # 元数据以 provider 响应为准重建，而不是沿用用户输入。
    assert result.candidate is not None
    assert result.candidate.title == REAL_TITLE
    assert result.candidate.doi == "10.5555/rag"


def test_fabricated_doi_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"status": "error"})

    result = verify_reference(
        VerificationRequest(doi="10.9999/does-not-exist"),
        client=_client(handler),
        config=_config(),
        cache=InMemoryHttpCache(),
    )
    assert not result.verified
    assert result.failure_reason == "verification_failed"
    assert result.candidate is None


def test_doi_falls_back_to_openalex_when_crossref_misses() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "api.crossref.org" in url:
            return httpx.Response(404, json={})
        if "api.openalex.org/works/doi:" in url:
            return httpx.Response(
                200,
                json={
                    "id": "https://openalex.org/W2963341956",
                    "display_name": REAL_TITLE,
                    "publication_year": 2020,
                    "doi": "https://doi.org/10.5555/rag",
                },
            )
        return httpx.Response(404, json={})

    result = verify_reference(
        VerificationRequest(doi="10.5555/rag"),
        client=_client(handler),
        config=_config(),
        cache=InMemoryHttpCache(),
    )
    assert result.verified
    assert result.matched_by == "doi_openalex"


def test_title_match_requires_threshold_and_agreeing_metadata() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "api.crossref.org/works" in str(request.url):
            return httpx.Response(200, json={"message": {"items": [_crossref_work()]}})
        return httpx.Response(200, json={"results": []})

    result = verify_reference(
        VerificationRequest(
            title="Retrieval Augmented Generation for Knowledge Intensive NLP Tasks",
            authors=("Lewis, Patrick",),
            publication_year=2020,
        ),
        client=_client(handler),
        config=_config(),
        cache=InMemoryHttpCache(),
    )
    assert result.verified
    assert result.matched_by == "title_crossref"
    assert (result.confidence or 0) >= 0.90


def test_title_match_rejected_when_year_disagrees() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "api.crossref.org/works" in str(request.url):
            return httpx.Response(200, json={"message": {"items": [_crossref_work()]}})
        return httpx.Response(200, json={"results": []})

    result = verify_reference(
        VerificationRequest(
            title=REAL_TITLE,
            authors=("Lewis, Patrick",),
            publication_year=2015,  # 与真实 2020 相差超过容差
        ),
        client=_client(handler),
        config=_config(),
        cache=InMemoryHttpCache(),
    )
    assert not result.verified
    assert result.failure_reason == "verification_failed"


def test_title_match_rejected_when_authors_disagree() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "api.crossref.org/works" in str(request.url):
            return httpx.Response(200, json={"message": {"items": [_crossref_work()]}})
        return httpx.Response(200, json={"results": []})

    result = verify_reference(
        VerificationRequest(
            title=REAL_TITLE,
            authors=("Fabricated, Author",),
            publication_year=2020,
        ),
        client=_client(handler),
        config=_config(),
        cache=InMemoryHttpCache(),
    )
    assert not result.verified


def test_hallucinated_title_never_matches() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "api.crossref.org/works" in str(request.url):
            return httpx.Response(200, json={"message": {"items": [_crossref_work()]}})
        return httpx.Response(200, json={"results": []})

    result = verify_reference(
        VerificationRequest(
            title="Quantum Neural Telepathy for Legal Contract Summarization",
            authors=("Nobody, A.",),
            publication_year=2024,
        ),
        client=_client(handler),
        config=_config(),
        cache=InMemoryHttpCache(),
    )
    assert not result.verified
    assert result.diagnostics.get("best_similarity", 1.0) < 0.90


def test_title_only_request_without_metadata_is_rejected() -> None:
    """只有标题、没有作者也没有年份时，无法确认是同一篇——不放行。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if "api.crossref.org/works" in str(request.url):
            return httpx.Response(200, json={"message": {"items": [_crossref_work()]}})
        return httpx.Response(200, json={"results": []})

    result = verify_reference(
        VerificationRequest(title=REAL_TITLE),
        client=_client(handler),
        config=_config(),
        cache=InMemoryHttpCache(),
    )
    assert not result.verified


def test_no_client_reports_unavailable_rather_than_passing() -> None:
    result = verify_reference(VerificationRequest(doi="10.5555/rag"), client=None)
    assert not result.verified
    assert result.failure_reason == "verification_unavailable"


def test_empty_request_is_rejected() -> None:
    result = verify_reference(
        VerificationRequest(),
        client=_client(lambda request: httpx.Response(404, json={})),
        config=_config(),
        cache=InMemoryHttpCache(),
    )
    assert not result.verified


def test_title_similarity_is_normalized() -> None:
    assert title_similarity("Deep Learning", "deep   learning!") == 1.0
    assert title_similarity(None, "x") == 0.0
    assert 0.0 < title_similarity("neural machine translation", "neural translation") < 1.0


def test_arxiv_datacite_doi_routes_to_arxiv_official_api() -> None:
    """10.48550/arXiv.* 不在 Crossref/OpenAlex 索引里，但 arXiv 官方 API 同样权威。"""
    atom = """<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <id>http://arxiv.org/abs/2005.11401v4</id>
        <published>2020-05-22T00:00:00Z</published>
        <title>Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks</title>
        <summary>We explore RAG.</summary>
        <author><name>Patrick Lewis</name></author>
      </entry>
    </feed>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "export.arxiv.org" in url:
            return httpx.Response(200, text=atom)
        return httpx.Response(404, json={})

    result = verify_reference(
        VerificationRequest(doi="10.48550/arXiv.2005.11401"),
        client=_client(handler),
        config=_config(),
        cache=InMemoryHttpCache(),
    )
    assert result.verified
    assert result.matched_by == "arxiv_id"
    assert result.candidate is not None
    assert result.candidate.arxiv_id == "2005.11401"


def test_non_arxiv_doi_does_not_reach_arxiv_lookup() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(404, json={})

    verify_reference(
        VerificationRequest(doi="10.9999/not-real"),
        client=_client(handler),
        config=_config(),
        cache=InMemoryHttpCache(),
    )
    assert not any("export.arxiv.org" in url for url in seen)
