"""证据单元与可比较测量仓储。"""

from __future__ import annotations

import hashlib
import json
import uuid

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models.library import (
    EvidenceMeasurement,
    EvidenceUnit,
    LibraryEntry,
)
from db.text_safety import sanitize_pg_text

EVIDENCE_KINDS = frozenset(
    {
        "experimental_fact",
        "theoretical_derivation",
        "author_conclusion",
        "review_restatement",
        "model_inference",
    }
)
EVIDENCE_GRADES = frozenset(
    {
        "A_located_structured",
        "B_located_prose",
        "C_fulltext_unlocated",
        "D_abstract_only",
    }
)


def comparability_key(
    *,
    task: str | None,
    dataset: str | None,
    metric_name: str,
    split: str | None,
    attack_goal: str | None = None,
    threat_model: str | None = None,
    victim_model: str | None = None,
    attack_budget: dict | None = None,
    protocol: dict | None = None,
    unknown_salt: str | None = None,
) -> str:
    """Deterministic comparison key; unknown protocol is never treated as equal."""

    def normalized(value: str | None) -> str:
        return " ".join((value or "").casefold().split())

    def structured(value: dict | None) -> str:
        return json.dumps(
            value or {},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    dimensions = (task, dataset, split, attack_goal, threat_model, victim_model)
    # ``protocol`` is an optional extension; the core comparison dimensions
    # are sufficient when all are explicitly reported.
    unknown = any(value is None or not str(value).strip() for value in dimensions[:3])
    payload = "|".join(
        (
            normalized(task),
            normalized(dataset),
            normalized(metric_name),
            normalized(split),
            normalized(attack_goal),
            normalized(threat_model),
            normalized(victim_model),
            structured(attack_budget),
            structured(protocol),
            # Unknown protocols must be unique per evidence unit.  This keeps
            # incomplete reports out of cross-study comparison clusters.
            f"unknown:{unknown_salt or uuid.uuid4()}" if unknown else "known",
        )
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


async def upsert_evidence_unit(
    session: AsyncSession,
    *,
    work_id: uuid.UUID,
    project_id: uuid.UUID | None,
    kind: str,
    grade: str,
    text: str,
    text_hash: str,
    page: int | None = None,
    section_path: str | None = None,
    paragraph_index: int | None = None,
    object_ref: str | None = None,
    char_start: int | None = None,
    char_end: int | None = None,
    source_document_file_id: uuid.UUID | None = None,
    extraction_model: str | None = None,
    source_hash: str | None = None,
    task_id: str | None = None,
    topical_status: str = "uncertain",
    anchor_strength: str = "prose_only",
    locator_display: str | None = None,
) -> tuple[EvidenceUnit, bool]:
    if kind not in EVIDENCE_KINDS:
        raise ValueError(f"unsupported evidence kind: {kind}")
    if grade not in EVIDENCE_GRADES:
        raise ValueError(f"unsupported evidence grade: {grade}")
    project_clause = (
        EvidenceUnit.project_id.is_(None)
        if project_id is None
        else EvidenceUnit.project_id == project_id
    )
    document_clause = (
        EvidenceUnit.source_document_file_id.is_(None)
        if source_document_file_id is None
        else EvidenceUnit.source_document_file_id == source_document_file_id
    )
    unit = await session.scalar(
        select(EvidenceUnit)
        .where(
            EvidenceUnit.work_id == work_id,
            project_clause,
            EvidenceUnit.text_hash == text_hash,
            document_clause,
        )
        .limit(1)
    )
    created = unit is None
    safe_text = sanitize_pg_text(text) or ""
    if unit is None:
        unit = EvidenceUnit(
            work_id=work_id,
            project_id=project_id,
            text_hash=text_hash,
            text=safe_text,
            kind=kind,
            grade=grade,
        )
        session.add(unit)
    unit.kind = kind
    unit.grade = grade
    unit.text = safe_text
    unit.page = page
    unit.section_path = section_path
    unit.paragraph_index = paragraph_index
    unit.object_ref = object_ref
    unit.char_start = char_start
    unit.char_end = char_end
    unit.source_document_file_id = source_document_file_id
    unit.extraction_model = extraction_model
    unit.source_hash = source_hash
    unit.task_id = task_id
    unit.topical_status = topical_status
    unit.anchor_strength = anchor_strength
    unit.locator_display = locator_display
    await session.flush()
    return unit, created


async def list_evidence_units(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    work_id: uuid.UUID | None = None,
    grades: set[str] | None = None,
    limit: int = 2000,
) -> list[EvidenceUnit]:
    stmt = (
        select(EvidenceUnit)
        .join(LibraryEntry, LibraryEntry.work_id == EvidenceUnit.work_id)
        .where(
            LibraryEntry.project_id == project_id,
            LibraryEntry.status == "selected",
            or_(EvidenceUnit.project_id.is_(None), EvidenceUnit.project_id == project_id),
        )
        .order_by(
            EvidenceUnit.work_id,
            EvidenceUnit.page.asc().nullslast(),
            EvidenceUnit.paragraph_index.asc().nullslast(),
            EvidenceUnit.created_at,
        )
        .limit(limit)
    )
    if work_id is not None:
        stmt = stmt.where(EvidenceUnit.work_id == work_id)
    if grades:
        unsupported = grades - EVIDENCE_GRADES
        if unsupported:
            raise ValueError(f"unsupported evidence grades: {sorted(unsupported)}")
        stmt = stmt.where(EvidenceUnit.grade.in_(grades))
    return list((await session.scalars(stmt)).unique().all())


async def upsert_evidence_measurement(
    session: AsyncSession,
    *,
    evidence_unit_id: uuid.UUID,
    metric_name: str,
    value: float,
    unit: str | None = None,
    ci_low: float | None = None,
    ci_high: float | None = None,
    std: float | None = None,
    dataset: str | None = None,
    task: str | None = None,
    model_family: str | None = None,
    sample_size: int | None = None,
    split: str | None = None,
    attack_goal: str | None = None,
    threat_model: str | None = None,
    victim_model: str | None = None,
    attack_budget: dict | None = None,
    protocol: dict | None = None,
) -> EvidenceMeasurement:
    key = comparability_key(
        task=task,
        dataset=dataset,
        metric_name=metric_name,
        split=split,
        attack_goal=attack_goal,
        threat_model=threat_model,
        victim_model=victim_model,
        attack_budget=attack_budget,
        protocol=protocol,
        unknown_salt=str(evidence_unit_id),
    )
    measurement = await session.scalar(
        select(EvidenceMeasurement)
        .where(
            EvidenceMeasurement.evidence_unit_id == evidence_unit_id,
            EvidenceMeasurement.metric_name == metric_name,
            EvidenceMeasurement.value == value,
            (
                EvidenceMeasurement.dataset.is_(None)
                if dataset is None
                else EvidenceMeasurement.dataset == dataset
            ),
            (
                EvidenceMeasurement.split.is_(None)
                if split is None
                else EvidenceMeasurement.split == split
            ),
        )
        .limit(1)
    )
    if measurement is None:
        measurement = EvidenceMeasurement(
            evidence_unit_id=evidence_unit_id,
            metric_name=metric_name,
            value=value,
            comparability_key=key,
        )
        session.add(measurement)
    measurement.unit = unit
    measurement.ci_low = ci_low
    measurement.ci_high = ci_high
    measurement.std = std
    measurement.dataset = dataset
    measurement.task = task
    measurement.model_family = model_family
    measurement.attack_goal = attack_goal
    measurement.threat_model = threat_model
    measurement.victim_model = victim_model
    measurement.attack_budget_json = attack_budget
    measurement.protocol_json = protocol
    measurement.sample_size = sample_size
    measurement.split = split
    measurement.comparability_key = key
    await session.flush()
    return measurement


async def list_evidence_measurements(
    session: AsyncSession,
    evidence_unit_ids: list[uuid.UUID],
) -> dict[uuid.UUID, list[EvidenceMeasurement]]:
    if not evidence_unit_ids:
        return {}
    rows = (
        await session.scalars(
            select(EvidenceMeasurement)
            .where(EvidenceMeasurement.evidence_unit_id.in_(evidence_unit_ids))
            .order_by(EvidenceMeasurement.evidence_unit_id, EvidenceMeasurement.metric_name)
        )
    ).all()
    grouped: dict[uuid.UUID, list[EvidenceMeasurement]] = {}
    for row in rows:
        grouped.setdefault(row.evidence_unit_id, []).append(row)
    return grouped


def evidence_payload(
    unit: EvidenceUnit,
    measurements: list[EvidenceMeasurement] | None = None,
) -> dict:
    return {
        "id": str(unit.id),
        "work_id": str(unit.work_id),
        "kind": unit.kind,
        "task_id": unit.task_id,
        "topical_status": unit.topical_status,
        "anchor_strength": unit.anchor_strength,
        "locator_display": unit.locator_display,
        "grade": unit.grade,
        "text": unit.text,
        "page": unit.page,
        "section_path": unit.section_path,
        "paragraph_index": unit.paragraph_index,
        "object_ref": unit.object_ref,
        "measurements": [
            {
                key: getattr(item, key)
                for key in (
                    "metric_name",
                    "value",
                    "unit",
                    "ci_low",
                    "ci_high",
                    "std",
                    "dataset",
                    "task",
                    "model_family",
                    "attack_goal",
                    "threat_model",
                    "victim_model",
                    "attack_budget_json",
                    "protocol_json",
                    "sample_size",
                    "split",
                    "comparability_key",
                )
            }
            for item in measurements or []
        ],
    }


__all__ = [
    "EVIDENCE_GRADES",
    "EVIDENCE_KINDS",
    "comparability_key",
    "evidence_payload",
    "list_evidence_measurements",
    "list_evidence_units",
    "upsert_evidence_measurement",
    "upsert_evidence_unit",
]
