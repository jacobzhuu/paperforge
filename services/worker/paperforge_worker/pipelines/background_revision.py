"""Deliver first; review and repair a private candidate through a durable outbox."""

from __future__ import annotations

import hashlib
import uuid

from db import (
    JOB_RESUME_KEY,
    create_job,
    document_snapshot_hash,
    get_project,
    latest_document,
    list_sections,
)
from db.models.paper import JobDispatch, PaperDocument, PaperProject
from db.models.revision import DraftRevision
from db.repositories.revisions import clone_document, revision_document
from sqlalchemy import select

from paperforge_worker.context import JobStopped, job_context


async def yield_to_foreground(context):
    """Cancellation/deadline checkpoint; provider admission owns fair scheduling."""
    await context.raise_if_stopped()
    if hasattr(context, "revision_active_seconds"):
        if context.revision_active_seconds() >= context.settings.agent_max_seconds:
            budget = dict(context.checkpoint.get("agent_budget") or {})
            budget["stop_reason"] = "deadline"
            await context.emit(
                "agent.budget_exhausted",
                {"reason": "deadline"},
                checkpoint={"agent_budget": budget},
            )
            raise JobStopped("pause", reason="budget_exhausted")


async def schedule_revision(context, *, document_id, snapshot, review_style):
    """Clone under the editor lock and commit the candidate + job + outbox atomically."""
    async with context.session() as session:
        await session.scalar(
            select(PaperProject.id).where(PaperProject.id == context.project_id).with_for_update()
        )
        existing = await session.scalar(
            select(DraftRevision).where(DraftRevision.source_job_id == context.job_id)
        )
        if existing:
            return str(existing.id)
        source = await session.scalar(
            select(PaperDocument)
            .where(PaperDocument.id == document_id, PaperDocument.project_id == context.project_id)
            .with_for_update()
        )
        if (
            source is None
            or document_snapshot_hash(await list_sections(session, source.id)) != snapshot
        ):
            return None
        candidate = await clone_document(session, source)
        revision_id = uuid.uuid4()
        kwargs = {"review_style": review_style, "revision_id": str(revision_id)}
        job = await create_job(
            session,
            project_id=context.project_id,
            kind="revision",
            checkpoint={
                "background_revision": True,
                JOB_RESUME_KEY: {"function": "run_background_revision", "kwargs": kwargs},
            },
        )
        record = DraftRevision(
            id=revision_id,
            project_id=context.project_id,
            source_job_id=context.job_id,
            job_id=job.id,
            source_document_id=source.id,
            candidate_document_id=candidate.id,
            source_hash=snapshot,
            status="pending",
            recovery_json={"version": 1, "operations": {}},
        )
        session.add(record)
        owner = await session.scalar(
            select(PaperProject.owner_id).where(PaperProject.id == context.project_id)
        )
        session.add(
            JobDispatch(
                job_id=job.id,
                owner_id=owner,
                queue_identity=hashlib.sha256(context.settings.redis_url.encode()).hexdigest(),
                function="run_background_revision",
                kwargs_json=kwargs,
            )
        )
        await session.flush()
        return str(record.id)


async def deliver_fast_draft(context, *, review_style):
    from paperforge_worker import worker as w

    report = await w._run_stage(
        context,
        "quality",
        lambda: w._quality(
            context, quality_profile="draft", review_style=review_style, deterministic_only=True
        ),
    )
    async with context.session() as session:
        document = await latest_document(session, context.project_id)
        document_id = document.id
        snapshot = document_snapshot_hash(await list_sections(session, document_id))
    await context.emit(
        "revision.intent",
        {},
        checkpoint={
            "revision_intent": {
                "document_id": str(document_id),
                "snapshot": snapshot,
                "review_style": review_style,
            }
        },
    )
    outcome = await w._run_stage(
        context,
        "render",
        lambda: w.export_document(
            context,
            store=w._object_store(context.settings),
            quality_profile="draft",
            quality_report_id=report.report_id if report else None,
            readiness_status="draft",
            paper_snapshot_hash=snapshot,
        ),
    )
    payload = outcome.to_payload() if outcome else context.stage_payload("render")
    delivered = bool(payload and "pdf" in (payload.get("formats") or {}))
    # Only persisted export formats count as delivery; previews do not.
    if delivered:
        await context.emit(
            "fast_draft.ready",
            {
                "document_id": str(document_id),
                "paper_snapshot_hash": snapshot,
                "status": "verification_pending",
            },
            checkpoint={
                "fast_draft_preview": {
                    "document_id": str(document_id),
                    "paper_snapshot_hash": snapshot,
                    "status": "pending",
                }
            },
        )
        try:
            revision_id = await schedule_revision(
                context, document_id=document_id, snapshot=snapshot, review_style=review_style
            )
            await context.emit(
                "revision.queued",
                {
                    "revision_id": revision_id,
                    "status": "pending" if revision_id else "superseded",
                },
                checkpoint={"revision_intent": None},
            )
        except JobStopped:
            raise
        except Exception as error:
            await context.emit("revision.scheduling_deferred", {"reason": type(error).__name__})
    await w._finish(context, delivered=delivered)
    return {"export": payload, "quality": report.to_payload() if report else None}


