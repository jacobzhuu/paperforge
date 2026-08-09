"""Task ontology repository — domain knowledge lives in data, not code (R16)."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models.paper import ProjectTaskProfile, TaskDefinition

#: 未绑定任务的项目在 ``generic_only`` 回退下解析到的任务。
GENERIC_TASK_SLUG = "generic.scholarly"


@dataclass(frozen=True)
class TaskVocabulary:
    """一个任务的论文**预计会报告**什么。空词表表示「不知道该找什么」。

    这是有意的语义：对领域外论文，留空严格优于填错。此前 `evidence.py` 里硬编码的
    正则会把任何提到 "BERT" 的化学论文标成 `pretrained_backbone=BERT`。

    ``split_strategies`` / ``task_variants`` 是 (提示词, 规范名) 对，列表顺序即匹配
    优先级——它们替换的是原来的 if 链，顺序变了归一结果就会变。
    """

    model_families: tuple[str, ...] = ()
    input_representations: tuple[str, ...] = ()
    pretrained_backbones: tuple[str, ...] = ()
    split_strategies: tuple[tuple[str, str], ...] = ()
    task_variants: tuple[tuple[str, str], ...] = ()

    @property
    def empty(self) -> bool:
        return not (
            self.model_families
            or self.input_representations
            or self.pretrained_backbones
            or self.split_strategies
            or self.task_variants
        )


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
    vocabulary: TaskVocabulary = field(default_factory=TaskVocabulary)

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


def _as_cue_pairs(value: Any) -> tuple[tuple[str, str], ...]:
    """把 [{"cue": .., "name": ..}] 读成有序 (提示词, 规范名) 对。

    结构不对的条目静默跳过，与 ``_as_tuple`` 的防御口径一致：一份手写坏了的词表
    应该少匹配几个词，而不是让整条证据管线抛异常。
    """
    if not isinstance(value, list):
        return ()
    pairs: list[tuple[str, str]] = []
    for item in value:
        if isinstance(item, dict):
            cue = str(item.get("cue") or "").strip()
            name = str(item.get("name") or cue).strip()
        elif isinstance(item, str):
            cue = name = item.strip()
        else:
            continue
        if cue and name:
            pairs.append((cue, name))
    return tuple(pairs)


def _vocabulary_from_row(value: Any) -> TaskVocabulary:
    if not isinstance(value, dict):
        return TaskVocabulary()
    return TaskVocabulary(
        model_families=_as_tuple(value.get("model_families")),
        input_representations=_as_tuple(value.get("input_representations")),
        pretrained_backbones=_as_tuple(value.get("pretrained_backbones")),
        split_strategies=_as_cue_pairs(value.get("split_strategies")),
        task_variants=_as_cue_pairs(value.get("task_variants")),
    )


def vocabulary_for_tasks(tasks: list[TaskSpec]) -> TaskVocabulary:
    """合并项目内所有任务的词表，保序去重。

    保序很重要：``split_strategies`` 与 ``task_variants`` 是先命中先赢，顺序决定
    "cluster-based random split" 归一成哪一个。
    """

    def _merge(values: Any) -> tuple[str, ...]:
        return tuple(dict.fromkeys(item for task in tasks for item in values(task)))

    def _merge_pairs(values: Any) -> tuple[tuple[str, str], ...]:
        merged: dict[str, tuple[str, str]] = {}
        for task in tasks:
            for cue, name in values(task):
                merged.setdefault(cue.casefold(), (cue, name))
        return tuple(merged.values())

    return TaskVocabulary(
        model_families=_merge(lambda task: task.vocabulary.model_families),
        input_representations=_merge(lambda task: task.vocabulary.input_representations),
        pretrained_backbones=_merge(lambda task: task.vocabulary.pretrained_backbones),
        split_strategies=_merge_pairs(lambda task: task.vocabulary.split_strategies),
        task_variants=_merge_pairs(lambda task: task.vocabulary.task_variants),
    )


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
        vocabulary=_vocabulary_from_row(getattr(row, "vocabulary_json", None)),
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
    *,
    fallback: str = "all_tasks",
) -> list[TaskSpec]:
    """Tasks bound to a project, with a configurable unbound-project fallback.

    ``fallback='all_tasks'`` 是历史行为，也是当前默认：没有 ProjectTaskProfile 的项目
    继承**全部**任务定义。由于没有任何路由会创建 profile 行，实践中每个项目——化学、
    临床、材料——都拿到了 recsys 与 BGC 的指标和数据集白名单。这正是领域词表外泄的
    机制（审计 §4.6 只描述了症状）。

    ``fallback='generic_only'`` 解析到 ``generic.scholarly``：空词表、空数据集，只保留
    真正跨领域的指标核心。它会缩小未绑定项目的匹配面，因此由 TASK_PROFILE_FALLBACK
    控制切换，而不是直接改默认。
    """
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
    if str(fallback).strip().lower() == "generic_only":
        generic = by_slug.get(GENERIC_TASK_SLUG)
        # 迁移尚未跑到的库里没有这一行；那时退回旧行为总好过让证据阶段拿到空列表。
        return [generic] if generic is not None else all_tasks
    return all_tasks


_SLUG_RE = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")


def task_spec_to_payload(spec: TaskSpec) -> dict[str, Any]:
    """TaskSpec → 可往返的 JSON（admin export 的输出格式）。"""
    return {
        "slug": spec.slug,
        "domain": spec.domain,
        "labels": dict(sorted(spec.labels.items())),
        "metrics": list(spec.metrics),
        "datasets": list(spec.datasets),
        "dimensions": list(spec.dimensions),
        "inclusion_cues": list(spec.inclusion_cues),
        "exclusion_cues": list(spec.exclusion_cues),
        "vocabulary": {
            "model_families": list(spec.vocabulary.model_families),
            "input_representations": list(spec.vocabulary.input_representations),
            "pretrained_backbones": list(spec.vocabulary.pretrained_backbones),
            "split_strategies": [
                {"cue": cue, "name": name} for cue, name in spec.vocabulary.split_strategies
            ],
            "task_variants": [
                {"cue": cue, "name": name} for cue, name in spec.vocabulary.task_variants
            ],
        },
    }


def validate_task_payloads(payloads: Any) -> list[str]:
    """返回人可读的错误清单；空列表表示可以写库。

    在写任何一行之前全部校验完：一次 upsert 要么整体生效，要么整体不生效，
    不能留下半套本体让证据阶段读到不一致的词表。
    """
    errors: list[str] = []
    if not isinstance(payloads, list):
        return ["top level must be a list of task objects"]
    seen_slugs: set[str] = set()
    for index, item in enumerate(payloads):
        where = f"[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{where}: must be an object")
            continue
        slug = str(item.get("slug") or "").strip()
        domain = str(item.get("domain") or "").strip()
        where = f"[{index}] {slug or '<no slug>'}"
        if not slug or not _SLUG_RE.match(slug):
            errors.append(f"{where}: slug must match {_SLUG_RE.pattern}")
        if slug in seen_slugs:
            errors.append(f"{where}: duplicate slug")
        seen_slugs.add(slug)
        if not domain:
            errors.append(f"{where}: domain is required")
        labels = item.get("labels")
        if not isinstance(labels, dict) or not labels:
            errors.append(f"{where}: labels must be a non-empty object")
        for key in ("metrics", "datasets", "dimensions", "inclusion_cues", "exclusion_cues"):
            value = item.get(key, [])
            if not isinstance(value, list) or any(not str(entry).strip() for entry in value):
                errors.append(f"{where}: {key} must be a list of non-empty strings")
        errors.extend(_validate_vocabulary(item.get("vocabulary"), where))
    return errors


def task_ontology_warnings(payloads: list[dict[str, Any]]) -> list[str]:
    """非阻断的本体提示。

    共享 inclusion cue **不是错误**：现有 recsys 任务刻意共用
    "sequential recommendation" 这类线索，而 ``infer_task_id`` 是按命中条数与最长
    线索打分的，共享因此是有意义的。但共享确实会让归类更依赖打分细节，值得在
    作者写本体时看到——所以是提示，不是拒绝。
    """
    cues_by_domain: dict[str, dict[str, list[str]]] = {}
    for item in payloads:
        if not isinstance(item, dict):
            continue
        domain = str(item.get("domain") or "").strip()
        slug = str(item.get("slug") or "").strip()
        bucket = cues_by_domain.setdefault(domain, {})
        for cue in item.get("inclusion_cues", []) or []:
            normalized = str(cue).strip().casefold()
            if normalized:
                bucket.setdefault(normalized, []).append(slug)
    warnings: list[str] = []
    for domain, bucket in sorted(cues_by_domain.items()):
        for cue, owners in sorted(bucket.items()):
            if len(owners) > 1:
                warnings.append(
                    f"{domain}: cue {cue!r} is shared by {len(owners)} tasks "
                    f"({', '.join(sorted(owners))})"
                )
    return warnings


def _validate_vocabulary(value: Any, where: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, dict):
        return [f"{where}: vocabulary must be an object"]
    errors: list[str] = []
    for key in ("model_families", "input_representations", "pretrained_backbones"):
        entries = value.get(key, [])
        if not isinstance(entries, list) or any(not str(entry).strip() for entry in entries):
            errors.append(f"{where}: vocabulary.{key} must be a list of non-empty strings")
    for key in ("split_strategies", "task_variants"):
        entries = value.get(key, [])
        if not isinstance(entries, list):
            errors.append(f"{where}: vocabulary.{key} must be a list")
            continue
        for entry in entries:
            if isinstance(entry, str) and entry.strip():
                continue
            if isinstance(entry, dict) and str(entry.get("cue") or "").strip():
                continue
            errors.append(f"{where}: vocabulary.{key} entries need a non-empty cue")
    unknown = set(value) - {
        "model_families",
        "input_representations",
        "pretrained_backbones",
        "split_strategies",
        "task_variants",
    }
    if unknown:
        errors.append(f"{where}: unknown vocabulary keys {sorted(unknown)}")
    return errors


async def upsert_task_definitions(
    session: AsyncSession,
    payloads: list[dict[str, Any]],
) -> dict[str, int]:
    """写入任务本体；调用方必须先跑 ``validate_task_payloads``。"""
    existing = {row.slug: row for row in (await session.scalars(select(TaskDefinition))).all()}
    created = updated = 0
    for item in payloads:
        slug = str(item["slug"]).strip()
        row = existing.get(slug)
        if row is None:
            row = TaskDefinition(slug=slug, domain=str(item["domain"]).strip(), label_i18n_json={})
            session.add(row)
            created += 1
        else:
            updated += 1
        row.domain = str(item["domain"]).strip()
        row.label_i18n_json = dict(item.get("labels") or {})
        row.metric_whitelist_json = list(item.get("metrics") or [])
        row.dataset_whitelist_json = list(item.get("datasets") or [])
        row.dimension_schema_json = list(item.get("dimensions") or [])
        row.inclusion_cues_json = list(item.get("inclusion_cues") or [])
        row.exclusion_cues_json = list(item.get("exclusion_cues") or [])
        row.vocabulary_json = _normalized_vocabulary(item.get("vocabulary"))
    await session.flush()
    return {"created": created, "updated": updated}


def _normalized_vocabulary(value: Any) -> dict[str, Any]:
    """归一成 export 会写出的同一形状，round-trip 才能真的无 diff。"""
    vocabulary = _vocabulary_from_row(value if isinstance(value, dict) else {})
    return {
        "model_families": list(vocabulary.model_families),
        "input_representations": list(vocabulary.input_representations),
        "pretrained_backbones": list(vocabulary.pretrained_backbones),
        "split_strategies": [
            {"cue": cue, "name": name} for cue, name in vocabulary.split_strategies
        ],
        "task_variants": [{"cue": cue, "name": name} for cue, name in vocabulary.task_variants],
    }


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


#: 真正跨领域的指标：任何量化学科都可能报告它们。
#:
#: 这个列表此前叫 "universal"，却混进了 tanimoto（化学信息学）、asr / er@k / ndcg /
#: hr / mrr / map（推荐与攻击）这些领域指标。于是一篇临床论文里的 "HR"（hazard ratio）
#: 会被当成 recsys 的 Hit Rate 抽出来。领域指标现在只从 task_definition 的
#: metric_whitelist_json 进入，由 `metrics_for_tasks` 汇集。
UNIVERSAL_METRICS: tuple[str, ...] = (
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
    "rmse",
    "mae",
    r"r\^?2",
    r"p[- ]?value",
    "correlation",
    "bleu",
    r"rouge(?:-[l12])?",
)


def metric_alternation(metrics: list[str]) -> str:
    """通用核心 + 本体白名单，合成一条正则交替式。

    `@K` 形式的指标展开成两条：``NDCG@K`` 既要能匹配 ``NDCG@10``，也要能匹配裸的
    ``NDCG``。这是唯一一处把指标词表编成正则的地方——散文抽取与表头解析共用它，
    以免同一份词表被抄成三份（Phase 3 之前正是三份）。
    """
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
    return "|".join(dict.fromkeys([*UNIVERSAL_METRICS, *extras]))


def compile_metric_pattern(metrics: list[str]) -> re.Pattern[str]:
    """散文里的「指标 = 数值」抽取。"""
    return re.compile(
        rf"(?P<metric>{metric_alternation(metrics)})"
        r"\s*(?:score\s*)?(?:=|:|of|was|is|reached|达到|为)?\s*"
        r"(?P<value>-?\d+(?:\.\d+)?)\s*(?P<unit>%|percent|percentage points?|百分点)?",
        re.IGNORECASE,
    )


def compile_metric_name_pattern(metrics: list[str]) -> re.Pattern[str]:
    """只认指标名，供 Markdown 表头解析使用（表头单元格里没有数值）。"""
    return re.compile(metric_alternation(metrics), re.IGNORECASE)


def compile_dataset_pattern(datasets: list[str]) -> re.Pattern[str] | None:
    if not datasets:
        return None
    escaped = "|".join(re.escape(name) for name in sorted(datasets, key=len, reverse=True))
    return re.compile(rf"\b(?:{escaped})\b", re.IGNORECASE)


__all__ = [
    "GENERIC_TASK_SLUG",
    "UNIVERSAL_METRICS",
    "TaskSpec",
    "TaskVocabulary",
    "compile_dataset_pattern",
    "compile_metric_name_pattern",
    "compile_metric_pattern",
    "datasets_for_tasks",
    "infer_task_id",
    "list_project_task_specs",
    "list_task_definitions",
    "metric_alternation",
    "metrics_for_tasks",
    "task_ontology_warnings",
    "task_spec_to_payload",
    "topical_status",
    "upsert_task_definitions",
    "validate_task_payloads",
    "vocabulary_for_tasks",
]
