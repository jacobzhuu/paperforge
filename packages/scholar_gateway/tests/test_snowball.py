"""引文雪球扩展测试（M0 验收：雪球测试全绿）。"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from scholar_gateway import (
    InMemoryHttpCache,
    SnowballSeed,
    expand_citation_snowball,
    rank_by_citation_influence,
    reset_provider_rate_limit_state,
    select_core_works_by_influence,
)


@pytest.fixture(autouse=True)
def _reset_runtime() -> None:
    reset_provider_rate_limit_state()
    yield
    reset_provider_rate_limit_state()


def _openalex_work(work_id: str, title: str) -> dict[str, Any]:
    return {
        "id": f"https://openalex.org/{work_id}",
        "display_name": title,
        "publication_year": 2023,
        "cited_by_count": 10,
    }


def _s2_paper(paper_id: str, title: str) -> dict[str, Any]:
    return {
        "paperId": paper_id,
        "title": title,
        "year": 2022,
        "externalIds": {"DOI": f"10.1000/{title.replace(' ', '-').lower()}"},
    }


def _handler(routes: dict[str, dict[str, Any]], calls: list[str]):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append(f"{path}?{request.url.params}")
        for key, payload in routes.items():
            if key in path:
                return httpx.Response(200, json=payload)
        return httpx.Response(404, json={"error": "not found"})

    return handler


def test_forward_and_backward_expansion_collects_unique_candidates() -> None:
    calls: list[str] = []
    routes = {
        "/works/W1": {"referenced_works": ["https://openalex.org/W900", "W901"]},
        "/works": {"results": [_openalex_work("W555", "Cited By Paper")]},
        "/citations": {"data": [{"citingPaper": _s2_paper("a" * 40, "S2 Citing")}]},
        "/references": {"data": [{"citedPaper": _s2_paper("b" * 40, "S2 Cited")}]},
    }
    client = httpx.Client(transport=httpx.MockTransport(_handler(routes, calls)))

    result = expand_citation_snowball(
        seeds=[SnowballSeed(work_id="w-1", openalex_id="W1", semantic_scholar_id="c" * 40)],
        client=client,
        cache=InMemoryHttpCache(),
        semantic_scholar_min_interval_seconds=0.0,
        max_neighbors_per_seed=10,
        direction="both",
    )

    assert result.seed_work_ids == ("w-1",)
    titles = {candidate.title for candidate in result.discovered_candidates}
    assert "S2 Citing" in titles
    assert "S2 Cited" in titles
    assert result.diagnostics["direction"] == "both"
    # 双向时每个方向取一半邻居预算。
    assert result.diagnostics["per_direction_limit"] == 5
    assert result.diagnostics["forward_candidate_count"] >= 1
    assert result.diagnostics["backward_candidate_count"] >= 1


def test_direction_forward_skips_backward_calls() -> None:
    calls: list[str] = []
    routes = {
        "/works": {"results": [_openalex_work("W555", "Cited By Paper")]},
        "/citations": {"data": [{"citingPaper": _s2_paper("a" * 40, "S2 Citing")}]},
    }
    client = httpx.Client(transport=httpx.MockTransport(_handler(routes, calls)))

    expand_citation_snowball(
        seeds=[SnowballSeed(work_id="w-1", openalex_id="W1", semantic_scholar_id="c" * 40)],
        client=client,
        cache=InMemoryHttpCache(),
        semantic_scholar_min_interval_seconds=0.0,
        direction="forward",
    )

    assert not any("/references" in call for call in calls)


def test_provider_failure_is_degraded_not_raised() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = expand_citation_snowball(
        seeds=[SnowballSeed(work_id="w-1", openalex_id="W1")],
        client=client,
        cache=InMemoryHttpCache(),
        semantic_scholar_min_interval_seconds=0.0,
    )
    # Draft-first：provider 挂了也只记诊断，返回空候选。
    assert result.discovered_candidates == ()
    assert result.diagnostics["errors"]
    assert all("error" in call for call in result.diagnostics["errors"])


def test_no_client_or_no_seeds_is_skipped() -> None:
    assert expand_citation_snowball(seeds=[], client=None).diagnostics["status"] == "skipped"


def test_unsupported_direction_raises() -> None:
    with pytest.raises(ValueError, match="unsupported snowball direction"):
        expand_citation_snowball(seeds=[], client=None, direction="sideways")  # type: ignore[arg-type]


def test_seed_budget_is_bounded_by_max_seeds() -> None:
    calls: list[str] = []
    routes = {"/works": {"results": []}}
    client = httpx.Client(transport=httpx.MockTransport(_handler(routes, calls)))
    seeds = [SnowballSeed(work_id=f"w-{i}", openalex_id=f"W{i}") for i in range(10)]

    result = expand_citation_snowball(
        seeds=seeds,
        client=client,
        cache=InMemoryHttpCache(),
        semantic_scholar_min_interval_seconds=0.0,
        max_seeds=3,
        direction="forward",
    )
    assert len(result.seed_work_ids) == 3


def test_citation_influence_ranking_prefers_influential_then_total() -> None:
    seeds = [
        SnowballSeed(work_id="low", citation_count=5),
        SnowballSeed(work_id="influential", citation_count=10, influential_citation_count=9),
        SnowballSeed(work_id="high-total", citation_count=90),
    ]
    ranked = [seed.work_id for seed in rank_by_citation_influence(seeds)]
    assert ranked == ["influential", "high-total", "low"]
    assert [s.work_id for s in select_core_works_by_influence(seeds, top_k=2)] == [
        "influential",
        "high-total",
    ]


def test_ranking_without_citation_signals_preserves_input_order() -> None:
    seeds = [SnowballSeed(work_id="a"), SnowballSeed(work_id="b")]
    assert [s.work_id for s in rank_by_citation_influence(seeds)] == ["a", "b"]
