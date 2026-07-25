"""五源适配器契约测试（M0 验收：五源检索测试全绿）。

覆盖：请求构造（查询净化 + 时间窗过滤）、响应映射、错误降级（draft-first）、
限流/熔断、缓存优先、凭据不进缓存键与诊断。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from scholar_gateway import (
    ALL_PROVIDERS,
    InMemoryHttpCache,
    ScholarlyDiscoveryQuery,
    build_adapter,
    reset_provider_rate_limit_state,
)
from scholar_gateway.providers import ProviderConfig


@pytest.fixture(autouse=True)
def _reset_runtime() -> None:
    reset_provider_rate_limit_state()
    yield
    reset_provider_rate_limit_state()


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _config(**kwargs: Any) -> ProviderConfig:
    defaults: dict[str, Any] = {
        "user_agent": "PaperForge/0.1 (mailto:test@example.com)",
        "rate_limit_delay": 0.0,
        "max_retries": 0,
        "timeout_seconds": 1.0,
    }
    defaults.update(kwargs)
    return ProviderConfig(**defaults)


CROSSREF_PAYLOAD = {
    "status": "ok",
    "message": {
        "total-results": 42,
        "items": [
            {
                "DOI": "10.1000/Test.123",
                "title": ["A Survey of Retrieval Augmented Generation"],
                "container-title": ["Journal of Testing"],
                "publisher": "ACME",
                "type": "journal-article",
                "language": "en",
                "is-referenced-by-count": 17,
                "issued": {"date-parts": [[2023, 5, 2]]},
                "author": [{"given": "Ada", "family": "Lovelace"}],
                "URL": "https://doi.org/10.1000/test.123",
                "link": [
                    {"URL": "https://example.org/paper.pdf", "content-type": "application/pdf"}
                ],
                "abstract": "<jats:p>We survey <b>RAG</b>.</jats:p>",
            }
        ],
    },
}

OPENALEX_PAYLOAD = {
    "meta": {"count": 7, "next_cursor": "cursor-2"},
    "results": [
        {
            "id": "https://openalex.org/W123456789",
            "doi": "https://doi.org/10.1000/openalex",
            "display_name": "Graph Neural Networks in Practice",
            "publication_year": 2022,
            "publication_date": "2022-03-04",
            "type": "article",
            "cited_by_count": 55,
            "is_retracted": False,
            "ids": {"pmid": "https://pubmed.ncbi.nlm.nih.gov/12345678"},
            "open_access": {"oa_status": "gold"},
            "primary_location": {
                "source": {"display_name": "Nature Machine Intelligence"},
                "landing_page_url": "https://example.org/landing",
                "pdf_url": "https://example.org/oa.pdf",
            },
            "best_oa_location": {"pdf_url": "https://example.org/best-oa.pdf", "license": "cc-by"},
            "authorships": [
                {
                    "author": {"display_name": "Grace Hopper"},
                    "institutions": [{"display_name": "Navy"}],
                }
            ],
            "abstract_inverted_index": {"Graph": [0], "networks": [1], "matter": [2]},
        }
    ],
}

S2_PAYLOAD = {
    "total": 3,
    "data": [
        {
            "paperId": "204e3073870fae3d05bcbc2f6a8e263d9b72e776",
            "corpusId": 99887766,
            "title": "Attention Is All You Need",
            "abstract": "We propose the Transformer.",
            "year": 2017,
            "publicationDate": "2017-06-12",
            "venue": "NeurIPS",
            "publicationTypes": ["JournalArticle"],
            "citationCount": 100000,
            "influentialCitationCount": 12000,
            "externalIds": {"DOI": "10.5555/transformer", "ArXiv": "1706.03762"},
            "url": "https://www.semanticscholar.org/paper/204e3073870fae3d05bcbc2f6a8e263d9b72e776",
            "openAccessPdf": {"url": "https://arxiv.org/pdf/1706.03762.pdf", "license": "cc-by"},
            "authors": [{"name": "Ashish Vaswani"}],
        }
    ],
}

EUROPE_PMC_PAYLOAD = {
    "hitCount": 12,
    "nextCursorMark": "next-cursor",
    "resultList": {
        "result": [
            {
                "title": "CRISPR screening in primary cells",
                "doi": "10.1000/epmc",
                "pmid": "31234567",
                "pmcid": "PMC7654321",
                "pubYear": "2021",
                "firstPublicationDate": "2021-08-01",
                "journalTitle": "Cell Reports",
                "abstractText": "We performed CRISPR screens.",
                "authorString": "Zhang Q, Li W",
                "isOpenAccess": "Y",
                "citedByCount": 31,
            }
        ]
    },
}

ARXIV_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">
  <opensearch:totalResults>231</opensearch:totalResults>
  <entry>
    <id>http://arxiv.org/abs/2401.01234v2</id>
    <published>2024-01-02T10:00:00Z</published>
    <title>Scaling Laws for Retrieval</title>
    <summary>We study scaling laws.</summary>
    <author><name>Jane Doe</name></author>
    <author><name>John Roe</name></author>
  </entry>
</feed>
"""


