"""SEARCH 排序：确定性打分 + LLM top-N 重排（取代多阶段筛选）。

改造自 DeepSearch literature_review/topic_relevance.py（624 行）：
保留实体锚定的主题相关性评分与 facet 覆盖；分数只用于排序与推荐，
**不做硬性纳入/排除**（设计 §3.2）。用户圈选或全自动 top-K 才决定入库。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from llm_runtime import LLMRunner
from scholar_gateway import ScholarlyWorkCandidate

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9\-]+|[一-鿿]")

# 打分权重：主题匹配为主，时效与影响力为辅（只影响排序，不影响准入）。
WEIGHT_TITLE = 0.40
WEIGHT_ABSTRACT = 0.22
WEIGHT_FACET_COVERAGE = 0.18
WEIGHT_RECENCY = 0.12
WEIGHT_CITATION = 0.08

_SYSTEM_PROMPT = """You rank scholarly search results for a literature review.
Given a research question and numbered candidates, return JSON only:
{"ranking": [{"index": 3, "score": 0.92, "reason": "short reason"}]}
Rules: score in [0,1]; include every index you judge relevant, most relevant first;
judge only from the given title/abstract — never invent facts about a paper;
a paper you cannot judge should get a low score, not an invented justification."""


@dataclass(frozen=True)
class RankedCandidate:
    candidate: ScholarlyWorkCandidate
    score: float
    reason: dict[str, Any] = field(default_factory=dict)


def rank_candidates(
    candidates: list[ScholarlyWorkCandidate],
    *,
    scope: dict[str, Any],
    now: datetime | None = None,
) -> list[RankedCandidate]:
    """确定性排序：主题词覆盖 + facet 覆盖 + 时效 + 引用影响力。"""
    clock = now or datetime.now(UTC)
    topic_tokens = _scope_tokens(scope)
    facets = _scope_facets(scope)
    time_range = scope.get("time_range") if isinstance(scope.get("time_range"), dict) else {}
    start_year = time_range.get("start_year")
    end_year = time_range.get("end_year") or clock.year

    ranked: list[RankedCandidate] = []
    for candidate in candidates:
        title_tokens = _tokens(candidate.title)
        abstract_tokens = _tokens(candidate.abstract or "")
        title_score = _overlap(topic_tokens, title_tokens)
        abstract_score = _overlap(topic_tokens, abstract_tokens)
        facet_score = _facet_coverage(facets, title_tokens | abstract_tokens)
        recency = _recency_score(candidate.publication_year, start_year, end_year, clock.year)
        citation = _citation_score(candidate)
        score = (
            WEIGHT_TITLE * title_score
            + WEIGHT_ABSTRACT * abstract_score
            + WEIGHT_FACET_COVERAGE * facet_score
            + WEIGHT_RECENCY * recency
            + WEIGHT_CITATION * citation
        )
        ranked.append(
            RankedCandidate(
                candidate=candidate,
                score=round(min(1.0, max(0.0, score)), 4),
                reason={
                    "method": "deterministic_v1",
                    "title_match": round(title_score, 4),
                    "abstract_match": round(abstract_score, 4),
                    "facet_coverage": round(facet_score, 4),
                    "recency": round(recency, 4),
                    "citation_influence": round(citation, 4),
                    "has_abstract": bool(candidate.abstract),
                },
            )
        )
    ranked.sort(key=_sort_key, reverse=True)
    return ranked


async def rerank_with_llm(
    ranked: list[RankedCandidate],
    *,
    scope: dict[str, Any],
    runner: LLMRunner | None,
    top_n: int = 40,
) -> list[RankedCandidate]:
    """对确定性 top-N 做 LLM 重排（reranker 角色，便宜档）。

    Draft-first：LLM 不可用或输出不合法时原样返回确定性排序。
    LLM 只调整顺序与理由，**不能**引入新文献，也不改变任何元数据。
    """
    if runner is None or not runner.enabled or not ranked:
        return ranked
    head = ranked[: max(1, top_n)]
    tail = ranked[len(head) :]
    question = scope.get("research_question") or scope.get("topic") or ""
    lines = []
    for index, item in enumerate(head):
        abstract = (item.candidate.abstract or "").replace("\n", " ")[:400]
        year = item.candidate.publication_year or "n.d."
        lines.append(f"[{index}] ({year}) {item.candidate.title}\n{abstract}")
    result = await runner.agenerate_json(
        "reranker",
        system_prompt=_SYSTEM_PROMPT,
        user_prompt=f"Research question: {question}\n\nCandidates:\n" + "\n\n".join(lines),
        max_output_tokens=2000,
        temperature=0.0,
        metadata={"stage": "rerank", "candidate_count": len(head)},
    )
    if not result.ok or not isinstance(result.value, dict):
        return ranked
    entries = result.value.get("ranking")
    if not isinstance(entries, list) or not entries:
        return ranked

    reranked: list[RankedCandidate] = []
    seen: set[int] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        index = entry.get("index")
        if not isinstance(index, int) or not 0 <= index < len(head) or index in seen:
            continue
        seen.add(index)
        base = head[index]
        score = entry.get("score")
        llm_score = float(score) if isinstance(score, int | float) else base.score
        llm_score = min(1.0, max(0.0, llm_score))
        # 与确定性分取平均：LLM 提供语义判断，确定性分守住可解释的下限。
        blended = round((llm_score + base.score) / 2, 4)
        reranked.append(
            RankedCandidate(
                candidate=base.candidate,
                score=blended,
                reason={
                    **base.reason,
                    "method": "deterministic_v1+llm_rerank",
                    "llm_score": round(llm_score, 4),
                    "llm_reason": str(entry.get("reason") or "")[:300],
                    "llm_model": result.model,
                },
            )
        )
    # LLM 漏掉的候选保持确定性顺序附在后面——不因为「没被提及」就丢弃。
    reranked.extend(head[index] for index in range(len(head)) if index not in seen)
    reranked.extend(tail)
    return reranked


def _sort_key(item: RankedCandidate) -> tuple[float, int, str]:
    return (
        item.score,
        item.candidate.publication_year or 0,
        item.candidate.normalized_title_hash or "",
    )


def _tokens(text: str | None) -> set[str]:
    if not text:
        return set()
    return {token.lower() for token in _TOKEN_RE.findall(text)}


def _scope_tokens(scope: dict[str, Any]) -> set[str]:
    tokens: set[str] = set()
    tokens |= _tokens(str(scope.get("topic") or ""))
    tokens |= _tokens(str(scope.get("research_question") or ""))
    for group in scope.get("keyword_groups") or []:
        if isinstance(group, dict):
            for keyword in group.get("keywords") or []:
                tokens |= _tokens(str(keyword))
    return tokens


def _scope_facets(scope: dict[str, Any]) -> list[set[str]]:
    facets: list[set[str]] = []
    for group in scope.get("keyword_groups") or []:
        if not isinstance(group, dict):
            continue
        facet: set[str] = set()
        for keyword in group.get("keywords") or []:
            facet |= _tokens(str(keyword))
        if facet:
            facets.append(facet)
    return facets


def _overlap(wanted: set[str], have: set[str]) -> float:
    if not wanted or not have:
        return 0.0
    return len(wanted & have) / len(wanted)


def _facet_coverage(facets: list[set[str]], have: set[str]) -> float:
    """覆盖了几个正交概念面——比单纯词频更能反映主题贴合度。"""
    if not facets:
        return 0.0
    hit = sum(1 for facet in facets if facet & have)
    return hit / len(facets)


def _recency_score(
    year: int | None,
    start_year: int | None,
    end_year: int,
    current_year: int,
) -> float:
    if year is None:
        return 0.3
    if start_year and year < start_year:
        # 窗口外的老文献不清零：经典文献仍可能是必引。
        return 0.15
    span = max(1, end_year - (start_year or (current_year - 10)))
    position = (year - (start_year or (current_year - 10))) / span
    return min(1.0, max(0.0, position))


def _citation_score(candidate: ScholarlyWorkCandidate) -> float:
    influential = candidate.influential_citation_count
    citations = candidate.citation_count
    value = influential if isinstance(influential, int) else citations
    if not isinstance(value, int) or value <= 0:
        return 0.0
    # 对数压缩：1000 引与 10000 引的差别不该压过主题相关性。
    import math

    return min(1.0, math.log10(value + 1) / 4.0)
