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

from paperforge_worker.concurrency import bounded_map

_LATIN_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9\-]+")
_CJK_RUN_RE = re.compile(r"[一-鿿]{2,}")

# 功能词不携带主题信息。research_question 的模板句（"What is the state of the art in …"）
# 会把它们灌进主题词集合，于是任何一篇英文摘要都能靠 the/of/in 拿到虚高的重叠分。
_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "from",
        "that",
        "this",
        "these",
        "those",
        "are",
        "was",
        "were",
        "has",
        "have",
        "had",
        "its",
        "their",
        "our",
        "using",
        "used",
        "use",
        "via",
        "based",
        "into",
        "over",
        "under",
        "between",
        "among",
        "such",
        "than",
        "then",
        "when",
        "where",
        "which",
        "what",
        "who",
        "how",
        "why",
        "can",
        "may",
        "might",
        "will",
        "would",
        "state",
        "art",
        "novel",
        "new",
        "approach",
        "approaches",
        "study",
        "studies",
        "paper",
        "papers",
        "results",
        "result",
        "show",
        "shows",
        "propose",
        "proposed",
        "we",
        "it",
        "is",
        "of",
        "in",
        "on",
        "to",
        "by",
        "as",
        "at",
        "an",
        "or",
        "be",
        "not",
        # 中文侧同理：这些二元组在任何中文题录里都出现，不构成主题证据。
        "研究",
        "综述",
        "分析",
        "方法",
        "基于",
        "我们",
        "本文",
        "一种",
        "以及",
    }
)

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


# 自动入库的主题证据下限。低于这个值意味着标题、摘要、概念面、LLM 判断
# 四个信号**全都**没有把候选和主题联系起来——分数只是时效与引用量堆出来的。
AUTO_SELECT_MIN_TOPIC_EVIDENCE = 0.15
# Absolute floor for the final blended score. Prefer selecting fewer papers over
# padding the library with off-topic noise (N3 / N-RC6).
AUTO_SELECT_MIN_ABSOLUTE_SCORE = 0.35
RERANK_BATCH_SIZE = 20


def balanced_selection_indices(
    ranked: list[RankedCandidate],
    *,
    limit: int,
    now: datetime | None = None,
) -> set[int]:
    """按相关性、年代与 OA 可得性分层选取，避免 top-K 只追逐最新年份。"""
    if limit <= 0:
        return set()
    clock = now or datetime.now(UTC)
    eligible = [
        index
        for index, item in enumerate(ranked)
        if topic_evidence(item) >= AUTO_SELECT_MIN_TOPIC_EVIDENCE
        and item.score >= AUTO_SELECT_MIN_ABSOLUTE_SCORE
    ]
    foundational = [
        index
        for index in eligible
        if (ranked[index].candidate.publication_year or clock.year) <= clock.year - 6
    ]
    recent = [
        index
        for index in eligible
        if (ranked[index].candidate.publication_year or 0) >= clock.year - 3
    ]
    oa = [
        index
        for index in eligible
        if ranked[index].candidate.oa_status not in {None, "closed"}
        or ranked[index].candidate.pmcid
        or ranked[index].candidate.arxiv_id
        or any(link.is_oa for link in ranked[index].candidate.links)
    ]
    selected: list[int] = []

    def take(pool: list[int], count: int) -> None:
        for index in pool:
            if index not in selected:
                selected.append(index)
            if len([item for item in selected if item in pool]) >= count or len(selected) >= limit:
                return

    take(foundational, max(1, round(limit * 0.25)) if foundational else 0)
    take(recent, max(1, round(limit * 0.4)) if recent else 0)
    take(oa, max(1, round(limit * 0.4)) if oa else 0)
    # Fill remaining slots only from the eligible pool — never drop the absolute
    # score floor just to hit the numeric limit.
    take(eligible, limit)
    return set(selected[:limit])


@dataclass(frozen=True)
class RankedCandidate:
    candidate: ScholarlyWorkCandidate
    score: float
    reason: dict[str, Any] = field(default_factory=dict)


