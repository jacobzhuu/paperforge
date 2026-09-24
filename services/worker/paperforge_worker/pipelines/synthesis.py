"""SYNTH 阶段：在每个子问题内判定一致、条件差异、冲突、不可比与缺口。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from db import (
    DETERMINISTIC_GENERATOR,
    get_project,
    list_entries,
    list_evidence_measurements,
    list_evidence_units,
    list_project_task_specs,
    list_question_evidence_links,
    list_question_syntheses,
    list_research_questions,
    set_question_answer_status,
    upsert_question_synthesis,
)

from paperforge_worker.concurrency import bounded_map
from paperforge_worker.context import JobContext
from paperforge_worker.pipelines.synthesis_llm import (
    CORE_DIMENSIONS,
    FULLTEXT_GRADES,
    QuestionSynthesisResult,
    bundle_fingerprint,
    should_synthesize,
    synthesis_policy,
    synthesize_bundle,
)


@dataclass
class SynthesisOutcome:
    bundles: list[dict[str, Any]] = field(default_factory=list)
    answered: int = 0
    partial: int = 0
    contested: int = 0
    insufficient: int = 0
    comparison_clusters: int = 0
    # 叙述性综合（Phase 4）。关闭时全为 0，payload 里也不出现。
    synthesis_generated: int = 0
    synthesis_reused: int = 0
    synthesis_skipped: int = 0
    synthesis_rejected: int = 0

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "sub_questions": len(self.bundles),
            "answered": self.answered,
            "partial": self.partial,
            "contested": self.contested,
            "insufficient": self.insufficient,
            "comparison_clusters": self.comparison_clusters,
            "bundles": [
                {
                    "question_id": bundle["question_id"],
                    "answer_status": bundle["answer_status"],
                    "stance_summary": bundle["stance_summary"],
                    "evidence_count": len(bundle["evidence"]),
                }
                for bundle in self.bundles
            ],
        }
        if self.synthesis_generated or self.synthesis_reused or self.synthesis_skipped:
            payload["synthesis"] = {
                "generated": self.synthesis_generated,
                "reused": self.synthesis_reused,
                "skipped": self.synthesis_skipped,
                "rejected_entries": self.synthesis_rejected,
            }
        return payload


async def synthesize_questions(context: JobContext) -> SynthesisOutcome:
    outcome = await load_synthesis_bundles(context)
    questions_by_id: dict[str, Any]
    async with context.session() as session:
        questions = await list_research_questions(session, context.project_id)
        questions_by_id = {str(question.id): question for question in questions}
        for bundle in outcome.bundles:
            question = questions_by_id.get(bundle["question_id"])
            if question is not None:
                await set_question_answer_status(
                    session,
                    question,
                    bundle["answer_status"],
                )
        core_questions = [question for question in questions if question.kind == "core"]
        if core_questions:
            statuses = {bundle["answer_status"] for bundle in outcome.bundles}
            core_status = (
                "insufficient_evidence"
                if not statuses or statuses == {"insufficient_evidence"}
                else "partial"
                if "insufficient_evidence" in statuses or "partial" in statuses
                else "contested"
                if "contested" in statuses
                else "answered"
            )
            await set_question_answer_status(session, core_questions[0], core_status)
    # 叙述性综合放在判定之后，且不回写任何判定：readiness 与写作前置门禁看到的
    # answer_status 与开关状态无关。
    await enrich_with_synthesis(context, outcome)
    return outcome


async def load_synthesis_bundles(context: JobContext) -> SynthesisOutcome:
    async with context.session() as session:
        questions = await list_research_questions(session, context.project_id, kind="sub")
        links = await list_question_evidence_links(session, context.project_id)
        evidence = await list_evidence_units(session, context.project_id)
        measurements = await list_evidence_measurements(
            session,
            [unit.id for unit in evidence],
        )
        entries = await list_entries(session, context.project_id, status="selected")
    evidence_by_id = {unit.id: unit for unit in evidence}
    work_context = {
        work.id: {
            "cite_key": entry.bibtex_key,
            "title": work.canonical_title,
            "year": work.publication_year,
        }
        for entry, work in entries
    }
    links_by_question: dict[Any, list[Any]] = {}
    for link in links:
        links_by_question.setdefault(link.research_question_id, []).append(link)

    outcome = SynthesisOutcome()
    for question in questions:
        question_links = links_by_question.get(question.id, [])
        linked = [
            (link, evidence_by_id[link.evidence_unit_id])
            for link in question_links
            if link.evidence_unit_id in evidence_by_id
        ]
        bundle = _synthesize_bundle(
            question,
            linked=linked,
            measurements=measurements,
            work_context=work_context,
        )
        outcome.bundles.append(bundle)
        outcome.comparison_clusters += len(bundle["comparison_clusters"])
        if bundle["answer_status"] == "answered":
            outcome.answered += 1
        elif bundle["answer_status"] == "partial":
            outcome.partial += 1
        elif bundle["answer_status"] == "contested":
            outcome.contested += 1
        else:
            outcome.insufficient += 1
    await _attach_persisted_synthesis(context, outcome)
    return outcome


async def _bind_synthesis_policy(context: JobContext, outcome: SynthesisOutcome) -> None:
    async with context.session() as session:
        project = await get_project(session, context.project_id)
        specs = await list_project_task_specs(
            session,
            context.project_id,
            fallback=context.settings.task_profile_fallback,
        )
    dimensions = frozenset(str(v).casefold() for spec in specs for v in spec.dimensions)
    policy = synthesis_policy(
        model=context.settings.llm_config().model_for_role("synthesizer"),
        language=str(getattr(project, "language", "en") or "en"),
        dimensions=dimensions | frozenset(CORE_DIMENSIONS),
    )
    for bundle in outcome.bundles:
        bundle["_synthesis_policy"] = policy


async def _attach_persisted_synthesis(context: JobContext, outcome: SynthesisOutcome) -> None:
    """把已存在的综合挂回 bundle。纯读，不会产生任何调用。

    ``load_synthesis_bundles`` 有五个调用点（大纲、写作、补充、对齐），它们都该看到
    同一段叙述；生成只发生在 ``synthesize_questions`` 里。
    """
    if not context.settings.synthesis_llm_enabled:
        return
    await _bind_synthesis_policy(context, outcome)
    fingerprints = {bundle["question_id"]: bundle_fingerprint(bundle) for bundle in outcome.bundles}
    for bundle in outcome.bundles:
        bundle["synthesis"] = None
    if not fingerprints:
        return
    async with context.session() as session:
        rows = await list_question_syntheses(
            session,
            project_id=context.project_id,
            bundle_hashes=list(fingerprints.values()),
        )
    stored = {(str(row.research_question_id), row.bundle_hash): row for row in rows}
    for bundle in outcome.bundles:
        row = stored.get((bundle["question_id"], fingerprints[bundle["question_id"]]))
        if row is not None and row.generator != DETERMINISTIC_GENERATOR:
            bundle["synthesis"] = _synthesis_payload(row)


def _synthesis_payload(row: Any) -> dict[str, Any]:
    return {
        "generator": row.generator,
        "claim": row.claim,
        "agreement": row.agreement_json or [],
        "conditional": row.conditional_json or [],
        "conflict": row.conflict_json or [],
        "gap": row.gap_json or [],
    }


async def enrich_with_synthesis(context: JobContext, outcome: SynthesisOutcome) -> None:
    """为还没有综合的 bundle 各跑一次调用，落库并挂回。

    失败一律降级成"没有综合"：bundle 保持确定性结构，正文照写。
    """
    if not context.settings.synthesis_llm_enabled or not outcome.bundles:
        return
    await _bind_synthesis_policy(context, outcome)
    fingerprints = {bundle["question_id"]: bundle_fingerprint(bundle) for bundle in outcome.bundles}

    async with context.session() as session:
        project = await get_project(session, context.project_id)
        specs = await list_project_task_specs(
            session,
            context.project_id,
            fallback=context.settings.task_profile_fallback,
        )
        rows = await list_question_syntheses(
            session,
            project_id=context.project_id,
            bundle_hashes=list(fingerprints.values()),
        )
    # 已有行（包括 generator='deterministic' 的负缓存）一律不重跑：那正是
    # bundle_hash 存在的意义——SYNTH 在一次完整流水线里最多跑 4 次。
    stored = {(str(row.research_question_id), row.bundle_hash) for row in rows}
    dimensions = frozenset(
        {str(value).casefold() for spec in specs for value in spec.dimensions}
    ) | frozenset(CORE_DIMENSIONS)

    runner = context.llm_runner()
    budget = max(0, int(context.settings.synthesis_llm_max_questions))
    language = str(getattr(project, "language", "en") or "en")

    # 复用判定、should_synthesize、预算切分**都不依赖 LLM 结果**，所以先按串行的
    # 原顺序把该跑的挑出来，再并发跑。选中的是同一批 bundle、同一个预算切法，
    # 计数器也在这一趟按原顺序累加——并发只改变这些调用什么时候发出。
    #
    # 实测（job 1b6ba10a）：18 次 synthesizer 调用、6.1 分钟，全部首尾相接。
    selected: list[dict[str, Any]] = []
    for bundle in outcome.bundles:
        if (bundle["question_id"], fingerprints[bundle["question_id"]]) in stored:
            outcome.synthesis_reused += 1
            continue
        if not should_synthesize(bundle):
            outcome.synthesis_skipped += 1
            continue
        if budget <= 0:
            outcome.synthesis_skipped += 1
            continue
        budget -= 1
        selected.append(bundle)
    if not selected:
        return

    async def _synthesize_one(bundle: dict[str, Any]) -> Any:
        return await synthesize_bundle(
            bundle=bundle,
            runner=runner,
            allowed_dimensions=dimensions,
            language=language,
        )

    async def _persist_one(_index: int, bundle: dict[str, Any], result: Any) -> None:
        if isinstance(result, BaseException):
            # 综合是增补，失败绝不能拖垮 SYNTH 阶段。
            context.warn("synth", "llm_call_failed", {"question_id": bundle["question_id"]})
            return
        if result is None:
            outcome.synthesis_skipped += 1
            return
        await _persist_synthesis(context, bundle=bundle, result=result, outcome=outcome)

    # 落库与计数只在 on_ready 里做，按输入序串行——`_persist_synthesis` 要占一条
    # 数据库连接，并发落库既会打乱顺序也会吃池子。
    await bounded_map(
        selected,
        _synthesize_one,
        limit=context.settings.synthesis_concurrency,
        on_ready=_persist_one,
        stop_check=context.raise_if_stopped,
    )


async def _persist_synthesis(
    context: JobContext,
    *,
    bundle: dict[str, Any],
    result: QuestionSynthesisResult,
    outcome: SynthesisOutcome,
) -> None:
    outcome.synthesis_rejected += len(result.rejected)
    empty = result.is_empty
    if empty:
        # 一条都没留下也要落行：它是负缓存，同一个 bundle 不再重复付费。
        context.warn(
            "synth",
            "llm_rejected",
            {
                "question_id": bundle["question_id"],
                "rejected": [item["reason"] for item in result.rejected][:8],
            },
        )
    async with context.session() as session:
        await upsert_question_synthesis(
            session,
            project_id=context.project_id,
            research_question_id=uuid.UUID(bundle["question_id"]),
            bundle_hash=bundle_fingerprint(bundle),
            generator=DETERMINISTIC_GENERATOR if empty else result.generator,
            claim=result.claim,
            agreement=[entry.to_payload() for entry in result.agreement],
            conditional=[entry.to_payload() for entry in result.conditional],
            conflict=[entry.to_payload() for entry in result.conflict],
            gap=[entry.to_payload() for entry in result.gap],
        )
    if empty:
        return
    bundle["synthesis"] = result.to_payload()
    outcome.synthesis_generated += 1


def _synthesize_bundle(
    question: Any,
    *,
    linked: list[tuple[Any, Any]],
    measurements: dict[Any, list[Any]],
    work_context: dict[Any, dict[str, Any]],
) -> dict[str, Any]:
    evidence_rows: list[dict[str, Any]] = []
    groups: dict[str, list[dict[str, Any]]] = {}
    eligible_count = 0
    eligible_work_ids: set[str] = set()
    for link, unit in linked:
        context = work_context.get(unit.work_id, {})
        unit_measurements = measurements.get(unit.id, [])
        measurement_rows = [
            {
                "metric_name": item.metric_name,
                "value": item.value,
                "unit": item.unit,
                "dataset": item.dataset,
                "task": item.task,
                "sample_size": item.sample_size,
                "split": item.split,
                "comparability_key": item.comparability_key,
            }
            for item in unit_measurements
        ]
        row = {
            "evidence_id": str(unit.id),
            "work_id": str(unit.work_id),
            "cite_key": context.get("cite_key"),
            "title": context.get("title"),
            "year": context.get("year"),
            "kind": unit.kind,
            "grade": unit.grade,
            "text": unit.text,
            "page": unit.page,
            "section_path": unit.section_path,
            "paragraph_index": unit.paragraph_index,
            "object_ref": unit.object_ref,
            "stance": link.stance,
            "condition_note": link.condition_note,
            "confidence": link.confidence,
            "measurements": measurement_rows,
        }
        from paperforge_worker.pipelines.scholarly_content import enrich_evidence

        row = enrich_evidence(row)
        evidence_rows.append(row)
        eligible_count += int(unit.grade in FULLTEXT_GRADES)
        if unit.grade in FULLTEXT_GRADES:
            eligible_work_ids.add(str(unit.work_id))
        for measurement in measurement_rows:
            groups.setdefault(measurement["comparability_key"], []).append(row)

    comparison_clusters: list[dict[str, Any]] = []
    has_conflict = False
    has_conditional = False
    for key, rows in groups.items():
        unique_rows = list({row["evidence_id"]: row for row in rows}.values())
        if len(unique_rows) < 2:
            continue
        stances = {row["stance"] for row in unique_rows}
        conditions = {row["condition_note"] for row in unique_rows if row.get("condition_note")}
        if "supports" in stances and "contradicts" in stances:
            classification = "conflicting"
            has_conflict = True
        elif "conditional" in stances or len(conditions) > 1:
            classification = "conditional"
            has_conditional = True
        elif stances <= {"supports"}:
            classification = "consistent"
        else:
            classification = "mixed"
        comparison_clusters.append(
            {
                "comparability_key": key,
                "classification": classification,
                "evidence_ids": [row["evidence_id"] for row in unique_rows],
                "cite_keys": list(
                    dict.fromkeys(row["cite_key"] for row in unique_rows if row["cite_key"])
                ),
                "conditions": sorted(conditions),
            }
        )

    distinct_keys = set(groups)
    non_comparable = []
    if len(distinct_keys) > 1:
        non_comparable = [
            {
                "comparability_key": key,
                "evidence_ids": list(dict.fromkeys(row["evidence_id"] for row in rows)),
            }
            for key, rows in groups.items()
        ]
    if eligible_count == 0:
        answer_status = "insufficient_evidence"
        stance_summary = "insufficient"
    elif has_conflict:
        answer_status = "contested"
        stance_summary = "conflicting"
    elif eligible_count < 2 or len(eligible_work_ids) < 2 or (groups and not comparison_clusters):
        answer_status = "partial"
        stance_summary = "conditional" if has_conditional else "partial"
    else:
        answer_status = "answered"
        stance_summary = "conditional" if has_conditional else "consistent"

    return {
        "question_id": str(question.id),
        "question": question.text,
        "order_index": question.order_index,
        "comparison_dimensions": question.comparison_dimensions_json or [],
        "expected_evidence_kinds": question.expected_evidence_kinds_json or [],
        "answer_status": answer_status,
        "stance_summary": stance_summary,
        "evidence": evidence_rows,
        "comparison_clusters": comparison_clusters,
        "not_comparable_groups": non_comparable,
        "evidence_gap": (
            None
            if eligible_count
            else "No grade A/B/C full-text evidence is linked to this sub-question."
        ),
    }


__all__ = [
    "FULLTEXT_GRADES",
    "SynthesisOutcome",
    "enrich_with_synthesis",
    "load_synthesis_bundles",
    "synthesize_questions",
]
