"""Task ontology repository — domain knowledge lives in data, not code (R16)."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models.paper import ProjectTaskProfile, TaskDefinition


@dataclass(frozen=True)
class TaskSpec:
    slug: str
    domain: str
    labels: dict[str, str] = field(default_factory=dict)
    metrics: tuple[str, ...] = ()
    datasets: tuple[str, ...] = ()
    dimensions: tuple[str, ...] = ()
    exclusion_cues: tuple[str, ...] = ()
    inclusion_cues: tuple[str, ...] = ()

    @property
    def all_cues(self) -> tuple[str, ...]:
        labels = tuple(self.labels.values())
        return tuple(
            dict.fromkeys(
                cue for cue in (*self.inclusion_cues, *labels, *self.datasets, self.slug) if cue
            )
        )


def _as_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def _spec_from_row(row: TaskDefinition) -> TaskSpec:
    labels = row.label_i18n_json if isinstance(row.label_i18n_json, dict) else {}
    return TaskSpec(
        slug=row.slug,
        domain=row.domain,
        labels={str(k): str(v) for k, v in labels.items()},
        metrics=_as_tuple(row.metric_whitelist_json),
        datasets=_as_tuple(row.dataset_whitelist_json),
        dimensions=_as_tuple(row.dimension_schema_json),
        exclusion_cues=_as_tuple(row.exclusion_cues_json),
        inclusion_cues=_as_tuple(getattr(row, "inclusion_cues_json", None)),
    )


async def list_task_definitions(
    session: AsyncSession,
    *,
    domain: str | None = None,
) -> list[TaskSpec]:
    stmt = select(TaskDefinition).order_by(TaskDefinition.domain, TaskDefinition.slug)
    if domain:
        stmt = stmt.where(TaskDefinition.domain == domain)
    rows = list((await session.scalars(stmt)).all())
    return [_spec_from_row(row) for row in rows]


async def list_project_task_specs(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> list[TaskSpec]:
    """Tasks bound to a project; falls back to all definitions when unbound."""
    profile = list(
        (
            await session.scalars(
                select(ProjectTaskProfile)
                .where(ProjectTaskProfile.project_id == project_id)
                .order_by(ProjectTaskProfile.order_index, ProjectTaskProfile.task_id)
            )
        ).all()
    )
    all_tasks = await list_task_definitions(session)
    by_slug = {task.slug: task for task in all_tasks}
    if profile:
        return [by_slug[row.task_id] for row in profile if row.task_id in by_slug]
    return all_tasks


def infer_task_id(text: str, tasks: list[TaskSpec]) -> str | None:
    """Pick the best matching task slug from ontology cues; None if none match."""
    lower = (text or "").casefold()
    if not lower or not tasks:
        return None
    best: tuple[int, TaskSpec] | None = None
    for task in tasks:
        score = sum(1 for cue in task.all_cues if cue.casefold() in lower)
        # Prefer more specific (longer) cues on ties by counting cue length.
        score = score * 100 + max(
            (len(cue) for cue in task.all_cues if cue.casefold() in lower), default=0
        )
        if score <= 0:
            continue
        if best is None or score > best[0]:
            best = (score, task)
    return best[1].slug if best else None


def topical_status(text: str, tasks: list[TaskSpec]) -> str:
    lower = (text or "").casefold()
    exclusions = [cue.casefold() for task in tasks for cue in task.exclusion_cues]
    if any(cue in lower for cue in exclusions):
        return "off_topic"
    inclusions = [cue.casefold() for task in tasks for cue in task.inclusion_cues]
    if any(cue in lower for cue in inclusions):
        return "on_topic"
    return "uncertain"


def metrics_for_tasks(tasks: list[TaskSpec]) -> list[str]:
    return list(dict.fromkeys(metric for task in tasks for metric in task.metrics))


def datasets_for_tasks(tasks: list[TaskSpec]) -> list[str]:
    return list(dict.fromkeys(dataset for task in tasks for dataset in task.datasets))


def compile_metric_pattern(metrics: list[str]) -> re.Pattern[str]:
    """Build a metric regex from ontology whitelist + universal scholarly metrics."""
    universal = [
        "auroc",
        "auprc",
        "auc",
        "accuracy",
        "precision",
        "recall",
        "macro[- ]?f1",
        "f[- ]?measure",
        "f1(?:-score)?",
        "mcc",
        "tanimoto",
        r"top[- ]?k\s+accuracy",
        "bleu",
        r"rouge(?:-[l12])?",
        "mrr",
        "map",
        r"ndcg@\d+",
        r"hr@\d+",
        r"hit\s*rate(?:@\d+)?",
        "asr",
        r"er@\d+",
        r"recall@\d+",
        "ndcg",
        "hr",
    ]
    extras: list[str] = []
    for metric in metrics:
        raw = metric.strip()
        if not raw:
            continue
        if re.search(r"@\s*k$", raw, re.I):
            base = re.sub(r"@\s*k$", "", raw, flags=re.I)
            extras.append(re.escape(base) + r"@\d+")
            extras.append(re.escape(base))
        else:
            extras.append(re.escape(raw))
    joined = "|".join(dict.fromkeys([*universal, *extras]))
    return re.compile(
        rf"(?P<metric>{joined})"
        r"\s*(?:score\s*)?(?:=|:|of|was|is|reached|达到|为)?\s*"
        r"(?P<value>-?\d+(?:\.\d+)?)\s*(?P<unit>%|percent|percentage points?|百分点)?",
        re.IGNORECASE,
    )


def compile_dataset_pattern(datasets: list[str]) -> re.Pattern[str] | None:
    if not datasets:
        return None
    escaped = "|".join(re.escape(name) for name in sorted(datasets, key=len, reverse=True))
    return re.compile(rf"\b(?:{escaped})\b", re.IGNORECASE)


__all__ = [
    "TaskSpec",
    "compile_dataset_pattern",
    "compile_metric_pattern",
    "datasets_for_tasks",
    "infer_task_id",
    "list_project_task_specs",
    "list_task_definitions",
    "metrics_for_tasks",
    "topical_status",
]
