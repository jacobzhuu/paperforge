"""QEMATRIX 阶段：确定性候选检索 + 批量 stance 判定。

R13：跨语言可路由——问题与证据不同语言时，用 comparison_dimensions /
search_query / term_aliases 做桥接；零候选时降级放行，绝不静默产出 0。
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any, cast

from db import (
    clear_automatic_question_evidence_links,
    list_entries,
    list_evidence_measurements,
    list_evidence_units,
    list_question_evidence_links,
    list_research_questions,
    upsert_question_evidence_link,
)

from paperforge_worker.context import JobContext

MAX_CANDIDATES_PER_QUESTION = 24
MAX_CANDIDATES_PER_WORK = 4
MIN_LEXICAL_SCORE = 0.02
# 可比性桥接的上限：桥进来的是"同一比较的另一条臂"，不是新的检索面。
MAX_COMPARABILITY_BRIDGE = 8
# 与 `_deterministic_links` 的准入分级一致；C/D 级不参与跨研究比较。
_COMPARABLE_GRADES = frozenset({"A_located_structured", "B_located_prose"})
# When absolute lexical overlap fails (typical for zh question × en evidence),
# keep a grade-ranked degraded pool so the LLM can still classify.
DEGRADED_CANDIDATE_LIMIT = 12
_STANCE_VALUES = frozenset({"supports", "contradicts", "conditional", "not_comparable", "gap"})
_GRADE_BONUS = {
    "A_located_structured": 0.4,
    "B_located_prose": 0.3,
    "C_fulltext_unlocated": 0.1,
    "D_abstract_only": 0.0,
}
_GRADE_RANK = {
    "A_located_structured": 0,
    "B_located_prose": 1,
    "C_fulltext_unlocated": 2,
    "D_abstract_only": 3,
}

_SYSTEM_PROMPT_ZH = """你判定证据片段与研究子问题的关系。只输出 JSON：
{"links":[{"evidence_id":"UUID","stance":"supports|contradicts|conditional|not_comparable",
"condition_note":"成立条件或不可比原因","confidence":0.0}]}
只能使用给定 evidence_id；不得补充片段之外的事实。D_abstract_only 只表示作者在摘要中报告，
不能把它当作已核验全文。没有关系的候选不要输出。"""

_SYSTEM_PROMPT_EN = """Classify how evidence excerpts relate to one research sub-question.
Output JSON only:
{"links":[{"evidence_id":"UUID","stance":"supports|contradicts|conditional|not_comparable",
"condition_note":"boundary condition or non-comparability reason","confidence":0.0}]}
Use only the supplied evidence IDs and facts. D_abstract_only means an abstract attribution,
not verified full text. Omit unrelated candidates."""


@dataclass
class QuestionMatrixOutcome:
    questions: int = 0
    candidates: int = 0
    links: int = 0
    llm_classified: int = 0
    fallback_classified: int = 0
    rejected_by_task: int = 0
    rejected_by_lexical: int = 0
    zero_candidate_questions: int = 0
    degraded_questions: int = 0
    bridge_sources: dict[str, int] = field(default_factory=dict)
    replaced_questions: int = 0
    retained_previous_questions: int = 0
    retained_previous_links: int = 0
    warnings: list[dict[str, Any]] = field(default_factory=list)
    diagnostics: list[dict[str, Any]] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        return {
            "questions": self.questions,
            "candidates": self.candidates,
            "links": self.links,
            "llm_classified": self.llm_classified,
            "fallback_classified": self.fallback_classified,
            "rejected_by_task": self.rejected_by_task,
            "rejected_by_lexical": self.rejected_by_lexical,
            "zero_candidate_questions": self.zero_candidate_questions,
            "degraded_questions": self.degraded_questions,
            "bridge_sources": self.bridge_sources,
            "replaced_questions": self.replaced_questions,
            "retained_previous_questions": self.retained_previous_questions,
            "retained_previous_links": self.retained_previous_links,
            "warnings": self.warnings,
            "diagnostics": self.diagnostics[:20],
        }


@dataclass(frozen=True)
class _QuestionClassification:
    question: Any
    candidate_count: int
    rejected_by_task: int
    rejected_by_lexical: int
    bridge_source_count: str | None
    routing_mode: str
    bridge_source: str | None
    warning: dict[str, Any] | None
    diagnostic: dict[str, Any]
    classified: list[dict[str, Any]]
    llm_classified: int
    fallback_classified: int


async def build_question_evidence_matrix(
    context: JobContext,
    *,
    language: str,
) -> QuestionMatrixOutcome:
    outcome = QuestionMatrixOutcome()
    async with context.session() as session:
        questions = await list_research_questions(session, context.project_id, kind="sub")
        evidence = await list_evidence_units(session, context.project_id)
        entries = await list_entries(session, context.project_id, status="selected")
        measurements = await list_evidence_measurements(
            session,
            [unit.id for unit in evidence],
        )
        existing_links = await list_question_evidence_links(session, context.project_id)
    work_context = {
        work.id: {
            "title": work.canonical_title or "",
            "abstract": work.abstract or "",
        }
        for _entry, work in entries
    }
    runner = context.llm_runner()
    evidence_by_id = {unit.id: unit for unit in evidence}
    previous_automatic: dict[Any, list[Any]] = {}
    for existing_link in existing_links:
        if not existing_link.manually_overridden:
            previous_automatic.setdefault(existing_link.research_question_id, []).append(
                existing_link
            )
    concurrency = max(1, min(int(context.settings.qmatrix_concurrency), 8))
    for batch_start in range(0, len(questions), concurrency):
        batch = questions[batch_start : batch_start + concurrency]
        results = await asyncio.gather(
            *(
                _classify_question(
                    question,
                    evidence=evidence,
                    measurements=measurements,
                    work_context=work_context,
                    runner=runner,
                    language=language,
                )
                for question in batch
            )
        )
        # LLM classification is concurrent; events and writes stay ordered so
        # SSE/checkpoint semantics and deterministic diagnostics do not change.
        for result in results:
            outcome.questions += 1
            outcome.candidates += result.candidate_count
            outcome.rejected_by_task += result.rejected_by_task
            outcome.rejected_by_lexical += result.rejected_by_lexical
            outcome.llm_classified += result.llm_classified
            outcome.fallback_classified += result.fallback_classified
            if result.bridge_source_count:
                outcome.bridge_sources[result.bridge_source_count] = (
                    outcome.bridge_sources.get(result.bridge_source_count, 0) + 1
                )
            if result.routing_mode == "degraded_lexical_bypass":
                outcome.degraded_questions += 1
                outcome.zero_candidate_questions += 1
            warning = result.warning
            if warning is None and result.diagnostic.get("no_link_reason"):
                warning = {
                    "stage": "qmatrix",
                    "reason": str(result.diagnostic["no_link_reason"]),
                    "question_id": str(result.question.id),
                    "candidate_count": result.candidate_count,
                }
            if warning:
                outcome.warnings.append(warning)
                context.warn(
                    "qmatrix.classification",
                    str(warning.get("reason") or "classification_warning"),
                    warning,
                )
                await context.emit(
                    "qmatrix.warning",
                    warning,
                    stage="qmatrix",
                )
            outcome.diagnostics.append(result.diagnostic)

        # Stage the whole batch before mutating durable links.  A weaker or
        # empty stochastic rerun must never erase a previously useful matrix.
        accepted: list[_QuestionClassification] = []
        for result in results:
            previous = previous_automatic.get(result.question.id, [])
            previous_ids = [link.evidence_unit_id for link in previous]
            new_ids = [link["evidence_id"] for link in result.classified]
            if previous and _link_set_score(new_ids, evidence_by_id) < _link_set_score(
                previous_ids,
                evidence_by_id,
            ):
                outcome.retained_previous_questions += 1
                outcome.retained_previous_links += len(previous)
                result.diagnostic["update_decision"] = "retained_previous"
                continue
            result.diagnostic["update_decision"] = "replaced"
            accepted.append(result)
            outcome.replaced_questions += 1

        if accepted:
            async with context.session() as session:
                await clear_automatic_question_evidence_links(
                    session,
                    [result.question.id for result in accepted],
                )
                for result in accepted:
                    previous_automatic[result.question.id] = []
                    for link_payload in result.classified:
                        stored = await upsert_question_evidence_link(
                            session,
                            research_question_id=result.question.id,
                            evidence_unit_id=link_payload["evidence_id"],
                            stance=link_payload["stance"],
                            condition_note=link_payload.get("condition_note"),
                            confidence=link_payload.get("confidence"),
                            routing_mode=result.routing_mode,
                            bridge_source=result.bridge_source,
                        )
                        previous_automatic[result.question.id].append(stored)
        await context.raise_if_stopped()
    async with context.session() as session:
        outcome.links = len(await list_question_evidence_links(session, context.project_id))
    return outcome


async def _classify_question(
    question: Any,
    *,
    evidence: list[Any],
    measurements: dict[Any, list[Any]],
    work_context: dict[Any, dict[str, Any]],
    runner: Any,
    language: str,
) -> _QuestionClassification:
    ranked, rejected_by_task, rejected_by_lexical, bridge_source = cast(
        tuple[list[tuple[Any, float]], int, int, str | None],
        _rank_candidates(
            question,
            evidence,
            expected_kinds=set(question.expected_evidence_kinds_json or []),
            task_id=question.task_id,
            work_context=work_context,
            measurements=measurements,
            return_diagnostics=True,
        ),
    )
    routing_mode = "lexical"
    bridge_source_count = None
    if bridge_source and bridge_source != "text":
        routing_mode = "bridged"
        bridge_source_count = bridge_source
    candidates = _diverse_ranked_candidates(ranked, measurements=measurements)
    warning = None
    if not candidates and evidence:
        # N0-2: never silently produce zero candidates for a question that has
        # evidence available.  Degrade to a grade-ranked pool and let the
        # cross-lingual classifier decide stance.
        candidates = _degraded_candidates(
            evidence,
            task_id=question.task_id,
            work_context=work_context,
        )
        routing_mode = "degraded_lexical_bypass"
        bridge_source = "degraded"
        warning = {
            "stage": "qmatrix",
            "reason": "zero_candidates",
            "question_id": str(question.id),
            "degraded_count": len(candidates),
        }
    diagnostic = {
        "question_id": str(question.id),
        "candidate_count": len(candidates),
        "rejected_by_task": rejected_by_task,
        "rejected_by_lexical": rejected_by_lexical,
        "best_score": round(ranked[0][1] if ranked else 0.0, 4),
        "routing_mode": routing_mode,
        "bridge_source": bridge_source,
        "classified_count": 0,
        "eligible_abc_count": 0,
        "no_link_reason": None,
    }
    if not candidates:
        diagnostic["no_link_reason"] = "no_evidence_available"
        return _QuestionClassification(
            question,
            0,
            rejected_by_task,
            rejected_by_lexical,
            bridge_source_count,
            routing_mode,
            bridge_source,
            warning,
            diagnostic,
            [],
            0,
            0,
        )

    classified: list[dict[str, Any]] = []
    classifier_returned_valid_payload = False
    fallback_returned_valid_payload = False
    if runner.enabled:
        llm_result = await runner.agenerate_json(
            "evidence_classifier",
            system_prompt=_SYSTEM_PROMPT_ZH if language == "zh" else _SYSTEM_PROMPT_EN,
            user_prompt=_matrix_prompt(
                question.text,
                candidates,
                measurements=measurements,
            ),
            max_output_tokens=3200,
            temperature=0.1,
            metadata={"stage": "qmatrix", "question_id": str(question.id)},
        )
        if (
            llm_result.ok
            and isinstance(llm_result.value, dict)
            and isinstance(llm_result.value.get("links"), list)
        ):
            classifier_returned_valid_payload = True
            classified = _normalize_links(
                llm_result.value.get("links"),
                allowed_ids={str(unit.id) for unit, _score in candidates},
            )
        # Retry on the quality tier with a smaller prompt whenever the first pass yielded no
        # usable links.  Two distinct causes, both answered by the same smaller ask:
        #
        # 1. A syntactically valid empty array is not trustworthy when lexical retrieval found a
        #    strong eligible pool.  Legitimate non-matches stay empty if the second independent
        #    judgement agrees.
        # 2. A truncated or malformed response yields no links at all.  This is the *common* case
        #    -- 31 of 65 production classifier calls truncated -- and it previously skipped the
        #    retry entirely (the gate required a valid payload), falling straight through to the
        #    deterministic fallback below.  That fallback can only ever emit stance="supports";
        #    157 of 367 production links sit at exactly its min(0.85, score) cap, so it is a large
        #    minority of all linkage.  Since synthesis detects conflict only through stance
        #    disagreement, every truncated call permanently removed any chance of a dissenting
        #    stance for that question -- production holds zero `contradicts` links.  Halving the
        #    candidate count is the targeted fix: the output budget, not the model's judgement,
        #    was the binding limit.
        #
        # If the retry itself returns a valid but empty array, that is an explicit decision by the
        # quality tier and the deterministic fallback stays off -- the same rule already applied to
        # the first pass, now applied consistently to whichever pass produced a valid payload.
        if not classified and candidates:
            focused = candidates[:12]
            retry = await runner.agenerate_json(
                "evidence_classifier_fallback",
                system_prompt=(_SYSTEM_PROMPT_ZH if language == "zh" else _SYSTEM_PROMPT_EN),
                user_prompt=_matrix_prompt(
                    question.text,
                    focused,
                    measurements=measurements,
                ),
                max_output_tokens=2400,
                temperature=0.0,
                metadata={
                    "stage": "qmatrix_fallback",
                    "question_id": str(question.id),
                },
            )
            if (
                retry.ok
                and isinstance(retry.value, dict)
                and isinstance(retry.value.get("links"), list)
            ):
                fallback_returned_valid_payload = True
                classified = _normalize_links(
                    retry.value.get("links"),
                    allowed_ids={str(unit.id) for unit, _score in focused},
                )
                diagnostic["fallback_classifier_used"] = True
    # Either pass returning a valid payload means a model judged these candidates.  The retry now
    # also runs after a truncated first pass, so this must not stay keyed on the first pass alone:
    # that would let the deterministic fallback below overwrite links the retry just recovered.
    semantic_payload = classifier_returned_valid_payload or fallback_returned_valid_payload
    llm_classified = len(classified) if semantic_payload else 0
    fallback_classified = 0
    # A valid empty list is an explicit decision.  Deterministic fallback is
    # only for an unavailable/invalid classifier and never promotes a
    # topic-blind degraded pool.
    if not semantic_payload and routing_mode != "degraded_lexical_bypass":
        classified = _deterministic_links(candidates, measurements=measurements)
        fallback_classified = len(classified)
    diagnostic["classified_count"] = len(classified)
    eligible_ids = {
        str(unit.id)
        for unit, _score in candidates
        if unit.grade in {"A_located_structured", "B_located_prose", "C_fulltext_unlocated"}
    }
    diagnostic["eligible_abc_count"] = sum(
        str(link["evidence_id"]) in eligible_ids for link in classified
    )
    if not classified:
        diagnostic["no_link_reason"] = (
            "classifier_rejected_all"
            if semantic_payload
            else (
                "degraded_classifier_unavailable"
                if routing_mode == "degraded_lexical_bypass"
                else "conservative_fallback_empty"
            )
        )
        # 只记一个聚合原因是不够诊断的：``classifier_rejected_all`` 既可能是候选
        # 真的不相关，也可能是提示过严或桥接退化塞进来的无关候选。留下被否掉的
        # 候选样本，才能区分这两种情况——而不是直接把它当作「问题不可研究」。
        diagnostic["rejected_sample"] = [
            {
                "evidence_id": str(unit.id),
                "work_id": str(unit.work_id),
                "grade": unit.grade,
                "score": round(float(score), 4),
                "section_path": unit.section_path,
                "text": (unit.text or "")[:200],
            }
            for unit, score in candidates[:5]
        ]
    return _QuestionClassification(
        question,
        len(candidates),
        rejected_by_task,
        rejected_by_lexical,
        bridge_source_count,
        routing_mode,
        bridge_source,
        warning,
        diagnostic,
        classified,
        llm_classified,
        fallback_classified,
    )


def _link_set_score(
    evidence_ids: list[Any],
    evidence_by_id: dict[Any, Any],
) -> tuple[int, int, int, int]:
    units: list[Any] = []
    for value in evidence_ids:
        unit = evidence_by_id.get(_uuid(str(value)))
        if unit is not None:
            units.append(unit)
    eligible = [
        unit
        for unit in units
        if unit.grade in {"A_located_structured", "B_located_prose", "C_fulltext_unlocated"}
    ]
    return (
        int(bool(eligible)),
        len({unit.work_id for unit in eligible}),
        len(eligible),
        len(units),
    )


def _rank_candidates(
    question: Any,
    evidence: list[Any],
    *,
    expected_kinds: set[str],
    task_id: str | None = None,
    work_context: dict[Any, dict[str, Any]] | None = None,
    measurements: dict[Any, list[Any]] | None = None,
    return_diagnostics: bool = False,
    return_rejected: bool = False,
) -> (
    list[tuple[Any, float]]
    | tuple[list[tuple[Any, float]], int]
    | tuple[list[tuple[Any, float]], int, int, str | None]
):
    """Rank evidence for a question.

    ``question`` may be a ResearchQuestion ORM row, a SimpleNamespace, or a
    plain string (legacy tests). Extra bridge fields are read when present.
    """
    if isinstance(question, str):
        question_text = question
        dimensions: list[str] = []
        search_query = ""
        aliases: list[str] = []
    else:
        question_text = str(getattr(question, "text", "") or "")
        dimensions = list(getattr(question, "comparison_dimensions_json", None) or [])
        search_query = str(getattr(question, "search_query", "") or "")
        aliases = _flatten_aliases(getattr(question, "term_aliases_json", None))

    channels = _question_term_channels(
        question_text,
        dimensions=dimensions,
        search_query=search_query,
        aliases=aliases,
    )
    # Relative threshold: never go below MIN_LEXICAL_SCORE, but if the best
    # scores in the pool are weak, admit the top quantile instead of rejecting
    # everything (N1 relative threshold).
    scored: list[tuple[Any, float, float, str | None]] = []
    rejected_by_task = 0
    for unit in evidence:
        if task_id and getattr(unit, "task_id", None) not in {None, task_id}:
            # Soft when unit has no task_id (pre-ontology rows); hard when mismatched.
            if getattr(unit, "task_id", None) is not None:
                rejected_by_task += 1
                continue
        context = (work_context or {}).get(getattr(unit, "work_id", None), {})
        unit_terms = _terms(
            " ".join(
                (
                    str(context.get("title") or ""),
                    str(context.get("abstract") or ""),
                    str(unit.section_path or ""),
                    str(unit.text or ""),
                )
            )
        )
        channel_scores = [
            (len(terms & unit_terms) / max(1, len(terms)), source)
            for source, terms in channels
            if terms
        ]
        overlap, source = max(channel_scores, default=(0.0, None), key=lambda item: item[0])
        score = overlap + _GRADE_BONUS.get(unit.grade, 0.0)
        if expected_kinds and unit.kind in expected_kinds:
            score += 0.15
        scored.append((unit, score, overlap, source))

    overlaps = [item[2] for item in scored]
    relative_floor = _relative_threshold(overlaps)
    ranked: list[tuple[Any, float]] = []
    rejected_by_lexical = 0
    admitted_sources: dict[str, float] = {}
    for unit, score, overlap, source in scored:
        if overlap >= relative_floor:
            ranked.append((unit, score))
            if source:
                admitted_sources[source] = max(admitted_sources.get(source, 0.0), overlap)
        else:
            rejected_by_lexical += 1
    result = sorted(ranked, key=lambda item: (item[1], item[0].grade), reverse=True)
    bridge_source = (
        max(admitted_sources.items(), key=lambda item: item[1])[0]
        if admitted_sources
        else _preferred_bridge_source(channels)
    )
    if return_diagnostics:
        return result, rejected_by_task, rejected_by_lexical, bridge_source
    if return_rejected:
        return result, rejected_by_task
    return result


def _comparability_bridge(
    selected: list[tuple[Any, float]],
    *,
    pool: list[tuple[Any, float]],
    measurements: dict[Any, list[Any]] | None,
    per_work: dict[Any, int],
    per_work_limit: int,
) -> int:
    """把**跨研究可比簇**的成员补进候选池，就地扩充 ``selected``，返回补入条数。

    稀缺的不是检索面而是名额：候选池按词面得分取前 24 条、每篇最多 4 条，而结果表
    那一段几乎全是数字和模型名，中文子问题的词一个都对不上，于是它排在名额之外。
    生产实测——某项目 618 条证据里 11 条带可比测量、68 条被链接到子问题，两者**只
    重叠 1 条**（随机期望 1.2）。可比簇因此只能靠巧合形成，Phase 4 的规则 2 从来
    没有真实数据可判。

    准入条件是"这个键在**已通过词面判定的**候选里跨了至少两篇论文"——也就是说，
    补进来的正好是会构成一个跨研究比较的那几条，而不是任何带数字的段落。安全性
    来自 ``comparability_key`` 自身：任务/数据集/划分缺一个，键就用证据单元 id 加盐，
    于是天生唯一、永远凑不出跨论文的簇。每篇上限照旧生效。
    """
    if not measurements or not pool:
        return 0
    clusters: dict[str, dict[str, Any]] = {}
    for item in pool:
        unit, _score = item
        if unit.grade not in _COMPARABLE_GRADES:
            continue
        for row in measurements.get(unit.id, []):
            key = str(getattr(row, "comparability_key", "") or "")
            if not key:
                continue
            entry = clusters.setdefault(key, {"works": set(), "units": []})
            entry["works"].add(getattr(unit, "work_id", None))
            entry["units"].append(item)

    chosen = {id(unit) for unit, _score in selected}
    bridged = 0
    for entry in clusters.values():
        if len(entry["works"]) < 2:
            continue
        for item in entry["units"]:
            if bridged >= MAX_COMPARABILITY_BRIDGE:
                return bridged
            unit, _score = item
            if id(unit) in chosen:
                continue
            work_id = getattr(unit, "work_id", None)
            if per_work.get(work_id, 0) >= per_work_limit:
                continue
            selected.append(item)
            chosen.add(id(unit))
            per_work[work_id] = per_work.get(work_id, 0) + 1
            bridged += 1
    return bridged


def _relative_threshold(overlaps: list[float]) -> float:
    """Keep absolute floor when there is no signal; soften only for weak-but-nonzero hits."""
    if not overlaps:
        return MIN_LEXICAL_SCORE
    best = max(overlaps)
    if best <= 0:
        # Force an empty lexical pool so the caller can take the degraded path
        # instead of admitting every unrelated unit.
        return MIN_LEXICAL_SCORE
    if best >= MIN_LEXICAL_SCORE:
        return MIN_LEXICAL_SCORE
    return max(best * 0.5, 1e-6)


def _diverse_ranked_candidates(
    ranked: list[tuple[Any, float]],
    *,
    limit: int = MAX_CANDIDATES_PER_QUESTION,
    per_work_limit: int = MAX_CANDIDATES_PER_WORK,
    measurements: dict[Any, list[Any]] | None = None,
) -> list[tuple[Any, float]]:
    """Keep a strong lexical pool without letting one long paper occupy it.

    Evidence extraction commonly yields dozens of adjacent passages from the
    same full text.  A plain top-K can therefore give the classifier no chance
    to corroborate a claim across papers even when other relevant works exist.
    """
    selected: list[tuple[Any, float]] = []
    per_work: dict[Any, int] = {}
    for item in ranked:
        unit, _score = item
        work_id = getattr(unit, "work_id", None)
        if per_work.get(work_id, 0) >= per_work_limit:
            continue
        selected.append(item)
        per_work[work_id] = per_work.get(work_id, 0) + 1
        if len(selected) >= limit:
            break
    _comparability_bridge(
        selected,
        pool=ranked,
        measurements=measurements,
        per_work=per_work,
        per_work_limit=per_work_limit,
    )
    return selected


def _degraded_candidates(
    evidence: list[Any],
    *,
    task_id: str | None,
    work_context: dict[Any, dict[str, Any]] | None = None,
) -> list[tuple[Any, float]]:
    pool = [
        unit
        for unit in evidence
        if task_id is None or getattr(unit, "task_id", None) in {None, task_id}
    ]
    ranked = sorted(
        pool,
        key=lambda unit: (
            _GRADE_RANK.get(unit.grade, 9),
            -(1 if unit.kind == "experimental_fact" else 0),
            str(unit.id),
        ),
    )
    # A topic-blind fallback should at least be diverse by paper.  The old
    # grade+UUID slice could spend all 12 slots on equations from one work.
    selected: list[Any] = []
    per_work: dict[Any, int] = {}
    for unit in ranked:
        work_id = getattr(unit, "work_id", None)
        if per_work.get(work_id, 0) >= 2:
            continue
        selected.append(unit)
        per_work[work_id] = per_work.get(work_id, 0) + 1
        if len(selected) >= DEGRADED_CANDIDATE_LIMIT:
            break
    return [(unit, _GRADE_BONUS.get(unit.grade, 0.0)) for unit in selected]


def _question_term_channels(
    text: str,
    *,
    dimensions: list[str],
    search_query: str,
    aliases: list[str],
) -> list[tuple[str, set[str]]]:
    """Build independent same-language and cross-language retrieval channels.

    Keeping channels separate prevents Chinese bigrams from diluting an
    otherwise strong English ``search_query`` match.
    """

    raw = [
        ("text", _terms(text)),
        ("search_query", _terms(search_query)),
        ("dimensions", _terms(" ".join(str(item) for item in dimensions if item))),
        ("aliases", _terms(" ".join(aliases))),
    ]
    channels: list[tuple[str, set[str]]] = []
    seen: set[tuple[str, ...]] = set()
    for source, terms in raw:
        latin = {term for term in terms if _is_latin_token(term)}
        native = terms - latin
        for subset in (latin, native):
            if not subset:
                continue
            key = tuple(sorted(subset))
            if key in seen:
                continue
            seen.add(key)
            channels.append((source, subset))
    return channels


def _preferred_bridge_source(channels: list[tuple[str, set[str]]]) -> str | None:
    for preferred in ("text", "search_query", "dimensions", "aliases"):
        if any(source == preferred and terms for source, terms in channels):
            return preferred
    return None


def _question_terms(
    text: str,
    *,
    dimensions: list[str],
    search_query: str,
    aliases: list[str],
) -> tuple[set[str], str | None]:
    """Compatibility helper returning the union used by older callers/tests."""
    channels = _question_term_channels(
        text,
        dimensions=dimensions,
        search_query=search_query,
        aliases=aliases,
    )
    terms = (
        set().union(*(channel_terms for _source, channel_terms in channels)) if channels else set()
    )
    latin_sources = [
        source
        for source, channel_terms in channels
        if any(_is_latin_token(token) for token in channel_terms)
    ]
    source = next(
        (
            preferred
            for preferred in ("text", "search_query", "dimensions", "aliases")
            if preferred in latin_sources
        ),
        _preferred_bridge_source(channels),
    )
    return terms, source


def _flatten_aliases(value: Any) -> list[str]:
    if isinstance(value, dict):
        items: list[str] = []
        for key, mapped in value.items():
            items.append(str(key))
            if isinstance(mapped, (list, tuple)):
                items.extend(str(item) for item in mapped)
            elif mapped:
                items.append(str(mapped))
        return items
    if isinstance(value, list):
        return [str(item) for item in value if item]
    return []


def _is_latin_token(token: str) -> bool:
    return bool(re.fullmatch(r"[a-z][a-z0-9@.-]{1,}", token))


def _matrix_prompt(
    question: str,
    candidates: list[tuple[Any, float]],
    *,
    measurements: dict[Any, list[Any]],
) -> str:
    lines = [f"Sub-question: {question}", "Candidate evidence:"]
    for unit, score in candidates:
        locator = ", ".join(
            value
            for value in (
                f"p.{unit.page}" if unit.page else "",
                unit.section_path or "",
                unit.object_ref or "",
            )
            if value
        )
        measure_text = "; ".join(
            f"{item.metric_name}={item.value}{item.unit or ''}"
            f" dataset={item.dataset or 'unknown'} key={item.comparability_key}"
            for item in measurements.get(unit.id, [])
        )
        lines.append(
            f"- EVIDENCE_ID={unit.id} grade={unit.grade} kind={unit.kind} "
            f"locator={locator or 'unlocated'} retrieval_score={score:.3f}\n"
            f"  text: {unit.text[:1200]}\n"
            f"  measurements: {measure_text or '(none)'}"
        )
    return "\n".join(lines)


def _deterministic_links(
    candidates: list[tuple[Any, float]],
    *,
    measurements: dict[Any, list[Any]],
) -> list[dict[str, Any]]:
    links: list[dict[str, Any]] = []
    for unit, score in candidates:
        dimensions = measurements.get(unit.id, [])
        # A lexical retrieval hit is not evidence support.  In no-LLM mode only
        # retain a conservative, located and sufficiently relevant linkage;
        # abstract/C-grade material is available to the model/UI but is not
        # silently promoted to a supporting answer.
        if unit.grade not in {"A_located_structured", "B_located_prose"} or score < 0.45:
            continue
        condition = "; ".join(
            f"dataset={item.dataset or 'unknown'}, metric={item.metric_name}, "
            f"split={item.split or 'unknown'}"
            for item in dimensions[:3]
        )
        links.append(
            {
                "evidence_id": unit.id,
                "stance": "supports",
                "condition_note": condition or None,
                "confidence": min(0.85, round(score, 4)),
            }
        )
    return links


def _normalize_links(raw: Any, *, allowed_ids: set[str]) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    links: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        evidence_id = str(item.get("evidence_id") or "")
        stance = str(item.get("stance") or "")
        if evidence_id not in allowed_ids or stance not in _STANCE_VALUES or evidence_id in seen:
            continue
        seen.add(evidence_id)
        confidence = item.get("confidence")
        links.append(
            {
                "evidence_id": _uuid(evidence_id),
                "stance": stance,
                "condition_note": _clean(item.get("condition_note")) or None,
                "confidence": (
                    max(0.0, min(1.0, float(confidence)))
                    if isinstance(confidence, (int, float))
                    else None
                ),
            }
        )
    return links


def _terms(text: str) -> set[str]:
    latin = re.findall(r"[a-z][a-z0-9@.-]{1,}", (text or "").casefold())
    cjk_runs = re.findall(r"[\u3400-\u9fff]+", text or "")
    # A whole Chinese clause is never shared with an English source; bigrams
    # preserve local topical signals and match the ranking tokenizer.
    cjk_bigrams = [run[index : index + 2] for run in cjk_runs for index in range(len(run) - 1)]
    return set(latin + cjk_bigrams)


def _clean(value: Any) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def _uuid(value: str):
    import uuid

    return uuid.UUID(value)


__all__ = ["QuestionMatrixOutcome", "build_question_evidence_matrix", "_rank_candidates", "_terms"]