def topic_evidence(item: RankedCandidate) -> float:
    """候选与主题之间最强的那一路证据（取四个信号的最大值）。

    总分里时效占 0.12、引用量占 0.08，一篇 2025 年的高引论文即使跟主题毫无关系
    也能拿到 0.15 左右——所以「是否够格自动入库」不能看总分，只能看主题证据本身。
    """
    signals = (
        item.reason.get("title_match"),
        item.reason.get("abstract_match"),
        item.reason.get("facet_coverage"),
        item.reason.get("llm_score"),
    )
    return max(
        (float(value) for value in signals if isinstance(value, int | float)),
        default=0.0,
    )


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
    concurrency: int = 1,
) -> list[RankedCandidate]:
    """对确定性 top-N 做 LLM 重排（reranker 角色，便宜档）。

    Draft-first：LLM 不可用或输出不合法时原样返回确定性排序。
    LLM 只调整顺序与理由，**不能**引入新文献，也不改变任何元数据。
    Batches of ≤20 avoid output truncation that previously abandoned the whole
    rerank (N3).
    """
    if runner is None or not runner.enabled or not ranked:
        return ranked
    head = ranked[: max(1, top_n)]
    tail = ranked[len(head) :]
    question = scope.get("research_question") or scope.get("topic") or ""
    remapped: list[RankedCandidate | None] = [None] * len(head)
    # 每批的 LLM 调用彼此独立，写入的 `remapped` 下标也互不相交。并发只改变调用
    # 什么时候发出：解析与写入仍在 on_ready 里按批次序串行做，重排结果逐位不变。
    batches = [
        (batch_start, head[batch_start : batch_start + RERANK_BATCH_SIZE])
        for batch_start in range(0, len(head), RERANK_BATCH_SIZE)
    ]

    async def _rerank_batch(entry: tuple[int, list[RankedCandidate]]) -> Any:
        batch_start, batch = entry
        lines = []
        for local_index, item in enumerate(batch):
            abstract = (item.candidate.abstract or "").replace("\n", " ")[:400]
            year = item.candidate.publication_year or "n.d."
            lines.append(f"[{local_index}] ({year}) {item.candidate.title}\n{abstract}")
        return await runner.agenerate_json(
            "reranker",
            system_prompt=_SYSTEM_PROMPT,
            user_prompt=(f"Research question: {question}\n\nCandidates:\n" + "\n\n".join(lines)),
            max_output_tokens=min(3200, 400 + 80 * len(batch)),
            temperature=0.0,
            metadata={
                "stage": "rerank",
                "candidate_count": len(batch),
                "batch_start": batch_start,
            },
        )

    async def _apply_batch(
        _index: int,
        entry: tuple[int, list[RankedCandidate]],
        result: Any,
    ) -> None:
        batch_start, batch = entry
        # 一批失败只损失这一批的重排：下面的补位循环会把它们按确定性顺序放回去。
        if isinstance(result, BaseException):
            return
        seen: set[int] = set()
        if result.ok and isinstance(result.value, dict):
            entries = result.value.get("ranking")
            if isinstance(entries, list):
                for ranking_entry in entries:
                    if not isinstance(ranking_entry, dict):
                        continue
                    local_index = ranking_entry.get("index")
                    if (
                        not isinstance(local_index, int)
                        or not 0 <= local_index < len(batch)
                        or local_index in seen
                    ):
                        continue
                    seen.add(local_index)
                    base = batch[local_index]
                    score = ranking_entry.get("score")
                    llm_score = float(score) if isinstance(score, int | float) else base.score
                    llm_score = min(1.0, max(0.0, llm_score))
                    blended = round((llm_score + base.score) / 2, 4)
                    remapped[batch_start + local_index] = RankedCandidate(
                        candidate=base.candidate,
                        score=blended,
                        reason={
                            **base.reason,
                            "method": "deterministic_v1+llm_rerank",
                            "llm_score": round(llm_score, 4),
                            "llm_reason": str(ranking_entry.get("reason") or "")[:300],
                            "llm_model": result.model,
                        },
                    )

    await bounded_map(
        batches,
        _rerank_batch,
        limit=max(1, int(concurrency)),
        on_ready=_apply_batch,
    )

    # 没有被 LLM 覆盖到的位置按确定性顺序补回。原来这一步在每批之后做，
    # 移到全部批次之后是等价的：它只填 None 槽。
    for global_index, item in enumerate(head):
        if remapped[global_index] is None:
            remapped[global_index] = item
    reranked = [item for item in remapped if item is not None]
    reranked.sort(key=_sort_key, reverse=True)
    reranked.extend(tail)
    return reranked


def _sort_key(item: RankedCandidate) -> tuple[float, int, str]:
    return (
        item.score,
        item.candidate.publication_year or 0,
        item.candidate.normalized_title_hash or "",
    )


def _tokens(text: str | None) -> set[str]:
    """英文按词、中文按二元组切分。

    单个汉字不是有意义的检索单位：按字切分时「的/系/统」这类高频字会让任何一篇
    中文文献都跟任何一个中文主题产生重叠——一批中文医学论文曾因此在
    「序列推荐系统的投毒攻击」项目里拿到 0.09–0.34 的相关性分并被自动入库。
    二元组要求字面连续，跨主题的偶然重合会掉到接近零。
    """
    if not text:
        return set()
    tokens = {token.lower() for token in _LATIN_TOKEN_RE.findall(text)}
    for run in _CJK_RUN_RE.findall(text):
        tokens |= {run[index : index + 2] for index in range(len(run) - 1)}
    return tokens - _STOPWORDS


def _scope_tokens(scope: dict[str, Any]) -> set[str]:
    tokens: set[str] = set()
    tokens |= _tokens(str(scope.get("topic") or ""))
    tokens |= _tokens(str(scope.get("research_question") or ""))
    for group in scope.get("keyword_groups") or []:
        if isinstance(group, dict):
            for keyword in group.get("keywords") or []:
                tokens |= _tokens(str(keyword))
    return tokens


def _scope_facets(scope: dict[str, Any]) -> list[list[set[str]]]:
    """Return keyword phrases grouped by conceptual facet.

    A facet is satisfied only when a whole phrase matches; ``prediction``
    alone cannot satisfy “biosynthetic gene cluster prediction”.
    """
    facets: list[list[set[str]]] = []
    for group in scope.get("keyword_groups") or []:
        if not isinstance(group, dict):
            continue
        phrases: list[set[str]] = []
        for keyword in group.get("keywords") or []:
            phrase = _tokens(str(keyword))
            if phrase:
                phrases.append(phrase)
        if phrases:
            facets.append(phrases)
    return facets


def _overlap(wanted: set[str], have: set[str]) -> float:
    if not wanted or not have:
        return 0.0
    return len(wanted & have) / len(wanted)


def _facet_coverage(facets: list[list[set[str]]], have: set[str]) -> float:
    """覆盖了几个正交概念面——比单纯词频更能反映主题贴合度。"""
    if not facets:
        return 0.0
    hit = sum(1 for alternatives in facets if any(phrase <= have for phrase in alternatives))
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
