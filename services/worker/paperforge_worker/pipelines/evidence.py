"""EVIDENCE 阶段：卡片/全文 → 可定位、分级的 EvidenceUnit。"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from db import (
    comparability_key,
    get_cards,
    list_entries,
    list_project_task_specs,
    upsert_evidence_measurement,
    upsert_evidence_unit,
)
from db.models.library import (
    DocumentChunk,
    DocumentFile,
    DocumentParse,
    EvidenceUnit,
    ExperimentResult,
    StructuredExtraction,
)
from db.repositories.tasks import (
    TaskSpec,
    compile_dataset_pattern,
    compile_metric_name_pattern,
    compile_metric_pattern,
    datasets_for_tasks,
    infer_task_id,
    metrics_for_tasks,
    vocabulary_for_tasks,
)
from db.repositories.tasks import (
    topical_status as ontology_topical_status,
)
from ingest import classify_content_role, parse_markdown_table
from sqlalchemy import or_, select

from paperforge_worker.context import JobContext
from paperforge_worker.pipelines.experiment_extraction import (
    ExperimentExtraction,
    ExtractedResultCell,
    build_locator_index,
    extract_experiment_results,
    reconcile,
)
from paperforge_worker.pipelines.fulltext import load_persisted_fulltext_sources

MAX_EVIDENCE_UNITS_PER_WORK = 32
MAX_EVIDENCE_TEXT_CHARS = 1_600

_MARKER_RE = re.compile(r"\[\[(?P<locator>[^\]]+)\]\]\s*")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?。！？])\s+")
_RESULT_CUES = re.compile(
    r"\b(?:result|experiment|evaluat|outperform|improv|decreas|increas|"
    r"conclu|find|observ|demonstrat|significant|accuracy|precision|recall|"
    r"f1|ndcg|auc|dataset|sample|theorem|proof|derive|limitation)\w*\b|"
    r"(?:结果|实验|评估|优于|提升|下降|增加|结论|发现|观察|证明|数据集|样本|定理|局限)",
    re.IGNORECASE,
)
# 无本体时的兜底：只用真正跨领域的指标核心。领域指标（NDCG@K、ASR、Tanimoto…）
# 只能经由 task_definition 的 metric_whitelist_json 进入。
_METRIC_RE = compile_metric_pattern([])
_METRIC_NAME_RE = compile_metric_name_pattern([])
_SAMPLE_RE = re.compile(
    r"(?:\bn\s*=\s*|sample(?: size)?(?: of|=|:)?\s*|样本(?:量|数)?(?:为|=|:)?\s*)"
    r"(?P<value>\d[\d,]*)",
    re.IGNORECASE,
)
_DATASET_RE = re.compile(
    r"(?:\bon\b|dataset(?:=|:)?|数据集(?:为|=|:)?)[\s\"“]*(?P<name>[A-Za-z][\w.-]{1,80})",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class EvidenceCandidate:
    text: str
    page: int | None = None
    section_path: str | None = None
    paragraph_index: int | None = None
    object_ref: str | None = None
    grade: str = "C_fulltext_unlocated"
    anchor_strength: str = "prose_only"


@dataclass(frozen=True)
class MeasurementCandidate:
    metric_name: str
    value: float
    unit: str | None = None
    dataset: str | None = None
    task: str | None = None
    sample_size: int | None = None
    split: str | None = None


@dataclass
class EvidenceOutcome:
    selected_works: int = 0
    works: int = 0
    works_failed: int = 0
    usable_works: int = 0
    units: int = 0
    usable_units: int = 0
    created: int = 0
    reused: int = 0
    measurements: int = 0
    grades: dict[str, int] = field(default_factory=dict)
    # LLM 结构化抽取（Phase 2）。off 档下全部为零，payload 形状不变。
    llm_extraction_works: int = 0
    llm_cells_accepted: int = 0
    llm_cells_locator_verified: int = 0
    llm_cells_rejected: int = 0
    llm_value_conflicts: int = 0
    llm_budget_skipped: int = 0

    def to_payload(self) -> dict[str, Any]:
        return {
            "selected_works": self.selected_works,
            "works": self.works,
            "works_ok": self.works,
            "works_failed": self.works_failed,
            "processing_coverage": (
                self.works / self.selected_works if self.selected_works else 0.0
            ),
            "coverage": (self.usable_works / self.selected_works if self.selected_works else 0.0),
            "usable_works": self.usable_works,
            "units": self.units,
            "usable_units": self.usable_units,
            "created": self.created,
            "reused": self.reused,
            "measurements": self.measurements,
            "grades": self.grades,
            "llm_extraction": {
                "works": self.llm_extraction_works,
                "cells_accepted": self.llm_cells_accepted,
                "cells_locator_verified": self.llm_cells_locator_verified,
                "cells_rejected": self.llm_cells_rejected,
                "value_conflicts": self.llm_value_conflicts,
                "budget_skipped": self.llm_budget_skipped,
                "locator_verification_rate": (
                    self.llm_cells_locator_verified / self.llm_cells_accepted
                    if self.llm_cells_accepted
                    else 0.0
                ),
            },
        }


async def extract_evidence_units(
    context: JobContext,
    *,
    fulltexts: dict[str, str] | None = None,
    work_ids: list[Any] | None = None,
) -> EvidenceOutcome:
    """双写 EvidenceUnit；旧卡片字段继续保留，旧项目仍可直接渲染。"""
    fulltexts = dict(fulltexts or {})
    outcome = EvidenceOutcome()
    async with context.session() as session:
        entries = await list_entries(session, context.project_id, status="selected")
        if work_ids is not None:
            requested_work_ids = {str(work_id) for work_id in work_ids}
            entries = [
                (entry, work) for entry, work in entries if str(work.id) in requested_work_ids
            ]
        cards = await get_cards(session, context.project_id)
        selected_work_ids = [work.id for _entry, work in entries]
    persisted_sources = await load_persisted_fulltext_sources(context, selected_work_ids)
    for work_id, source in persisted_sources.items():
        if source.is_private:
            fulltexts[work_id] = source.text
        else:
            fulltexts.setdefault(work_id, source.text)
    source_document_ids = {source.document_file_id for source in persisted_sources.values()}
    async with context.session() as session:
        source_documents = (
            list(
                (
                    await session.scalars(
                        select(DocumentFile).where(
                            DocumentFile.id.in_(source_document_ids),
                        )
                    )
                ).all()
            )
            if source_document_ids
            else []
        )
    documents_by_id = {document.id: document for document in source_documents}

    outcome.selected_works = len(entries)
    async with context.session() as session:
        task_specs = await list_project_task_specs(
            session,
            context.project_id,
            fallback=context.settings.task_profile_fallback,
        )
    ontology_metrics = metrics_for_tasks(task_specs)
    metric_pattern = compile_metric_pattern(ontology_metrics)
    metric_name_pattern = compile_metric_name_pattern(ontology_metrics)
    dataset_pattern = compile_dataset_pattern(datasets_for_tasks(task_specs))
    # LLM 抽取按 job 计预算，用完的文献静默退回纯正则路径（并计数留痕）。
    llm_budget = (
        int(context.settings.experiment_extraction_max_works)
        if context.settings.experiment_extraction_enabled
        else 0
    )
    for index, (_entry, work) in enumerate(entries):
        fulltext = fulltexts.get(str(work.id))
        persisted_source = persisted_sources.get(str(work.id))
        document = (
            documents_by_id.get(persisted_source.document_file_id)
            if persisted_source is not None and persisted_source.text == fulltext
            else None
        )
        try:
            work_outcome = await _extract_work_evidence(
                context,
                work=work,
                card=cards.get(work.id),
                fulltext=fulltext,
                document=document,
                tasks=task_specs,
                metric_pattern=metric_pattern,
                metric_name_pattern=metric_name_pattern,
                dataset_pattern=dataset_pattern,
                llm_budget_remaining=llm_budget,
            )
        except Exception as error:  # noqa: BLE001 - one bad paper must not abort evidence coverage
            outcome.works_failed += 1
            context.warn(
                "evidence.work_failed",
                type(error).__name__,
                {"work_id": str(work.id), "message": str(error)[:300]},
            )
            await context.emit(
                "evidence.work_failed",
                {"work_id": str(work.id), "error": type(error).__name__},
                stage="evidence",
            )
        else:
            _merge_evidence_outcome(outcome, work_outcome)
            llm_budget = max(0, llm_budget - work_outcome.llm_extraction_works)
        if index % 5 == 0:
            await context.emit(
                "evidence.progress",
                {
                    "done": index + 1,
                    "total": len(entries),
                    "units": outcome.units,
                    "works_ok": outcome.works,
                    "works_failed": outcome.works_failed,
                },
                stage="evidence",
            )
        await context.raise_if_stopped()
    return outcome


def _merge_evidence_outcome(target: EvidenceOutcome, source: EvidenceOutcome) -> None:
    """Accumulate a paper-local result only after its transaction work succeeded."""
    target.works += source.works
    target.usable_works += source.usable_works
    target.units += source.units
    target.usable_units += source.usable_units
    target.created += source.created
    target.reused += source.reused
    target.measurements += source.measurements
    target.llm_extraction_works += source.llm_extraction_works
    target.llm_cells_accepted += source.llm_cells_accepted
    target.llm_cells_locator_verified += source.llm_cells_locator_verified
    target.llm_cells_rejected += source.llm_cells_rejected
    target.llm_value_conflicts += source.llm_value_conflicts
    target.llm_budget_skipped += source.llm_budget_skipped
    for grade, count in source.grades.items():
        target.grades[grade] = target.grades.get(grade, 0) + count


async def _extract_work_evidence(
    context: JobContext,
    *,
    work: Any,
    card: Any,
    fulltext: str | None,
    document: DocumentFile | None,
    tasks: list[TaskSpec],
    metric_pattern: re.Pattern[str],
    metric_name_pattern: re.Pattern[str] | None,
    dataset_pattern: re.Pattern[str] | None,
    llm_budget_remaining: int = 0,
) -> EvidenceOutcome:
    """Extract one work in isolation so a malformed source cannot stop the stage."""
    outcome = EvidenceOutcome()
    candidates = _evidence_candidates(
        fulltext=fulltext,
        abstract=work.abstract,
        quotable_points=(card.quotable_points_json if card else None) or [],
        fulltext_used=bool(card and card.fulltext_used and fulltext),
    )
    if not candidates:
        return outcome
    outcome.works = 1
    source_hash = hashlib.sha256(
        "\u241f".join((work.canonical_title or "", work.abstract or "", fulltext or "")).encode(
            "utf-8"
        )
    ).hexdigest()[:40]
    document = document if fulltext else None
    private_source = bool(fulltext) and (document is None or document.access_scope == "private")
    unit_ids_by_text: dict[str, Any] = {}
    for candidate in candidates[:MAX_EVIDENCE_UNITS_PER_WORK]:
        text_hash = hashlib.sha256(candidate.text.encode("utf-8")).hexdigest()
        async with context.session() as session:
            unit, created = await upsert_evidence_unit(
                session,
                work_id=work.id,
                # Private full text may only derive project-scoped evidence.
                # A content/text hash is intentionally not treated as an ACL.
                project_id=context.project_id if private_source else None,
                kind=_evidence_kind(
                    candidate.text,
                    work_type=work.work_type,
                ),
                grade=candidate.grade,
                text=candidate.text,
                text_hash=text_hash,
                page=candidate.page,
                section_path=candidate.section_path,
                paragraph_index=candidate.paragraph_index,
                object_ref=candidate.object_ref,
                source_document_file_id=document.id if document else None,
                extraction_model="deterministic_evidence_v1",
                source_hash=source_hash,
                task_id=infer_task_id(candidate.text, tasks),
                topical_status=ontology_topical_status(candidate.text, tasks),
                anchor_strength=candidate.anchor_strength,
                locator_display=_locator_display(candidate),
            )
            unit_ids_by_text[candidate.text] = unit.id
            for measurement in _measurement_candidates(
                candidate.text,
                metric_pattern=metric_pattern,
                metric_name_pattern=metric_name_pattern,
                dataset_pattern=dataset_pattern,
            ):
                await upsert_evidence_measurement(
                    session,
                    evidence_unit_id=unit.id,
                    metric_name=measurement.metric_name,
                    value=measurement.value,
                    unit=measurement.unit,
                    dataset=measurement.dataset,
                    task=measurement.task,
                    sample_size=measurement.sample_size,
                    split=measurement.split,
                    extraction_source="regex",
                )
                outcome.measurements += 1
        outcome.units += 1
        outcome.created += int(created)
        outcome.reused += int(not created)
        outcome.grades[candidate.grade] = outcome.grades.get(candidate.grade, 0) + 1
        if candidate.grade in {
            "A_located_structured",
            "B_located_prose",
            "C_fulltext_unlocated",
        }:
            outcome.usable_units += 1
    outcome.usable_works = int(outcome.usable_units > 0)
    if document is None or not fulltext:
        return outcome

    extraction: ExperimentExtraction | None = None
    settings = context.settings
    if settings.experiment_extraction_enabled:
        if llm_budget_remaining <= 0:
            outcome.llm_budget_skipped = 1
        else:
            extraction = await _llm_structured_extraction(
                context,
                document=document,
                fulltext=fulltext,
                anchors=candidates,
            )
            if extraction is not None:
                outcome.llm_extraction_works = 1
                outcome.llm_cells_accepted = extraction.accepted_count
                outcome.llm_cells_locator_verified = extraction.verified_count
                outcome.llm_cells_rejected = len(extraction.rejected)
                if extraction.rejected and not extraction.cells:
                    context.warn(
                        "evidence.llm_extraction_rejected",
                        "all_cells_rejected",
                        {
                            "work_id": str(work.id),
                            "reasons": extraction.to_payload()["rejection_reasons"],
                        },
                    )

    conflicts = await _persist_structured_experiment_data(
        context,
        work_id=work.id,
        document=document,
        fulltext=fulltext,
        candidates=candidates[:MAX_EVIDENCE_UNITS_PER_WORK],
        evidence_unit_ids=unit_ids_by_text,
        tasks=tasks,
        metric_pattern=metric_pattern,
        metric_name_pattern=metric_name_pattern,
        dataset_pattern=dataset_pattern,
        llm_extraction=extraction,
    )
    outcome.llm_value_conflicts = len(conflicts)
    if conflicts:
        await context.emit(
            "evidence.extraction_conflict",
            {"work_id": str(work.id), "conflicts": conflicts[:5], "total": len(conflicts)},
            stage="evidence",
        )

    # ``on`` 档才让模型读出的维度进入 EvidenceMeasurement——那是 comparability_key
    # 的输入，也就是唯一会改变 SYNTH 与正文的地方。shadow 档到此为止。
    if extraction is not None and settings.experiment_extraction_authoritative:
        outcome.measurements += await _apply_llm_measurements(
            context,
            extraction=extraction,
            candidates=candidates[:MAX_EVIDENCE_UNITS_PER_WORK],
            evidence_unit_ids=unit_ids_by_text,
        )
    return outcome


async def _llm_structured_extraction(
    context: JobContext,
    *,
    document: DocumentFile,
    fulltext: str,
    anchors: list[EvidenceCandidate] | None = None,
) -> ExperimentExtraction | None:
    """跑一次模型抽取；核验面取自本文档的 chunk 行、解析元数据与已有证据锚点。"""
    async with context.session() as session:
        parsed = await session.scalar(
            select(DocumentParse)
            .where(DocumentParse.document_file_id == document.id)
            .order_by(DocumentParse.created_at.desc())
            .limit(1)
        )
        chunks = (
            list(
                (
                    await session.scalars(
                        select(DocumentChunk).where(DocumentChunk.document_parse_id == parsed.id)
                    )
                ).all()
            )
            if parsed is not None
            else []
        )
        structured_objects = (
            list((parsed.metadata_json or {}).get("structured_objects") or []) if parsed else []
        )
    locators = build_locator_index(
        chunks=chunks,
        structured_objects=structured_objects,
        anchors=anchors,
    )
    return await extract_experiment_results(
        fulltext=fulltext,
        locators=locators,
        runner=context.llm_runner(),
        max_chars=int(context.settings.experiment_extraction_max_chars),
        parse_locator=_parse_locator,
        normalize_metric=_normalize_metric,
    )


async def _apply_llm_measurements(
    context: JobContext,
    *,
    extraction: ExperimentExtraction,
    candidates: list[EvidenceCandidate],
    evidence_unit_ids: dict[str, Any],
) -> int:
    """把模型读出的维度写成 EvidenceMeasurement，绑定到最贴近的证据单元。

    只有定位核验过的单元格参与：未核验的格子进不了跨研究比较，写进来只会
    污染 comparability_key。
    """
    if not extraction.cells:
        return 0
    by_location: dict[str, Any] = {}
    for candidate in candidates:
        unit_id = evidence_unit_ids.get(candidate.text)
        if unit_id is not None:
            by_location.setdefault(_source_location(candidate), unit_id)

    written = 0
    async with context.session() as session:
        for cell in extraction.cells:
            if not cell.locator_verified:
                continue
            unit_id = by_location.get(cell.source_location) or _unit_for_span(
                cell.verbatim_span,
                candidates=candidates,
                evidence_unit_ids=evidence_unit_ids,
            )
            if unit_id is None:
                continue
            await upsert_evidence_measurement(
                session,
                evidence_unit_id=unit_id,
                metric_name=cell.metric_name,
                value=cell.value,
                unit=cell.unit,
                dataset=cell.dataset,
                task=extraction.task,
                model_family=cell.model_family,
                sample_size=cell.sample_size,
                split=cell.split,
                ci_low=cell.ci_low,
                ci_high=cell.ci_high,
                std=cell.std,
                protocol=extraction.protocol or None,
                extraction_source="llm",
                locator_verified=True,
            )
            written += 1
    return written


@dataclass
class WorkDimensionExtraction:
    """一篇论文的维度回填素材：抽取结果 + 写回所需的绑定信息。

    抽取与写入分成两步，是为了让调用方能先看**整批**的定位核验率再决定写不写
    （Phase 2 的推广门槛是 ≥80%），同时只付一次调用的钱。
    """

    work_id: Any
    skipped: str | None = None
    extraction: ExperimentExtraction | None = None
    candidates: list[EvidenceCandidate] = field(default_factory=list)
    evidence_unit_ids: dict[str, Any] = field(default_factory=dict)

    @property
    def cells(self) -> int:
        return len(self.extraction.cells) if self.extraction else 0

    @property
    def locator_verified(self) -> int:
        if self.extraction is None:
            return 0
        return sum(1 for cell in self.extraction.cells if cell.locator_verified)

    @property
    def rejected(self) -> int:
        return len(self.extraction.rejected) if self.extraction else 0


async def extract_work_dimensions(
    context: JobContext,
    *,
    work_id: Any,
) -> WorkDimensionExtraction:
    """对**已经存在**的证据单元补跑一次结构化抽取，不写库。

    正常流程里抽取发生在 EVIDENCE 阶段，用的是那一轮在内存里的候选。历史项目的
    证据是在 ``EXPERIMENT_EXTRACTION_MODE=off`` 时抽的，维度全空，于是
    ``comparability_key`` 退化成按证据单元加盐的唯一值，跨研究比较从来没成立过。
    这里从已落库的单元重建候选，走同一条抽取路径，避免为了补维度而重跑整条流水线。
    """
    sources = await load_persisted_fulltext_sources(context, [work_id])
    source = sources.get(str(work_id))
    if source is None or not (source.text or "").strip():
        return WorkDimensionExtraction(work_id=work_id, skipped="no_fulltext")

    async with context.session() as session:
        units = list(
            (
                await session.scalars(
                    select(EvidenceUnit)
                    .where(EvidenceUnit.work_id == work_id)
                    .where(
                        or_(
                            EvidenceUnit.project_id.is_(None),
                            EvidenceUnit.project_id == context.project_id,
                        )
                    )
                )
            ).all()
        )
        document = await session.get(DocumentFile, source.document_file_id)
    if not units or document is None:
        return WorkDimensionExtraction(work_id=work_id, skipped="no_evidence_units")

    candidates = [
        EvidenceCandidate(
            text=unit.text,
            page=unit.page,
            section_path=unit.section_path,
            paragraph_index=unit.paragraph_index,
            object_ref=unit.object_ref,
            grade=unit.grade,
        )
        for unit in units
    ]
    extraction = await _llm_structured_extraction(
        context,
        document=document,
        fulltext=source.text,
        anchors=candidates,
    )
    if extraction is None:
        return WorkDimensionExtraction(work_id=work_id, skipped="extraction_unavailable")
    return WorkDimensionExtraction(
        work_id=work_id,
        extraction=extraction,
        candidates=candidates,
        evidence_unit_ids={unit.text: unit.id for unit in units},
    )


async def apply_work_dimensions(
    context: JobContext,
    extracted: WorkDimensionExtraction,
) -> int:
    """把抽出的维度写进 EvidenceMeasurement（走生产同一条写入路径）。"""
    if extracted.extraction is None:
        return 0
    return await _apply_llm_measurements(
        context,
        extraction=extracted.extraction,
        candidates=extracted.candidates,
        evidence_unit_ids=extracted.evidence_unit_ids,
    )


def _unit_for_span(
    span: str,
    *,
    candidates: list[EvidenceCandidate],
    evidence_unit_ids: dict[str, Any],
) -> Any | None:
    """定位对不上时的兜底：找一条正文包含该引文片段的证据单元。"""
    needle = " ".join((span or "").split())
    if len(needle) < 12:
        return None
    for candidate in candidates:
        haystack = " ".join(candidate.text.split())
        if needle in haystack or haystack in needle:
            return evidence_unit_ids.get(candidate.text)
    return None


STRUCTURED_EXTRACTION_SCHEMA = "experiment_v2"


async def _persist_structured_experiment_data(
    context: JobContext,
    *,
    work_id: Any,
    document: DocumentFile,
    fulltext: str,
    candidates: list[EvidenceCandidate],
    evidence_unit_ids: dict[str, Any],
    tasks: list[TaskSpec],
    metric_pattern: re.Pattern[str],
    metric_name_pattern: re.Pattern[str] | None,
    dataset_pattern: re.Pattern[str] | None,
    llm_extraction: ExperimentExtraction | None = None,
) -> list[dict[str, Any]]:
    """Persist a conservative, source-addressable experiment schema.

    The deterministic pass intentionally leaves unknown fields empty instead of
    inventing protocol values.  Every emitted numerical cell keeps a page /
    section / object source location, so the evidence matrix can distinguish
    comparable results from merely nearby numbers.

    ``extraction`` 非空时，额外写一份 ``experiment_v3_llm``：正则与模型对账之后的
    结果，带来源与定位核验标记。**v2 那一份保持逐字不变**——这样 off 档下这个函数
    的行为与引入 Phase 2 之前完全一致，是"零行为变化"最容易验证的形式。

    返回硬数值冲突列表（同一定位两条路径给出不同数字），供调用方发事件。
    """
    async with context.session() as session:
        parsed = await session.scalar(
            select(DocumentParse)
            .where(DocumentParse.document_file_id == document.id)
            .order_by(DocumentParse.created_at.desc())
            .limit(1)
        )
        structured_objects = (
            list((parsed.metadata_json or {}).get("structured_objects") or []) if parsed else []
        )
        source_hash = hashlib.sha256(fulltext.encode("utf-8")).hexdigest()[:40]
        extraction = await session.scalar(
            select(StructuredExtraction).where(
                StructuredExtraction.work_id == work_id,
                StructuredExtraction.document_file_id == document.id,
                StructuredExtraction.schema_version == STRUCTURED_EXTRACTION_SCHEMA,
            )
        )
        if extraction is None:
            extraction = StructuredExtraction(
                work_id=work_id,
                document_file_id=document.id,
                schema_version=STRUCTURED_EXTRACTION_SCHEMA,
                status="parsed",
            )
            session.add(extraction)
            await session.flush()

        records: list[dict[str, Any]] = []
        metrics: set[str] = set()
        datasets: set[str] = set()
        for candidate in candidates:
            source_location = _source_location(candidate)
            for measurement in _measurement_candidates(
                candidate.text,
                metric_pattern=metric_pattern,
                metric_name_pattern=metric_name_pattern,
                dataset_pattern=dataset_pattern,
            ):
                metrics.add(measurement.metric_name)
                if measurement.dataset:
                    datasets.add(measurement.dataset)
                records.append(
                    {
                        "metric_name": measurement.metric_name,
                        "value": measurement.value,
                        "unit": measurement.unit,
                        "dataset": measurement.dataset,
                        "source_location": source_location,
                        "evidence_unit_id": evidence_unit_ids.get(candidate.text),
                        "anchor_strength": candidate.anchor_strength,
                    }
                )

        payload = _structured_payload(
            fulltext=fulltext,
            structured_objects=structured_objects,
            datasets=sorted(datasets),
            metrics=sorted(metrics),
            records=_json_safe(records),
            tasks=tasks,
            dataset_pattern=dataset_pattern,
        )
        extraction.status = "parsed"
        extraction.payload_json = payload
        extraction.source_hash = source_hash
        extraction.extraction_model = "deterministic_experiment_v2"
        extraction.validation_json = {
            "required_keys": sorted(payload),
            "source_locations_complete": all(bool(record["source_location"]) for record in records),
            "numeric_records": len(records),
        }
        existing = (
            await session.scalars(
                select(ExperimentResult).where(
                    ExperimentResult.structured_extraction_id == extraction.id
                )
            )
        ).all()
        for item in existing:
            await session.delete(item)
        task = payload["task_id"] or None
        for record in records:
            session.add(
                ExperimentResult(
                    structured_extraction_id=extraction.id,
                    evidence_unit_id=record["evidence_unit_id"],
                    task_id=task,
                    task_variant=payload["task_variant"],
                    task=task,
                    dataset=record["dataset"],
                    dataset_version=payload["dataset_version"],
                    split_strategy=payload["split_strategy"],
                    model_family=(payload["model_families"] or [None])[0],
                    input_representation=(payload["input_representations"] or [None])[0],
                    pretrained_backbone=payload["pretrained_backbone"],
                    loss_function=payload["loss_function"],
                    optimizer=payload["optimizer"],
                    learning_rate=payload["learning_rate"],
                    batch_size=payload["batch_size"],
                    epochs=payload["epochs"],
                    regularization=payload["regularization"],
                    metric_name=record["metric_name"],
                    value=record["value"],
                    unit=record["unit"],
                    comparability_key=comparability_key(
                        task=task,
                        dataset=record["dataset"],
                        metric_name=record["metric_name"],
                        split=None,
                        unknown_salt=str(record["evidence_unit_id"] or ""),
                    ),
                    source_location=record["source_location"],
                    anchor_strength=record["anchor_strength"],
                    extraction_source="regex",
                    locator_verified=record["source_location"] != "fulltext:unlocated",
                )
            )

    if llm_extraction is None:
        return []
    return await _persist_reconciled_experiment_data(
        context,
        work_id=work_id,
        document=document,
        fulltext=fulltext,
        regex_records=records,
        extraction=llm_extraction,
    )


LLM_STRUCTURED_EXTRACTION_SCHEMA = "experiment_v3_llm"


async def _persist_reconciled_experiment_data(
    context: JobContext,
    *,
    work_id: Any,
    document: DocumentFile,
    fulltext: str,
    regex_records: list[dict[str, Any]],
    extraction: ExperimentExtraction,
) -> list[dict[str, Any]]:
    """写 experiment_v3_llm：正则 × 模型对账后的结果表。"""
    regex_cells = tuple(
        ExtractedResultCell(
            metric_name=str(record["metric_name"]),
            value=float(record["value"]),
            unit=record["unit"],
            dataset=record["dataset"],
            source_location=str(record["source_location"]),
            locator_verified=record["source_location"] != "fulltext:unlocated",
        )
        for record in regex_records
    )
    merged, conflicts = reconcile(llm_cells=extraction.cells, regex_cells=regex_cells)
    unit_by_location = {
        str(record["source_location"]): record["evidence_unit_id"] for record in regex_records
    }

    async with context.session() as session:
        row = await session.scalar(
            select(StructuredExtraction).where(
                StructuredExtraction.work_id == work_id,
                StructuredExtraction.document_file_id == document.id,
                StructuredExtraction.schema_version == LLM_STRUCTURED_EXTRACTION_SCHEMA,
            )
        )
        if row is None:
            row = StructuredExtraction(
                work_id=work_id,
                document_file_id=document.id,
                schema_version=LLM_STRUCTURED_EXTRACTION_SCHEMA,
                status="parsed",
            )
            session.add(row)
            await session.flush()
        row.status = "parsed"
        row.source_hash = hashlib.sha256(fulltext.encode("utf-8")).hexdigest()[:40]
        row.extraction_model = extraction.model or "llm_experiment_v3"
        row.payload_json = _json_safe(
            {
                "schema": LLM_STRUCTURED_EXTRACTION_SCHEMA,
                "task": extraction.task,
                "task_variant": extraction.task_variant,
                "protocol": extraction.protocol,
                "results": [
                    {
                        "metric_name": item.cell.metric_name,
                        "value": item.cell.value,
                        "unit": item.cell.unit,
                        "dataset": item.cell.dataset,
                        "split": item.cell.split,
                        "model_family": item.cell.model_family,
                        "source_location": item.cell.source_location,
                        "locator_verified": item.cell.locator_verified,
                        "extraction_source": item.extraction_source,
                        "conflict": item.conflict,
                    }
                    for item in merged
                ],
            }
        )
        row.validation_json = {
            **extraction.to_payload(),
            "merged_cells": len(merged),
            "value_conflicts": len(conflicts),
            "conflicts": conflicts[:20],
        }
        for stale in (
            await session.scalars(
                select(ExperimentResult).where(
                    ExperimentResult.structured_extraction_id == row.id
                )
            )
        ).all():
            await session.delete(stale)
        for item in merged:
            cell = item.cell
            session.add(
                ExperimentResult(
                    structured_extraction_id=row.id,
                    evidence_unit_id=unit_by_location.get(cell.source_location),
                    task_id=None,
                    task=extraction.task,
                    task_variant=extraction.task_variant,
                    dataset=cell.dataset,
                    split_strategy=cell.split or extraction.protocol.get("split_strategy"),
                    model_family=cell.model_family,
                    loss_function=extraction.protocol.get("loss_function"),
                    optimizer=extraction.protocol.get("optimizer"),
                    learning_rate=extraction.protocol.get("learning_rate"),
                    batch_size=extraction.protocol.get("batch_size"),
                    epochs=extraction.protocol.get("epochs"),
                    metric_name=cell.metric_name,
                    value=cell.value,
                    unit=cell.unit,
                    ci_low=cell.ci_low,
                    ci_high=cell.ci_high,
                    std=cell.std,
                    baseline_name=cell.baseline_name,
                    baseline_value=cell.baseline_value,
                    # 维度齐全时 comparability_key 才稳定；缺任一维度仍按未知加盐，
                    # 语义与正则通道一致，只是现在维度真的填得上了。
                    comparability_key=comparability_key(
                        task=extraction.task,
                        dataset=cell.dataset,
                        metric_name=cell.metric_name,
                        split=cell.split,
                        unknown_salt=cell.source_location,
                    ),
                    source_location=cell.source_location,
                    anchor_strength=None,
                    extraction_source=item.extraction_source,
                    locator_verified=cell.locator_verified,
                    extraction_conflict_json=item.conflict,
                )
            )
    return conflicts


def _structured_payload(
    *,
    fulltext: str,
    structured_objects: list[dict[str, Any]],
    datasets: list[str],
    metrics: list[str],
    records: list[dict[str, Any]],
    tasks: list[TaskSpec] | None = None,
    dataset_pattern: re.Pattern[str] | None = None,
) -> dict[str, Any]:
    tasks = list(tasks or [])
    # R16：模型族 / 输入表征 / 预训练骨干 / 划分策略 / 任务变体全部来自 task_definition。
    # 词表为空时这些字段留空——对领域外论文，留空严格优于填错。
    vocabulary = vocabulary_for_tasks(tasks)

    object_payload = [
        {
            "object_ref": item.get("object_ref"),
            "page": item.get("page_number"),
            "section": item.get("section_title") or item.get("heading"),
            "caption": str(item.get("caption") or "")[:800] or None,
        }
        for item in structured_objects
        if isinstance(item, dict)
    ]
    limitations = [
        sentence
        for sentence in _sentences(fulltext)
        if re.search(r"\b(?:limitation|limited|future work|constraint)\b|局限|限制", sentence, re.I)
    ][:12]
    ontology_datasets = datasets_for_tasks(tasks)
    fallback_datasets: list[str] = []
    if dataset_pattern is not None:
        fallback_datasets = list(dict.fromkeys(dataset_pattern.findall(fulltext)))[:12]
    elif ontology_datasets:
        # Last-resort: literal membership scan without baking domain names into code.
        lower = fulltext.casefold()
        fallback_datasets = [name for name in ontology_datasets if name.casefold() in lower][:12]
    return {
        "schema": "experiment_v2",
        "task_id": infer_task_id(fulltext, tasks),
        "task_variant": _cue_lookup(fulltext, vocabulary.task_variants),
        "datasets": datasets or fallback_datasets,
        "dataset_version": _dataset_version(fulltext, ontology_datasets),
        "split_strategy": _cue_lookup(fulltext, vocabulary.split_strategies),
        "model_families": _vocabulary_matches(fulltext, vocabulary.model_families),
        "input_representations": _vocabulary_matches(fulltext, vocabulary.input_representations),
        "pretrained_backbone": _first_vocabulary_match(
            fulltext,
            vocabulary.pretrained_backbones,
            # 骨干常带版本后缀（ESM2-650M）；原实现的 `ESM2[- ]?\d+[A-Z]?` 就是为此。
            # 词表里存裸名，后缀在这里补，手写 JSON 因此不必写正则。
            version_suffix=True,
        ),
        # 下面三条是跨学科通用的训练超参，不属于领域词表，保持在代码里。
        # 唯一的例外是 BPR（推荐领域的损失），见 Phase 3 报告的残留项。
        "loss_function": _first_match(
            fulltext, r"\b(?:cross[- ]entropy|focal loss|contrastive loss|mean squared error|BPR)\b"
        ),
        "optimizer": _first_match(fulltext, r"\b(?:AdamW?|SGD|RMSprop)\b"),
        "learning_rate": _numeric_hyperparameter(
            fulltext,
            r"(?:learning rate|\blr)\s*(?:=|:|of)?\s*",
        ),
        "batch_size": _integer_hyperparameter(fulltext, r"batch size\s*(?:=|:|of)?\s*"),
        "epochs": _epoch_count(fulltext),
        "regularization": _first_match(fulltext, r"\b(?:dropout|weight decay|early stopping)\b"),
        # `baselines` 曾是 `[A-Z][A-Za-z0-9+-]{2,24}`——它匹配任何首字母大写的词，
        # 于是把 The / We / Table / 作者姓氏当成"基线方法"存进 payload。
        # 空列表不如真实基线有用，但比结构化噪声强；真正的基线由 experiment_v3_llm
        # 的 baseline_name 提供。
        "metrics": metrics,
        "main_results": records,
        "ablation_results": [
            record
            for record in records
            if "ablation" in str(record.get("source_location", "")).casefold()
        ],
        "equations": [
            item
            for item in object_payload
            if str(item.get("object_ref")).startswith(("eq:", "equation:"))
        ],
        "algorithms": [
            item
            for item in object_payload
            if str(item.get("object_ref")).startswith(("algo:", "algorithm:"))
        ],
        "limitations": limitations,
        "source_locations": object_payload
        + [{"source_location": record["source_location"]} for record in records],
    }


def _json_safe(value: Any) -> Any:
    """Convert values destined for JSONB without changing relational values.

    Evidence identifiers remain UUIDs for ``ExperimentResult`` inserts.  Only
    the denormalized structured-extraction payload needs this conversion.
    """
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _source_location(candidate: EvidenceCandidate) -> str:
    parts: list[str] = []
    if candidate.object_ref:
        parts.append(candidate.object_ref)
    if candidate.page is not None:
        parts.append(f"p.{candidate.page}")
    if candidate.section_path:
        parts.append(candidate.section_path[:300])
    if candidate.paragraph_index is not None:
        parts.append(f"paragraph {candidate.paragraph_index}")
    return ", ".join(parts) or "fulltext:unlocated"


def _locator_display(candidate: EvidenceCandidate) -> str | None:
    parts = [f"p.{candidate.page}" if candidate.page else "", candidate.object_ref or ""]
    return ", ".join(part for part in parts if part) or None


def _cue_lookup(text: str, pairs: tuple[tuple[str, str], ...]) -> str | None:
    """先命中先赢的提示词 → 规范名查找（替换原来的 if 链）。

    保留原实现的子串匹配语义（不是词边界）：``"multiclass"`` 要能在
    ``"multiclass classification"`` 里命中。列表顺序即优先级，由 task_definition 决定。
    """
    lower = (text or "").casefold()
    for cue, name in pairs:
        if cue.casefold() in lower:
            return name
    return None


def _vocabulary_pattern(terms: tuple[str, ...], *, version_suffix: bool = False) -> str | None:
    """把词表编成带词边界的交替式，长词优先以免短名吃掉长名。"""
    cleaned = [term.strip() for term in terms if term and term.strip()]
    if not cleaned:
        return None
    alternatives = "|".join(
        re.escape(term) for term in sorted(set(cleaned), key=len, reverse=True)
    )
    tail = r"(?:[- ]?\d+[A-Z]?)?" if version_suffix else ""
    return rf"\b(?:{alternatives}){tail}\b"


def _vocabulary_matches(text: str, terms: tuple[str, ...], *, limit: int = 12) -> list[str]:
    """词表里出现在正文中的词，按**正文顺序**去重（与原 findall 语义一致）。"""
    pattern = _vocabulary_pattern(terms)
    if pattern is None:
        return []
    return list(dict.fromkeys(re.findall(pattern, text, re.IGNORECASE)))[:limit]


def _first_vocabulary_match(
    text: str,
    terms: tuple[str, ...],
    *,
    version_suffix: bool = False,
) -> str | None:
    pattern = _vocabulary_pattern(terms, version_suffix=version_suffix)
    if pattern is None:
        return None
    return _first_match(text, pattern)


def _dataset_version(text: str, datasets: list[str]) -> str | None:
    for name in datasets:
        match = re.search(rf"\b{re.escape(name)}\s*(\d+(?:\.\d+)?)\b", text, re.IGNORECASE)
        if match:
            return match.group(1)
    match = re.search(r"\b(?:v(?:ersion)?\s*)(\d+(?:\.\d+)?)\b", text, re.IGNORECASE)
    return match.group(1) if match else None


def _first_match(text: str, pattern: str) -> str | None:
    match = re.search(pattern, text, re.IGNORECASE)
    return match.group(0) if match else None


def _numeric_hyperparameter(text: str, pattern: str) -> float | None:
    match = re.search(pattern + r"([0-9]+(?:\.[0-9]+)?(?:e-?\d+)?)", text, re.IGNORECASE)
    return float(match.group(1)) if match else None


def _integer_hyperparameter(text: str, pattern: str) -> int | None:
    match = re.search(pattern + r"(\d+)", text, re.IGNORECASE)
    return int(match.group(1)) if match else None


def _epoch_count(text: str) -> int | None:
    match = re.search(r"(?:epochs?\s*(?:=|:|of)?\s*(\d+)|(\d+)\s+epochs?)", text, re.IGNORECASE)
    if not match:
        return None
    return int(match.group(1) or match.group(2))


def _evidence_candidates(
    *,
    fulltext: str | None,
    abstract: str | None,
    quotable_points: list[Any],
    fulltext_used: bool,
) -> list[EvidenceCandidate]:
    candidates: list[EvidenceCandidate] = []
    if fulltext_used:
        for point in quotable_points:
            if not isinstance(point, dict):
                continue
            text = _clean_text(point.get("text") or point.get("excerpt"))
            if not text:
                continue
            page = point.get("page") if isinstance(point.get("page"), int) else None
            section = _clean_text(point.get("section")) or None
            paragraph = point.get("paragraph") if isinstance(point.get("paragraph"), int) else None
            structured_object_ref = str(point.get("object_ref") or "").strip()[:64] or None
            object_ref = structured_object_ref or _object_reference(text)
            if structured_object_ref:
                strength = "structured_cell"
            elif object_ref:
                strength = "object_mention"
            else:
                strength = "prose_only"
            candidates.append(
                EvidenceCandidate(
                    text=text[:MAX_EVIDENCE_TEXT_CHARS],
                    page=page,
                    section_path=section,
                    paragraph_index=paragraph,
                    object_ref=object_ref,
                    grade=_fulltext_grade(page, section, paragraph, object_ref, strength),
                    anchor_strength=strength,
                )
            )
        candidates.extend(_located_fulltext_candidates(fulltext or ""))
    else:
        # D 级明确落库，后续写作与质量门才能“物理”区分摘要和全文证据。
        for index, sentence in enumerate(_sentences(abstract or ""), start=1):
            candidates.append(
                EvidenceCandidate(
                    text=sentence[:MAX_EVIDENCE_TEXT_CHARS],
                    section_path="abstract",
                    paragraph_index=index,
                    grade="D_abstract_only",
                )
            )
    return _deduplicate(candidates)


def _located_fulltext_candidates(fulltext: str) -> list[EvidenceCandidate]:
    matches = list(_MARKER_RE.finditer(fulltext))
    candidates: list[EvidenceCandidate] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(fulltext)
        locator = _parse_locator(match.group("locator"))
        block = fulltext[match.end() : end].strip()
        if not block or not classify_content_role(block).claim_eligible:
            continue
        passages = [part.strip() for part in re.split(r"\n{2,}", block) if part.strip()]
        if not passages:
            passages = [block]
        for paragraph_index, passage in enumerate(passages, start=1):
            if not _RESULT_CUES.search(passage) and not re.search(r"\d", passage):
                continue
            text = passage[:MAX_EVIDENCE_TEXT_CHARS]
            structured_object_ref = locator.get("object_ref")
            object_ref = structured_object_ref or _object_reference(text)
            if structured_object_ref:
                strength = "structured_cell"
            elif object_ref:
                strength = "object_mention"
            else:
                strength = "prose_only"
            candidates.append(
                EvidenceCandidate(
                    text=text,
                    page=locator.get("page"),
                    section_path=locator.get("section"),
                    paragraph_index=paragraph_index,
                    object_ref=object_ref,
                    grade=_fulltext_grade(
                        locator.get("page"),
                        locator.get("section"),
                        paragraph_index,
                        structured_object_ref,
                        strength,
                    ),
                    anchor_strength=strength,
                )
            )
    return candidates


def _parse_locator(value: str) -> dict[str, Any]:
    locator: dict[str, Any] = {}
    for part in value.split("|"):
        key, separator, raw_value = part.strip().partition("=")
        if not separator:
            continue
        normalized_key = key.strip().upper()
        cleaned = raw_value.strip()
        if normalized_key == "PAGE" and cleaned.isdigit():
            locator["page"] = int(cleaned)
        elif normalized_key == "SECTION":
            locator["section"] = cleaned[:300]
        elif normalized_key in {"TABLE", "FIG", "EQ", "ALGO"}:
            locator["object_ref"] = f"{normalized_key.casefold()}:{cleaned}"[:64]
    return locator


def _measurement_candidates(
    text: str,
    *,
    metric_pattern: re.Pattern[str] | None = None,
    metric_name_pattern: re.Pattern[str] | None = None,
    dataset_pattern: re.Pattern[str] | None = None,
) -> list[MeasurementCandidate]:
    """从散文或 Markdown 表格中抽取保守的显式指标值，不推断缺失维度。

    散文与表头用同一份指标词表，只是编成两条正则：散文那条要匹配「指标 = 数值」，
    表头那条只认名字。两者都由 ``compile_metric_*_pattern`` 从本体生成。
    """
    candidates: list[MeasurementCandidate] = []
    pattern = metric_pattern or _METRIC_RE
    header_pattern = metric_name_pattern or _METRIC_NAME_RE
    dataset = None
    if dataset_pattern is not None:
        dataset_match = dataset_pattern.search(text)
        dataset = dataset_match.group(0) if dataset_match else None
    if dataset is None:
        dataset_match = _DATASET_RE.search(text)
        dataset = dataset_match.group("name") if dataset_match else None
    sample_match = _SAMPLE_RE.search(text)
    sample_size = int(sample_match.group("value").replace(",", "")) if sample_match else None
    for match in pattern.finditer(text):
        candidates.append(
            MeasurementCandidate(
                metric_name=_normalize_metric(match.group("metric")),
                value=float(match.group("value")),
                unit=_normalize_unit(match.group("unit")),
                dataset=dataset,
                sample_size=sample_size,
            )
        )

    table = parse_markdown_table(text)
    if table is not None:
        by_row: dict[int, dict[str, str]] = {}
        for cell in table.cells:
            by_row.setdefault(cell.row_index, {})[cell.column_name] = cell.value
        for row in by_row.values():
            row_dataset = next(
                (
                    value
                    for column, value in row.items()
                    if column.casefold() in {"dataset", "data", "数据集"}
                ),
                dataset,
            )
            for column, raw_value in row.items():
                metric = _metric_from_header(column, header_pattern)
                numeric = _parse_numeric_value(raw_value)
                if metric is None or numeric is None:
                    continue
                value, unit = numeric
                candidates.append(
                    MeasurementCandidate(
                        metric_name=metric,
                        value=value,
                        unit=unit,
                        dataset=row_dataset,
                        sample_size=sample_size,
                    )
                )
    unique: list[MeasurementCandidate] = []
    seen: set[tuple[str, float, str | None, str | None]] = set()
    for candidate in candidates:
        key = (
            candidate.metric_name,
            candidate.value,
            candidate.unit,
            candidate.dataset,
        )
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def _fulltext_grade(
    page: int | None,
    section: str | None,
    paragraph: int | None,
    object_ref: str | None,
    anchor_strength: str = "prose_only",
) -> str:
    # N4 / M1-9: A-grade only for true structured cells, never prose mentions.
    if anchor_strength == "structured_cell" and object_ref and (page or section):
        return "A_located_structured"
    if page or section or paragraph:
        return "B_located_prose"
    return "C_fulltext_unlocated"


def _metric_from_header(value: str, pattern: re.Pattern[str] | None = None) -> str | None:
    """表头单元格 → 规范指标名。词表与散文抽取共用一条交替式。"""
    cleaned = " ".join(value.split()).casefold()
    match = (pattern or _METRIC_NAME_RE).search(cleaned)
    return _normalize_metric(match.group()) if match else None


def _normalize_metric(value: str) -> str:
    compact = re.sub(r"\s+", "", value).replace("-score", "")
    upper = compact.upper()
    return {
        "ACCURACY": "accuracy",
        "PRECISION": "precision",
        "RECALL": "recall",
        "HITRATE": "HR",
        "FMEASURE": "F1",
        "MACROF1": "macro-F1",
        "TOP-KACCURACY": "top-k accuracy",
    }.get(upper, upper)


def _normalize_unit(value: str | None) -> str | None:
    if not value:
        return None
    percent_units = {"%", "percent", "percentage point", "percentage points"}
    return "%" if value.casefold() in percent_units else value


def _parse_numeric_value(value: str) -> tuple[float, str | None] | None:
    match = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*(%|percent)?\s*", value, re.IGNORECASE)
    if not match:
        return None
    return float(match.group(1)), _normalize_unit(match.group(2))


def _evidence_kind(text: str, *, work_type: str | None) -> str:
    lower = text.casefold()
    if work_type and any(token in work_type.casefold() for token in ("review", "survey")):
        return "review_restatement"
    if re.search(
        r"\b(?:theorem|lemma|proof|derive|derivation|equation)\b|(?:定理|证明|推导)",
        lower,
    ):
        return "theoretical_derivation"
    if re.search(r"\b(?:infer|predict|estimate|model suggests?)\b|(?:推断|预测|估计)", lower):
        return "model_inference"
    if re.search(r"\d|\b(?:experiment|result|dataset|sample|metric|evaluat)\w*\b", lower):
        return "experimental_fact"
    return "author_conclusion"


def _object_reference(text: str) -> str | None:
    match = re.search(
        r"\b(?P<kind>table|tab\.|figure|fig\.|equation|eq\.|algorithm|algo\.)\s*"
        r"(?P<number>[A-Za-z]?\d+(?:[.-]\d+)?)",
        text,
        re.IGNORECASE,
    )
    if not match:
        return None
    kind = match.group("kind").rstrip(".").casefold()
    normalized = {
        "tab": "table",
        "fig": "fig",
        "equation": "eq",
        "algorithm": "algo",
    }.get(kind, kind)
    return f"{normalized}:{match.group('number')}"[:64]


def _sentences(text: str) -> list[str]:
    cleaned = " ".join(text.split())
    if not cleaned:
        return []
    return [
        sentence.strip()
        for sentence in _SENTENCE_SPLIT_RE.split(cleaned)
        if len(sentence.strip()) >= 20
    ][:MAX_EVIDENCE_UNITS_PER_WORK]


def _deduplicate(candidates: list[EvidenceCandidate]) -> list[EvidenceCandidate]:
    unique: list[EvidenceCandidate] = []
    seen: set[str] = set()
    # 卡片原文点排在全文补充块之前；同文本优先保留卡片提供的精细定位。
    for candidate in candidates:
        key = hashlib.sha256(_clean_text(candidate.text).casefold().encode("utf-8")).hexdigest()
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


def _clean_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())


__all__ = [
    "EvidenceCandidate",
    "EvidenceOutcome",
    "WorkDimensionExtraction",
    "apply_work_dimensions",
    "extract_evidence_units",
    "extract_work_dimensions",
]
