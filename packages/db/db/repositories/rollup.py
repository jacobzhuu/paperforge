"""Canonical per-paper evidence rollup used by synthesis main tables (R8 / N6)."""

from __future__ import annotations

import uuid
from collections import defaultdict
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from db.repositories.evidence import list_evidence_measurements, list_evidence_units
from db.repositories.library import list_entries


async def list_paper_evidence_rollups(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> list[dict[str, Any]]:
    """One row per selected paper — never one row per evidence unit."""
    entries = await list_entries(session, project_id, status="selected")
    units = await list_evidence_units(session, project_id)
    measurements = await list_evidence_measurements(session, [unit.id for unit in units])
    by_work: dict[Any, list[Any]] = defaultdict(list)
    for unit in units:
        by_work[unit.work_id].append(unit)

    grade_rank = {
        "A_located_structured": 0,
        "B_located_prose": 1,
        "C_fulltext_unlocated": 2,
        "D_abstract_only": 3,
    }
    rows: list[dict[str, Any]] = []
    for entry, work in entries:
        work_units = by_work.get(work.id, [])
        best_grade = None
        if work_units:
            best_grade = sorted(work_units, key=lambda unit: grade_rank.get(unit.grade, 9))[0].grade
        task_ids = sorted({unit.task_id for unit in work_units if getattr(unit, "task_id", None)})
        datasets: list[str] = []
        headline_metric = None
        headline_value = None
        for unit in work_units:
            for item in measurements.get(unit.id, []):
                if item.dataset:
                    datasets.append(str(item.dataset))
                if headline_metric is None and item.metric_name is not None:
                    headline_metric = item.metric_name
                    headline_value = item.value
        rows.append(
            {
                "work_id": str(work.id),
                "cite_key": entry.bibtex_key,
                "title": work.canonical_title,
                "year": work.publication_year,
                "task_ids": task_ids,
                "datasets": list(dict.fromkeys(datasets)),
                "headline_metric": headline_metric,
                "headline_value": headline_value,
                "evidence_grade_best": best_grade,
                "evidence_unit_count": len(work_units),
            }
        )
    return rows


__all__ = ["list_paper_evidence_rollups"]
