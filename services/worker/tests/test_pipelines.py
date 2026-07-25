"""M1 管线单测：SCOPE / 排序 / 卡片。

重点：LLM 不可用时的确定性回退必须永远产出合法结果（draft-first），
且 LLM 输出被严格净化——不允许把 LLM 幻觉带进排序或卡片。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest
from llm_runtime import LLMConfig, LLMRunner
from llm_runtime.types import LLMError, LLMResponse
from paperforge_worker.pipelines.cards import (
    card_source_hash,
    deterministic_card,
    extract_card,
    normalize_card,
)
from paperforge_worker.pipelines.ranking import rank_candidates, rerank_with_llm
from paperforge_worker.pipelines.scope import (
    deterministic_scope,
    generate_scope,
    normalize_scope,
    scope_filters,
    search_queries,
    topic_terms,
)

FIXED_NOW = datetime(2026, 7, 25, tzinfo=UTC)


class _StubProvider:
    """按顺序吐出预设响应的 provider 替身。"""

    def __init__(self, payloads: list[Any]) -> None:
        self.payloads = list(payloads)
        self.requests: list[Any] = []

    def generate(self, request):
        self.requests.append(request)
        payload = self.payloads.pop(0) if self.payloads else ""
        if isinstance(payload, Exception):
            raise payload
        text = payload if isinstance(payload, str) else json.dumps(payload)
        return LLMResponse(text=text, model="stub-model", provider="stub")


def _runner(payloads: list[Any]) -> tuple[LLMRunner, _StubProvider, list]:
    provider = _StubProvider(payloads)
    calls: list = []
    runner = LLMRunner(
        LLMConfig(provider="openai", base_url="http://stub", api_key="k"),
        provider=provider,
        on_call=calls.append,
    )
    return runner, provider, calls


@dataclass(frozen=True)
class _Candidate:
    title: str
    abstract: str | None = None
    publication_year: int | None = 2024
    citation_count: int | None = None
    influential_citation_count: int | None = None
    normalized_title_hash: str = "hash"
    provider_name: str = "openalex"
    provider_record_id: str | None = "W1"
    raw_provider_metadata: dict[str, Any] = field(default_factory=dict)


# ---- SCOPE ----


def test_deterministic_scope_is_valid_without_llm() -> None:
    scope = deterministic_scope("retrieval augmented generation", language="en", now=FIXED_NOW)
    assert scope["generator"] == "deterministic"
    assert scope["keyword_groups"]
    assert scope["time_range"] == {"start_year": 2021, "end_year": 2026}
    assert scope["research_question"]


def test_topic_terms_drops_stopwords_and_keeps_order() -> None:
    assert topic_terms("A Survey of Retrieval Augmented Generation for the Sciences") == [
        "Retrieval",
        "Augmented",
        "Generation",
        "Sciences",
    ]


async def test_generate_scope_uses_llm_output_when_valid() -> None:
    runner, _provider, calls = _runner(
        [
            {
                "research_question": "How do RAG systems ground scientific claims?",
                "scope_summary": "Covers retrieval-augmented generation for science.",
                "keyword_groups": [
                    {"name": "retrieval", "keywords": ["dense retrieval", "BM25"]},
                    {"name": "generation", "keywords": ["LLM", "language model"]},
                ],
                "subtopics": ["evaluation", "hallucination"],
                "time_range": {"start_year": 2019, "end_year": 2025},
            }
        ]
    )
    scope = await generate_scope("RAG for science", runner=runner, now=FIXED_NOW)
    assert scope["generator"].startswith("llm:")
    assert scope["time_range"] == {"start_year": 2019, "end_year": 2025}
    assert [g["name"] for g in scope["keyword_groups"]] == ["retrieval", "generation"]
    assert len(calls) == 1


async def test_generate_scope_falls_back_when_llm_fails() -> None:
    runner, _provider, _calls = _runner(
        [LLMError(provider="stub", error_code="server_error", message="boom")]
    )
    scope = await generate_scope("RAG for science", runner=runner, now=FIXED_NOW)
    assert scope["generator"] == "deterministic_fallback"
    assert scope["keyword_groups"]


async def test_generate_scope_falls_back_on_unparsable_output() -> None:
    runner, _provider, _calls = _runner(["not json at all"])
    scope = await generate_scope("RAG for science", runner=runner, now=FIXED_NOW)
    assert scope["generator"] == "deterministic_fallback"


def test_normalize_scope_repairs_partial_llm_output() -> None:
    scope = normalize_scope(
        {"keyword_groups": ["single string group"], "time_range": {"start_year": 2030}},
        topic="RAG",
        now=FIXED_NOW,
    )
    assert scope["keyword_groups"] == [
        {"name": "single string group", "keywords": ["single string group"]}
    ]
    # end_year 缺失时用确定性回退补齐，且顺序被修正。
    assert scope["time_range"]["start_year"] <= scope["time_range"]["end_year"]


def test_search_queries_and_filters_from_scope() -> None:
    scope = deterministic_scope("graph neural networks", language="en", now=FIXED_NOW)
    queries = search_queries(scope)
    assert queries[0] == "graph neural networks"
    assert all(query.strip() for query in queries)
    # 确定性回退里 subtopics 就是主题切词，不应再单独成查询。
    assert "graph" not in queries
    assert scope_filters(scope) == {"time_range": {"start_year": 2021, "end_year": 2026}}


# ---- 排序 ----


def _scope() -> dict[str, Any]:
    return {
        "topic": "retrieval augmented generation",
        "research_question": "How does retrieval augmented generation reduce hallucination?",
        "keyword_groups": [
            {"name": "retrieval", "keywords": ["retrieval", "retriever"]},
            {"name": "generation", "keywords": ["generation", "language model"]},
            {"name": "evaluation", "keywords": ["hallucination", "faithfulness"]},
        ],
        "time_range": {"start_year": 2020, "end_year": 2026},
    }


def test_ranking_prefers_on_topic_recent_work() -> None:
    on_topic = _Candidate(
        title="Retrieval augmented generation reduces hallucination",
        abstract="We study retrieval and generation with a retriever and language model.",
        publication_year=2025,
        citation_count=100,
        normalized_title_hash="a",
    )
    off_topic = _Candidate(
        title="A study of medieval pottery glazing",
        abstract="Nothing to do with the query.",
        publication_year=2025,
        normalized_title_hash="b",
    )
    ranked = rank_candidates([off_topic, on_topic], scope=_scope(), now=FIXED_NOW)
    assert ranked[0].candidate is on_topic
    assert ranked[0].score > ranked[1].score
    assert ranked[0].reason["method"] == "deterministic_v1"
    assert ranked[0].reason["facet_coverage"] > 0


def test_ranking_never_drops_candidates() -> None:
    candidates = [
        _Candidate(title=f"Paper {index}", normalized_title_hash=str(index)) for index in range(5)
    ]
    ranked = rank_candidates(candidates, scope=_scope(), now=FIXED_NOW)
    # 分数只用于排序与推荐，不做硬性纳入/排除（设计 §3.2）。
    assert len(ranked) == 5


async def test_llm_rerank_blends_scores_and_keeps_unmentioned_candidates() -> None:
    candidates = [
        _Candidate(title="First", normalized_title_hash="1"),
        _Candidate(title="Second", normalized_title_hash="2"),
    ]
    ranked = rank_candidates(candidates, scope=_scope(), now=FIXED_NOW)
    runner, _provider, _calls = _runner(
        [{"ranking": [{"index": 1, "score": 0.95, "reason": "closer to the question"}]}]
    )
    reranked = await rerank_with_llm(ranked, scope=_scope(), runner=runner, top_n=10)

    assert len(reranked) == 2
    assert reranked[0].candidate.title == ranked[1].candidate.title
    assert reranked[0].reason["method"] == "deterministic_v1+llm_rerank"
    assert {item.candidate.title for item in reranked} == {"First", "Second"}


async def test_llm_rerank_ignores_out_of_range_indexes() -> None:
    """LLM 不得凭空引入候选：越界下标一律丢弃。"""
    ranked = rank_candidates(
        [_Candidate(title="Only", normalized_title_hash="1")], scope=_scope(), now=FIXED_NOW
    )
    runner, _provider, _calls = _runner([{"ranking": [{"index": 99, "score": 1.0}]}])
    reranked = await rerank_with_llm(ranked, scope=_scope(), runner=runner)
    assert len(reranked) == 1
    assert reranked[0].candidate.title == "Only"


async def test_llm_rerank_falls_back_on_bad_output() -> None:
    ranked = rank_candidates(
        [_Candidate(title="Only", normalized_title_hash="1")], scope=_scope(), now=FIXED_NOW
    )
    runner, _provider, _calls = _runner(["<html>not json</html>"])
    reranked = await rerank_with_llm(ranked, scope=_scope(), runner=runner)
    assert reranked == ranked


async def test_rerank_without_runner_is_identity() -> None:
    ranked = rank_candidates(
        [_Candidate(title="Only", normalized_title_hash="1")], scope=_scope(), now=FIXED_NOW
    )
    assert await rerank_with_llm(ranked, scope=_scope(), runner=None) == ranked


# ---- 卡片 ----


async def test_card_extraction_uses_llm_output() -> None:
    runner, _provider, _calls = _runner(
        [
            {
                "summary": "The paper proposes RAG.",
                "contributions": ["A retrieval-augmented architecture"],
                "methods": ["dense retrieval"],
                "results": ["improves accuracy on NQ"],
                "limitations": [],
                "quotable_points": ["RAG grounds generation in retrieved passages"],
            }
        ]
    )
    card, used_fallback = await extract_card(
        title="RAG",
        abstract="We propose retrieval augmented generation.",
        runner=runner,
    )
    assert used_fallback is False
    assert card["summary"] == "The paper proposes RAG."
    assert card["extraction_model"] == "stub-model"


async def test_card_extraction_falls_back_without_llm() -> None:
    card, used_fallback = await extract_card(
        title="RAG",
        abstract=(
            "We propose retrieval augmented generation for knowledge intensive tasks. "
            "It retrieves passages and conditions generation on them. "
            "Experiments cover open-domain question answering."
        ),
        runner=None,
    )
    assert used_fallback is True
    assert card["extraction_model"] == "deterministic_abstract_v1"
    # 确定性回退只做切句：不产生摘要之外的断言。
    assert card["contributions"] == []
    assert card["results"] == []
    assert card["quotable_points"]


def test_deterministic_card_without_abstract_uses_title_only() -> None:
    card = deterministic_card(title="A paper with no abstract", abstract=None)
    assert card["summary"] == "A paper with no abstract"
    assert card["quotable_points"] == []


def test_normalize_card_clips_and_cleans_lists() -> None:
    card = normalize_card(
        {
            "summary": "  spaced   out  ",
            "contributions": ["a"] * 20,
            "methods": "not a list",
            "results": [None, "", "kept"],
        }
    )
    assert card["summary"] == "spaced out"
    assert len(card["contributions"]) == 6
    assert card["methods"] == []
    assert card["results"] == ["kept"]


def test_card_source_hash_is_stable_and_input_sensitive() -> None:
    first = card_source_hash(title="T", abstract="A")
    assert first == card_source_hash(title="T", abstract="A")
    assert first != card_source_hash(title="T", abstract="B")


@pytest.mark.parametrize("language", ["zh", "en"])
async def test_card_prompt_language_switches(language: str) -> None:
    runner, provider, _calls = _runner([{"summary": "s"}])
    await extract_card(title="T", abstract="A", runner=runner, language=language)
    system_prompt = provider.requests[0].system_prompt
    assert ("只输出 JSON" in system_prompt) is (language == "zh")
