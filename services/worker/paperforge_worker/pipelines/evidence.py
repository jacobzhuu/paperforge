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
    DocumentFile,
    DocumentParse,
    ExperimentResult,
    StructuredExtraction,
)
from db.repositories.tasks import (
    TaskSpec,
    compile_dataset_pattern,
    compile_metric_pattern,
    datasets_for_tasks,
    infer_task_id,
    metrics_for_tasks,
)
from db.repositories.tasks import (
    topical_status as ontology_topical_status,
)
from ingest import classify_content_role, parse_markdown_table
from sqlalchemy import select

from paperforge_worker.context import JobContext
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
_METRIC_RE = re.compile(
    r"(?P<metric>auroc|auprc|auc|accuracy|precision|recall|macro[- ]?f1|"
    r"f[- ]?measure|f1(?:-score)?|mcc|tanimoto|top[- ]?k\s+accuracy|bleu|rouge(?:-[L12])?|"
    r"mrr|map|ndcg@\d+|hr@\d+|hit\s*rate(?:@\d+)?|asr|er@\d+|recall@\d+|ndcg|hr)"
    r"\s*(?:score\s*)?(?:=|:|of|was|is|reached|达到|为)?\s*"
    r"(?P<value>-?\d+(?:\.\d+)?)\s*(?P<unit>%|percent|percentage points?|百分点)?",
    re.IGNORECASE,
)
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
        task_specs = await list_project_task_specs(session, context.project_id)
    metric_pattern = compile_metric_pattern(metrics_for_tasks(task_specs))
    dataset_pattern = compile_dataset_pattern(datasets_for_tasks(task_specs))
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
                dataset_pattern=dataset_pattern,
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
    dataset_pattern: re.Pattern[str] | None,
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
    if document is not None and fulltext:
        await _persist_structured_experiment_data(
            context,
            work_id=work.id,
            document=document,
            fulltext=fulltext,
            candidates=candidates[:MAX_EVIDENCE_UNITS_PER_WORK],
            evidence_unit_ids=unit_ids_by_text,
            tasks=tasks,
            metric_pattern=metric_pattern,
            dataset_pattern=dataset_pattern,
        )
    return outcome


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
    dataset_pattern: re.Pattern[str] | None,
) -> None:
    """Persist a conservative, source-addressable experiment schema.

    The deterministic pass intentionally leaves unknown fields empty instead of
    inventing protocol values.  Every emitted numerical cell keeps a page /
    section / object source location, so the evidence matrix can distinguish
    comparable results from merely nearby numbers.
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
                )
            )


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

    def matches(pattern: str, limit: int = 12) -> list[str]:
        return list(dict.fromkeys(re.findall(pattern, fulltext, re.IGNORECASE)))[:limit]

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
        "task_variant": _task_variant(fulltext),
        "datasets": datasets or fallback_datasets,
        "dataset_version": _dataset_version(fulltext, ontology_datasets),
        "split_strategy": _split_strategy(fulltext),
        "model_families": matches(
            r"\b(?:CNN|BiLSTM|Transformer|GNN|CRF|BERT|LSTM|GRU|SASRec|BERT4Rec|GRU4Rec)\b"
        ),
        "input_representations": matches(
            r"\b(?:nucleotide|amino acid|Pfam domain|domain graph|item sequence|user sequence)\b"
        ),
        "pretrained_backbone": _first_match(
            fulltext, r"\b(?:ESM2[- ]?\d+[A-Z]?|ProtBERT|DNABERT|BERT|RoBERTa)\b"
        ),
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
        "baselines": matches(r"\b(?:[A-Z][A-Za-z0-9+-]{2,24})\b")[:8],
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


def _task_variant(text: str) -> str | None:
    lower = text.casefold()
    if "multi-class" in lower or "multiclass" in lower:
        return "multi-class"
    if "binary" in lower or "detection" in lower:
        return "binary detection"
    if "targeted" in lower:
        return "targeted attack"
    return None


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


def _split_strategy(text: str) -> str | None:
    lower = text.casefold()
    for cue, name in (
        ("leave-one-genome-out", "leave-one-genome-out"),
        ("cluster-based", "cluster-based"),
        ("temporal split", "temporal"),
        ("random split", "random"),
    ):
        if cue in lower:
            return name
    return None


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
    dataset_pattern: re.Pattern[str] | None = None,
) -> list[MeasurementCandidate]:
    """从散文或 Markdown 表格中抽取保守的显式指标值，不推断缺失维度。"""
    candidates: list[MeasurementCandidate] = []
    pattern = metric_pattern or _METRIC_RE
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
                metric = _metric_from_header(column)
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


def _metric_from_header(value: str) -> str | None:
    cleaned = " ".join(value.split()).casefold()
    match = re.search(
        r"auroc|auprc|auc|accuracy|precision|recall|macro[- ]?f1|f[- ]?measure|"
        r"f1(?:-score)?|mcc|tanimoto|top[- ]?k\s+accuracy|bleu|rouge(?:-[l12])?|"
        r"mrr|map|ndcg@\d+|hr@\d+|hit\s*rate(?:@\d+)?",
        cleaned,
    )
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
    "extract_evidence_units",
]