def test_crossref_maps_items_and_builds_bibliographic_query() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json=CROSSREF_PAYLOAD)

    adapter = build_adapter(
        "crossref",
        client=_client(handler),
        config=_config(contact_email="test@example.com"),
    )
    result = adapter.discover(
        ScholarlyDiscoveryQuery(
            query_text="retrieval augmented generation",
            limit=5,
            filters={"time_range": {"start_year": 2020, "end_year": 2024}},
        )
    )

    assert result.status == "completed"
    assert result.hit_count == 42
    assert "query.bibliographic=" in seen["url"]
    assert "from-pub-date%3A2020-01-01" in seen["url"]
    assert "mailto=test%40example.com" in seen["url"]

    candidate = result.candidates[0]
    assert candidate.doi == "10.1000/test.123"  # 规范化为小写
    assert candidate.publication_year == 2023
    assert candidate.venue_name == "Journal of Testing"
    assert candidate.citation_count == 17
    assert candidate.abstract == "We survey RAG ."  # JATS 标记被剥离为空白
    assert [author.author_name for author in candidate.authors] == ["Ada Lovelace"]
    pdf_links = [link for link in candidate.links if link.url_type == "pdf"]
    assert pdf_links and pdf_links[0].is_oa is True


def test_openalex_strips_field_prefixes_and_year_pipes_from_search() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json=OPENALEX_PAYLOAD)

    adapter = build_adapter("openalex", client=_client(handler), config=_config())
    result = adapter.discover(
        ScholarlyDiscoveryQuery(
            query_text="title_and_abstract:(graph neural) AND (2020|2021|2022)",
            limit=10,
        )
    )

    params = seen["params"]
    # 字段前缀与年份管道组都会被 OpenAlex 的 search= 拒绝，必须先净化。
    assert "title_and_abstract:" not in params["search"]
    assert "|" not in params["search"]
    assert params["cursor"] == "*"

    candidate = result.candidates[0]
    assert candidate.openalex_id == "W123456789"
    assert candidate.doi == "10.1000/openalex"
    assert candidate.pmid == "12345678"
    assert candidate.oa_status == "gold"
    assert candidate.license == "cc-by"
    assert candidate.citation_count == 55
    assert candidate.abstract == "Graph networks matter"  # 倒排索引还原
    assert candidate.authors[0].raw_affiliation == "Navy"
    assert result.metadata["next_cursor"] == "cursor-2"


def test_openalex_marks_retracted_works() -> None:
    payload = json.loads(json.dumps(OPENALEX_PAYLOAD))
    payload["results"][0]["is_retracted"] = True

    adapter = build_adapter(
        "openalex",
        client=_client(lambda request: httpx.Response(200, json=payload)),
        config=_config(),
    )
    result = adapter.discover(ScholarlyDiscoveryQuery(query_text="anything"))
    # R1：撤稿标记必须一路带到入库，写作白名单据此拒绝该文献。
    assert result.candidates[0].is_retracted is True


def test_semantic_scholar_sends_api_key_header_and_year_filter() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = dict(request.headers)
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json=S2_PAYLOAD)

    adapter = build_adapter(
        "semantic_scholar",
        client=_client(handler),
        config=_config(api_key="secret-key", min_request_interval_seconds=0.0),
    )
    result = adapter.discover(
        ScholarlyDiscoveryQuery(
            query_text="transformer",
            filters={"time_range": {"start_year": 2017, "end_year": 2019}},
        )
    )

    assert seen["headers"]["x-api-key"] == "secret-key"
    assert seen["params"]["year"] == "2017-2019"
    candidate = result.candidates[0]
    assert candidate.semantic_scholar_id == "204e3073870fae3d05bcbc2f6a8e263d9b72e776"
    assert candidate.corpus_id == "99887766"
    assert candidate.arxiv_id == "1706.03762"
    assert candidate.influential_citation_count == 12000


def test_arxiv_parses_atom_and_wraps_plain_query() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(
            200, text=ARXIV_ATOM, headers={"content-type": "application/atom+xml"}
        )

    adapter = build_adapter("arxiv", client=_client(handler), config=_config())
    result = adapter.discover(ScholarlyDiscoveryQuery(query_text="scaling laws", limit=3))

    assert seen["params"]["search_query"] == "all:scaling laws"
    assert result.hit_count == 231
    candidate = result.candidates[0]
    assert candidate.arxiv_id == "2401.01234"
    assert candidate.work_type == "preprint"
    assert candidate.oa_status == "green"
    assert [a.author_name for a in candidate.authors] == ["Jane Doe", "John Roe"]
    assert any(link.url.endswith(".pdf") and link.is_oa for link in candidate.links)