async def _review(context, language):
    from paperforge_worker import worker as w
    from paperforge_worker.pipelines.manuscript_review import review_manuscript
    from paperforge_worker.pipelines.semantic_review import build_section_verdict

    inputs = await w._section_review_inputs(context, include_unlinked=True)
    audit = await review_manuscript(context, inputs, language=language)
    verdicts = {
        p["section_key"]: build_section_verdict(p, section_key=p["section_key"])
        for p in audit["findings"]
    }
    return inputs, verdicts, audit["unassessed"]


async def run_background_revision(
    ctx, project_id, job_id, *, review_style="narrative", revision_id=None
):
    from paperforge_worker import worker as w
    from paperforge_worker.config import get_settings

    settings = (ctx.get("settings") or get_settings()).model_copy(
        update={
            "section_review_concurrency": 2,
            "verifier_concurrency": 2,
            "writer_repair_concurrency": 2,
            "claim_entailment_mode": "enforce",
        }
    )
    async with job_context(
        project_id=uuid.UUID(project_id),
        job_id=uuid.UUID(job_id),
        settings=settings,
        session_factory=ctx.get("session_factory"),
        scholar_cache=ctx.get("scholar_cache"),
        event_publisher=ctx.get("redis"),
    ) as context:
        context.shadow_admission = True
        async with context.session() as session:
            record = await session.scalar(
                select(DraftRevision)
                .where(
                    DraftRevision.id == uuid.UUID(revision_id),
                    DraftRevision.project_id == context.project_id,
                )
                .with_for_update()
            )
            if record is None:
                raise ValueError("revision not found")
            if record.status in {"accepted", "discarded", "ready", "needs_review"}:
                await w._finish(context, delivered=True)
                return {"status": record.status}
            if record.job_id != context.job_id:
                raise JobStopped("pause", reason="revision_owner_changed")
            record.job_id = context.job_id
            candidate_id = record.candidate_document_id
            record.status = "running"
            project = await get_project(session, context.project_id)
            language, paper_type = project.language, project.paper_type
            project_inputs = {
                "language": language,
                "paper_type": paper_type,
                "scope": project.scope_json or {},
            }
        from paperforge_worker.pipelines import revision_recovery as recovery

        if await recovery.initialize(context, revision_id) is None:
            return {"status": "already_running"}
        if hasattr(context, "start_revision_clock"):
            context.start_revision_clock()
            await context.save_revision_clock()
        await recovery.guard_sources(context, project_inputs, field="project_input_hash")
        await w._mark_running(context)
        with revision_document(context.project_id, candidate_id):
            await yield_to_foreground(context)
            initial_report = await w._quality(
                context, quality_profile="draft", review_style=review_style
            )
            inputs, verdicts, unassessed = await _review(context, language)
            failing = [v for v in verdicts.values() if not v.acceptable]
            # The durable per-section ledger owns retries, not a pre-call attempt flag.
            selected = sorted(
                failing, key=lambda v: (not bool(v.unsupported_claims), v.section_key)
            )[: settings.background_revision_max_sections]
            notes = {
                v.section_key: w._repair_note(
                    v, next(i for i in inputs if i["section_key"] == v.section_key)
                )
                for v in selected
            }
            # Numeric/locator and manuscript-wide findings must also reach the writer,
            # even if a section reviewer is unavailable or mistakenly accepts the prose.
            deterministic_notes = {}
            for anchor in initial_report.claim_evidence:
                if anchor.get("is_core") and anchor.get("support_status") != "supported":
                    key = anchor.get("section_key")
                    deterministic_notes.setdefault(key, []).append(
                        "核对论断与原文定位："
                        + str(anchor.get("claim_text") or "")
                        + "；无法获得支持时限定或删除，不能补造数值/机制。"
                    )
            for finding in w.recoverable_findings(initial_report):
                for key in finding.get("section_keys") or finding.get("sections") or []:
                    deterministic_notes.setdefault(key, []).append(str(finding))
            available = {i["section_key"] for i in inputs}
            for key, items in deterministic_notes.items():
                if key not in available:
                    continue
                if key not in notes and len(notes) >= settings.background_revision_max_sections:
                    continue
                notes[key] = notes.get(key, "") + "\n" + "\n".join(items)
            gap_questions = {
                uuid.UUID(i["question_id"])
                for i in inputs
                if i.get("question_id")
                and any(
                    v.section_key == i["section_key"] and v.diagnosis == "evidence_gap"
                    for v in selected
                )
            }
            plan = await recovery.save_plan(
                context,
                {
                    "notes": notes,
                    "question_ids": sorted(str(q) for q in gap_questions),
                },
            )
            notes = plan["notes"]
            gap_questions = {uuid.UUID(q) for q in plan["question_ids"]}
            if gap_questions:
                from paperforge_worker.pipelines.revision_retrieval import supplement

                await supplement(context, language=language, question_ids=gap_questions)
            if notes:
                await w.repair_document_sections(
                    context,
                    section_keys=set(notes),
                    language=language,
                    paper_type=paper_type,
                    notes=notes,
                    concurrency=2,
                )
                await recovery.ensure_settled(context)
                inputs, verdicts, unassessed = await _review(context, language)
            await yield_to_foreground(context)
            report = (
                await w._quality(context, quality_profile="draft", review_style=review_style)
                if notes or gap_questions
                else initial_report
            )
            review = {
                "status": "partial" if unassessed else "completed",
                "sections_reviewed": len(verdicts),
                "unassessed": sorted(unassessed),
                "sections_failing": sorted(k for k, v in verdicts.items() if not v.acceptable),
                "repair_attempts": sorted(notes),
                "findings": [v.to_payload() for v in verdicts.values()],
            }
            review = {**review, **report.depth_metrics.get("review_coverage", {})}
            review["repair_attempts"] = sorted(notes)
            report.depth_metrics["review_coverage"] = review
            await w._publish_quality_report(
                context,
                report,
                document_id=candidate_id,
                document_version=report.document_version,
                quality_profile="draft",
                review_style=review_style,
            )
            await yield_to_foreground(context)
            outcome = await w._run_stage(
                context,
                "render",
                lambda: w.export_document(
                    context,
                    store=w._object_store(settings),
                    quality_profile="draft",
                    quality_report_id=report.report_id,
                    readiness_status="draft",
                    paper_snapshot_hash=report.paper_snapshot_hash,
                ),
            )
            payload = outcome.to_payload() if outcome else context.stage_payload("render")
            incomplete = report.depth_metrics.get("claim_evidence_verification", {}).get(
                "unverified_count", 0
            )
            delivered = bool(payload and "pdf" in (payload.get("formats") or {}))
            review["pdf_ready"] = delivered
            review["quality_warnings"] = report.warnings + report.blockers
            review["layout_checks"] = (payload or {}).get("layout_checks") or {}
            status = (
                "needs_review"
                if unassessed
                or review["sections_failing"]
                or incomplete
                or report.warnings
                or report.blockers
                or not delivered
                or review["layout_checks"].get("passed") is False
                or (payload or {}).get("bibliography_ok") is False
                else "ready"
            )
            if not delivered:
                status = "failed"
            async with context.session() as session:
                record = await session.scalar(
                    select(DraftRevision).where(
                        DraftRevision.id == uuid.UUID(revision_id),
                        DraftRevision.project_id == context.project_id,
                    )
                )
                record.status, record.review_json = status, review
            await context.emit(
                "revision.available", {"status": status, "document_id": str(candidate_id)}
            )
            await w._finish(context, delivered=delivered)
            return {"status": status, "review": review, "export": payload}
