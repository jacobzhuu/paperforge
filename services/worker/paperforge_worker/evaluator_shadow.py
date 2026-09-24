"""Bounded shadow outbox. No writes to documents, jobs, gates or production LLM ledgers."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from db import (
    WRITE_DOCUMENT_KEY,
    document_snapshot_hash,
    evidence_payload,
    get_outline,
    get_writing_whitelist,
    grounded_asset_payloads,
    list_evidence_units,
    list_research_questions,
    list_sections,
)
from db.models.paper import (
    ClaimEvidenceAnchor,
    EvaluatorShadowRun,
    GenerationJob,
    PaperDocument,
    QualityReportRecord,
)
from observability import get_logger
from sqlalchemy import func, select, text
from storage import make_object_store

from paperforge_worker.context import JobContext
from paperforge_worker.orchestration.writing_graph import fingerprint
from paperforge_worker.pipelines.quality import _body_text as _body_text_for_quality
from paperforge_worker.pipelines.review_inputs import REVIEW_INPUT_VERSION, build_review_input
from paperforge_worker.pipelines.semantic_review import review_section

VERSION = f"shadow-v1:{REVIEW_INPUT_VERSION}"
# One initial bounded cohort, not an unbounded paid experiment.
MAX_SECTIONS = 6
LOCK_ID = 739241810
logger = get_logger(__name__)


def implementation_hash() -> str:
    paths = [Path(__file__)] + [
        Path(__file__).parent / "pipelines" / n
        for n in ("review_inputs.py", "review_contract.py", "semantic_review.py")
    ]
    return fingerprint({p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths})


def dumps(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()


def sample_sections(materials: list[dict]) -> list[dict]:
    """Deterministic risk/coverage sample; keep frame + most numeric + most claims."""
    if len(materials) <= MAX_SECTIONS:
        return materials
    chosen = {}
    for key in ("abstract", "conclusion"):
        for m in materials:
            if m["section_key"] == key:
                chosen[key] = m
    for key_fn in (
        lambda m: len(m["binding_issues"]) + len(m["table_role_conflicts"]),
        lambda m: sum(c.isdigit() for c in m["prose"]),
        lambda m: len(m["claims"]),
    ):
        for m in sorted(materials, key=lambda m: (-key_fn(m), m["section_key"])):
            if m["section_key"] not in chosen:
                chosen[m["section_key"]] = m
                break
    for m in materials:
        if len(chosen) >= MAX_SECTIONS:
            break
        chosen.setdefault(m["section_key"], m)
    return [chosen[k] for k in sorted(chosen)]


async def capture(ctx: dict, source_id: uuid.UUID) -> None:
    """Freeze after terminal delivery, using a repeatable-read snapshot. No model calls."""
    settings, factory = ctx["settings"], ctx["session_factory"]
    async with factory() as session:
        await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        job = await session.get(GenerationJob, source_id)
        if job is None or job.kind not in {"write", "full", "polish", "rebuild", "quality_repair"}:
            return
        if job.status not in {"succeeded", "needs_input"}:
            return
        cp = job.checkpoint_json or {}
        doc_id = cp.get(WRITE_DOCUMENT_KEY)
        if not doc_id:
            stage = cp.get("write") or {}
            doc_id = stage.get("document_id") if isinstance(stage, dict) else None
        if not doc_id:
            return  # Never substitute an unrelated latest document.
        document = await session.get(PaperDocument, uuid.UUID(str(doc_id)))
        if document is None or document.project_id != job.project_id:
            return
        rows = await list_sections(session, document.id)
        if not rows:
            return
        snapshot = document_snapshot_hash(rows)
        baseline = await session.scalar(
            select(QualityReportRecord)
            .where(
                QualityReportRecord.document_id == document.id,
                QualityReportRecord.paper_snapshot_hash == snapshot,
            )
            .order_by(QualityReportRecord.created_at.desc())
            .limit(1)
        )
        units = [evidence_payload(e) for e in await list_evidence_units(session, job.project_id)]
        whitelist = await get_writing_whitelist(session, job.project_id)
        assets = await grounded_asset_payloads(session, job.project_id)
        questions = {
            str(q.id): q.text for q in await list_research_questions(session, job.project_id)
        }
        outline = await get_outline(session, document.outline_id) if document.outline_id else None
        mapping = (
            {
                str(s.get("key")): questions.get(str(s.get("question_id")))
                for s in (outline.tree_json or {}).get("sections", [])
            }
            if outline
            else {}
        )
        materials = [
            build_review_input(
                section_key=r.section_key,
                question=mapping.get(r.section_key) or f"核查本节事实与证据支持：{r.title}",
                prose=_body_text_for_quality(r.body_ir_json or {}),
                body=r.body_ir_json or {},
                evidence=units,
                whitelist=whitelist,
                assets=assets,
                scope={
                    "project_id": str(job.project_id),
                    "document_id": str(document.id),
                    "snapshot": snapshot,
                    "purpose": "shadow",
                },
            )
            for r in rows
        ]
        anchors = (
            list(
                (
                    await session.scalars(
                        select(ClaimEvidenceAnchor).where(
                            ClaimEvidenceAnchor.quality_report_id == baseline.id
                        )
                    )
                ).all()
            )
            if baseline
            else []
        )
        baseline_data = (
            {c.name: getattr(baseline, c.name) for c in baseline.__table__.columns}
            if baseline
            else None
        )
        frozen = json.loads(
            dumps(
                {
                    "version": VERSION,
                    "implementation_hash": implementation_hash(),
                    "baseline_claim_anchors": [
                        {c.name: getattr(a, c.name) for c in a.__table__.columns} for a in anchors
                    ],
                    "source_job_id": str(source_id),
                    "document_id": str(document.id),
                    "document_version": document.version,
                    "snapshot": snapshot,
                    "source_status": job.status,
                    "baseline": baseline_data,
                    "baseline_comparable": baseline is not None,
                    "sections": sample_sections(materials),
                    "all_section_keys": [r.section_key for r in rows],
                    "full_body": [
                        {"section_key": r.section_key, "body": r.body_ir_json} for r in rows
                    ],
                    "repeats": settings.evaluator_shadow_repeats,
                    "sampling": "bounded cohort; risk/coverage sections; not random population",
                }
            )
        )
        project_id = job.project_id
    async with factory() as session:
        # Serialize admission across both workers and blue/green deployments.
        await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": LOCK_ID})
        count = await session.scalar(
            select(func.count())
            .select_from(EvaluatorShadowRun)
            .where(EvaluatorShadowRun.version == VERSION)
        )
        existing = await session.scalar(
            select(EvaluatorShadowRun.id).where(
                EvaluatorShadowRun.source_job_id == source_id, EvaluatorShadowRun.version == VERSION
            )
        )
        if existing or count >= settings.evaluator_shadow_document_limit:
            return
        # Repeated jobs over an identical snapshot do not consume cohort slots.
        duplicate = await session.scalar(
            select(EvaluatorShadowRun.id).where(
                EvaluatorShadowRun.version == VERSION,
                EvaluatorShadowRun.project_id == project_id,
                EvaluatorShadowRun.input_json["snapshot"].astext == snapshot,
            )
        )
        if duplicate:
            return
        run_id = uuid.uuid4()
        session.add(
            EvaluatorShadowRun(
                id=run_id,
                source_job_id=source_id,
                project_id=project_id,
                version=VERSION,
                status="pending",
                input_json=frozen,
                results_json=[],
                artifact_prefix=f"projects/{project_id}/evaluator-shadow/{run_id}",
            )
        )
        await session.commit()


async def after_job_end(ctx: dict) -> None:
    """ARQ invokes this after recording the foreground result; failures cannot change it."""
    if not ctx["settings"].evaluator_shadow_enabled:
        return
    try:
        source_id = uuid.UUID(str(ctx.get("job_id")))
    except ValueError:
        return  # Shadow cron jobs have non-UUID ARQ ids.
    try:
        await asyncio.wait_for(capture(ctx, source_id), timeout=15)
    except Exception:
        logger.warning("shadow capture failed; foreground result unchanged", exc_info=True)


class ShadowContext(JobContext):
    """Use existing model telemetry/limiter, but isolate every write in private artifacts."""

    async def emit(self, event_type, payload=None, **kwargs):
        self.checkpoint.update(kwargs.get("checkpoint") or {})
        item = {"event": event_type, "payload": payload, "time": datetime.now(UTC).isoformat()}
        self.events.append(item)
        await asyncio.to_thread(
            self.store.put,
            f"{self.prefix}/events/{len(self.events):04}.json",
            dumps(item),
            content_type="application/json",
        )

    async def _save_call(self, record):
        await asyncio.to_thread(
            self.store.put,
            f"{self.prefix}/calls/{fingerprint(asdict(record))}.json",
            dumps(asdict(record)),
            content_type="application/json",
        )


def deterministic_checks(material):
    from paperforge_worker.pipelines.quality import _PLACEHOLDER_RE

    return (
        material["binding_issues"]
        + material["table_role_conflicts"]
        + [
            {"claim_id": c["claim_id"], "reason": "placeholder", "quote": c["text"]}
            for c in material["claims"]
            if _PLACEHOLDER_RE.search(c["text"])
        ]
    )


def result_row(material, repeat, verdict, events):
    trace = next((e["payload"] for e in reversed(events) if e["event"] == "review.result"), {})
    raw = trace.get("raw_judgment") or {}
    checks = raw.get("claim_checks")
    checks = checks if isinstance(checks, list) else []
    return {
        "section_key": material["section_key"],
        "input_hash": material["input_hash"],
        "repeat": repeat,
        "status": trace.get("status", "unassessed"),
        "reason": trace.get("reason", "no_result"),
        "deterministic": deterministic_checks(material),
        "shadow_has_violation": bool(deterministic_checks(material))
        or (verdict is not None and not verdict.acceptable),
        "llm": raw,
        "verdict": verdict.to_payload() if verdict else None,
        "format_attempts": sum(e["event"] == "review.raw_result" for e in events),
        "source_ambiguity": any(
            c.get("status") == "uncertain" for c in checks if isinstance(c, dict)
        ),
    }


async def foreground_busy(factory) -> bool:
    async with factory() as session:
        return bool(
            await session.scalar(
                select(GenerationJob.id)
                .where(GenerationJob.status.in_(["queued", "running"]))
                .limit(1)
            )
        )


async def update_shadow(factory, run_id, **values):
    async with factory() as session:
        row = await session.get(EvaluatorShadowRun, run_id)
        if row is not None:
            for k, v in values.items():
                setattr(row, k, v)
            row.updated_at = datetime.now(UTC)
            await session.commit()


async def shadow_tick(ctx: dict) -> None:
    """One bounded low-priority run. Crash/timeout is terminal, never a paid replay loop."""
    settings, factory = ctx["settings"], ctx["session_factory"]
    if not settings.evaluator_shadow_enabled or await foreground_busy(factory):
        return
    async with factory() as session:
        await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": LOCK_ID})
        running = list(
            (
                await session.scalars(
                    select(EvaluatorShadowRun).where(EvaluatorShadowRun.status == "running")
                )
            ).all()
        )
        for old in running:
            if old.updated_at < datetime.now(UTC) - timedelta(minutes=40):
                old.status, old.error = "interrupted", "lease_expired_no_automatic_replay"
        if running:
            await session.commit()
            return
        row = await session.scalar(
            select(EvaluatorShadowRun)
            .where(
                EvaluatorShadowRun.status == "pending",
                EvaluatorShadowRun.version == VERSION,
            )
            .order_by(EvaluatorShadowRun.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if row is None:
            return
        row.status, row.updated_at = "running", datetime.now(UTC)
        run_id, project_id, frozen, prefix = (
            row.id,
            row.project_id,
            row.input_json,
            row.artifact_prefix,
        )
        results = list(row.results_json)
        await session.commit()
    context = None
    try:
        if frozen.get("implementation_hash") != implementation_hash():
            raise ValueError("shadow_implementation_changed")
        store = make_object_store(settings)
        await asyncio.to_thread(
            store.put, f"{prefix}/input.json", dumps(frozen), content_type="application/json"
        )
        # Independent budget and trace; never inherit the writer's checkpoint or costs.
        bounded_settings = settings.model_copy(
            update={
                "agent_max_calls": 72,
                "agent_max_reserved_tokens": 2_000_000,
                "agent_max_seconds": 1800,
            }
        )
        context = ShadowContext(
            project_id=project_id,
            job_id=None,
            settings=bounded_settings,
            session_factory=None,
            http_client=httpx.Client(),
            scholar_cache=None,
        )
        context.store = store
        context.events = []
        # Preserve reservation budget when foreground arrival pauses between calls.
        context.checkpoint = dict(results[-1].get("budget_checkpoint") or {}) if results else {}
        completed = {(r["section_key"], r["repeat"]) for r in results}
        runner = context.llm_runner()
        for repeat in range(1, frozen["repeats"] + 1):
            for material in frozen["sections"]:
                if (material["section_key"], repeat) in completed:
                    continue
                if await foreground_busy(factory):
                    await update_shadow(factory, run_id, status="pending")
                    return
                # No reuse of outputs across repeats; identical frozen inputs every time.
                context.events = []
                context.prefix = f"{prefix}/repeat-{repeat}/{material['input_hash']}"
                start = asyncio.get_running_loop().time()
                call_start = len(context.llm_calls)
                failure = None
                verdict = None
                try:
                    verdict = await review_section(
                        section_key=material["section_key"],
                        question=material["question"],
                        prose=material["prose"],
                        evidence=material["evidence"],
                        review_input=material,
                        runner=runner,
                        trace_context=context,
                    )
                except BaseException as error:
                    failure = error
                result = result_row(material, repeat, verdict, context.events)
                result["calls"] = [
                    json.loads(dumps(asdict(c))) for c in context.llm_calls[call_start:]
                ]
                if failure is not None:
                    result.update(status="unassessed", reason=type(failure).__name__)
                result["seconds"] = asyncio.get_running_loop().time() - start
                result["budget_checkpoint"] = dict(context.checkpoint)
                result["artifact_prefix"] = context.prefix
                results.append(result)
                await update_shadow(factory, run_id, results_json=results)
                if failure is not None:
                    raise failure
        await update_shadow(factory, run_id, status="completed")
    except BaseException as error:
        await update_shadow(
            factory,
            run_id,
            status="interrupted" if not isinstance(error, Exception) else "failed",
            error=type(error).__name__,
        )
        raise
    finally:
        if context is not None:
            context.http_client.close()
