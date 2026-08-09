"""SYNTH 阶段：在每个子问题内判定一致、条件差异、冲突、不可比与缺口。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from db import (
    list_entries,
    list_evidence_measurements,
    list_evidence_units,
    list_question_evidence_links,
    list_research_questions,
    set_question_answer_status,
)

from paperforge_worker.context import JobContext

FULLTEXT_GRADES = frozenset({"A_located_structured", "B_located_prose", "C_fulltext_unlocated"})


@dataclass
class SynthesisOutcome:
    bundles: list[dict[str, Any]] = field(default_factory=list)
    answered: int = 0
    partial: int = 0
    contested: int = 0
    insufficient: int = 0
    comparison_clusters: int = 0

    def to_payload(self) -> dict[str, Any]:
        return {
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
    return outcome


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
    "load_synthesis_bundles",
    "synthesize_questions",
]