def test_arxiv_native_field_query_is_not_double_prefixed() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, text=ARXIV_ATOM)

    adapter = build_adapter("arxiv", client=_client(handler), config=_config())
    adapter.discover(ScholarlyDiscoveryQuery(query_text="cat:cs.CL AND ti:retrieval"))
    assert seen["params"]["search_query"] == "cat:cs.CL AND ti:retrieval"


def test_europe_pmc_maps_results_and_cursor() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json=EUROPE_PMC_PAYLOAD)

    adapter = build_adapter("europe_pmc", client=_client(handler), config=_config())
    result = adapter.discover(
        ScholarlyDiscoveryQuery(
            query_text="CRISPR",
            filters={"time_range": {"start_year": 2020, "end_year": 2022}},
        )
    )

    assert seen["params"]["cursorMark"] == "*"
    assert "FIRST_PDATE:[2020-01-01 TO 2022-12-31]" in seen["params"]["query"]
    candidate = result.candidates[0]
    assert candidate.pmcid == "PMC7654321"
    assert candidate.pmid == "31234567"
    assert candidate.oa_status == "green"
    assert [a.author_name for a in candidate.authors] == ["Zhang Q", "Li W"]
    assert result.metadata["next_cursor"] == "next-cursor"


def test_all_five_providers_are_registered_and_constructible() -> None:
    assert set(ALL_PROVIDERS) == {
        "openalex",
        "crossref",
        "semantic_scholar",
        "arxiv",
        "europe_pmc",
    }
    for provider in ALL_PROVIDERS:
        assert build_adapter(provider).provider_name == provider


def test_unknown_provider_raises() -> None:
    with pytest.raises(ValueError, match="unknown scholarly provider"):
        build_adapter("google_scholar")


def test_adapter_without_client_degrades_instead_of_raising() -> None:
    result = build_adapter("crossref").discover(ScholarlyDiscoveryQuery(query_text="x"))
    # Draft-first：未配置网络客户端时返回结构化错误，不抛异常。
    assert result.status == "failed"
    assert result.errors[0].error_code == "adapter_not_configured"


def test_http_error_is_reported_as_degraded_result() -> None:
    adapter = build_adapter(
        "crossref",
        client=_client(lambda request: httpx.Response(503, text="upstream down")),
        config=_config(),
    )
    result = adapter.discover(ScholarlyDiscoveryQuery(query_text="x"))
    assert result.status == "failed"
    assert result.errors[0].error_code == "http_error"
    assert result.errors[0].retryable is True


def test_invalid_json_is_reported_as_degraded_result() -> None:
    adapter = build_adapter(
        "crossref",
        client=_client(lambda request: httpx.Response(200, text="<html>not json</html>")),
        config=_config(),
    )
    result = adapter.discover(ScholarlyDiscoveryQuery(query_text="x"))
    assert result.errors[0].error_code == "invalid_json"


def test_openalex_daily_quota_429_fails_fast_and_opens_circuit() -> None:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(
            429,
            json={"message": "Insufficient budget", "retryAfter": 120},
            headers={"X-RateLimit-Remaining": "0"},
        )

    client = _client(handler)
    config = _config(max_retries=3)
    first = build_adapter("openalex", client=client, config=config).discover(
        ScholarlyDiscoveryQuery(query_text="x")
    )
    assert first.errors[0].metadata["daily_quota_exhausted"] is True
    # 预算耗尽时不再盲目重试：只发一次请求。
    assert calls["count"] == 1

    second = build_adapter("openalex", client=client, config=config).discover(
        ScholarlyDiscoveryQuery(query_text="y")
    )
    # 熔断打开后连一个请求都不发（礼貌性，绝非绕过）。
    assert second.errors[0].error_code == "circuit_open"
    assert calls["count"] == 1


def test_cache_hit_avoids_second_network_call_and_excludes_credentials() -> None:
    calls = {"count": 0}
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        seen_urls.append(str(request.url))
        return httpx.Response(200, json=OPENALEX_PAYLOAD)

    cache = InMemoryHttpCache()
    client = _client(handler)
    config = _config(api_key="openalex-secret")
    query = ScholarlyDiscoveryQuery(query_text="graph neural networks", limit=5)

    first = build_adapter("openalex", client=client, config=config, cache=cache).discover(query)
    second = build_adapter("openalex", client=client, config=config, cache=cache).discover(query)

    assert calls["count"] == 1
    assert first.candidates[0].openalex_id == second.candidates[0].openalex_id
    assert second.metadata["cache_hit"] is True
    # 凭据只在发请求时合并，不进缓存键。
    assert "api_key=openalex-secret" in seen_urls[0]
    cached = cache.get(
        url="https://api.openalex.org/works",
        params={"search": "graph neural networks", "per-page": 5, "cursor": "*"},
        ttl_hours=72.0,
    )
    assert cached is not None
    assert "api_key" not in cached.url
