"""Durable, low-priority Jev observations. Never changes the baseline verdict."""

import uuid
from datetime import UTC, datetime, timedelta

from db.models.paper import GenerationJob
from db.models.research import CitationShadow
from ingest.research import digest
from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert

from paperforge_worker.context import JobContext
from paperforge_worker.pipelines.citation_decisions import decision_request


async def enqueue(context, pairs, observation):
    state, questions = decision_request(pairs)
    frozen = {
        "state": state,
        "questions": questions,
        "model": context.settings.typesafe_model,
        "pairs": pairs,
        "baseline": observation,
        "version": "citation-shadow-v3",
        "source_job": str(context.job_id),
    }
    async with context.session() as session:
        await session.execute(text("SELECT pg_advisory_xact_lock(739241811)"))
        pending = await session.scalar(
            select(func.count())
            .select_from(CitationShadow)
            .where(CitationShadow.status.in_(["pending", "running"]))
        )
        if pending >= 100:
            return
        await session.execute(
            insert(CitationShadow)
            .values(
                id=uuid.uuid4(),
                project_id=context.project_id,
                job_id=context.job_id,
                fingerprint=digest(frozen),
                status="pending",
                payload_json=frozen,
            )
            .on_conflict_do_nothing(index_elements=["fingerprint"])
        )


async def citation_shadow_tick(ctx):
    factory = ctx["session_factory"]
    # Keep a small, bounded background cohort. Do not compete with queued/running foreground work.
    async with factory() as session:
        await session.execute(
            update(CitationShadow)
            .where(
                CitationShadow.status == "running",
                CitationShadow.created_at < datetime.now(UTC) - timedelta(hours=1),
            )
            .values(status="interrupted", result_json={"error": "execution_uncertain"})
        )
        await session.execute(
            update(CitationShadow)
            .where(
                CitationShadow.status == "pending",
                CitationShadow.created_at < datetime.now(UTC) - timedelta(days=1),
            )
            .values(status="expired")
        )
        await session.commit()
        active = await session.scalar(
            select(func.count())
            .select_from(GenerationJob)
            .where(GenerationJob.status.in_(["queued", "running"]))
        )
        if active:
            return
        row = await session.scalar(
            select(CitationShadow)
            .where(CitationShadow.status == "pending")
            .order_by(CitationShadow.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if row is None:
            return
        row.status = "running"
        identifier, project_id, job_id, frozen = (
            row.id,
            row.project_id,
            row.job_id,
            row.payload_json,
        )
        await session.commit()
    import httpx

    with httpx.Client() as client:
        context = JobContext(
            project_id=project_id,
            job_id=job_id,
            settings=ctx["settings"],
            session_factory=factory,
            http_client=client,
            scholar_cache=ctx.get("scholar_cache"),
            event_publisher=ctx.get("redis"),
        )
        context.shadow_admission = True
        async with factory() as session:
            job = await session.get(GenerationJob, job_id)
            if job:
                context.checkpoint = dict(job.checkpoint_json or {})
        try:
            if frozen["model"] != ctx["settings"].typesafe_model:
                raise ValueError("shadow model version changed")
            state, questions = frozen["state"], frozen["questions"]
            response = await context.decision_runner().decide(
                state=state,
                questions=questions,
                metadata={"stage": "citation_shadow", "decision_version": "citation-shadow-v3"},
            )
            result = {
                "answers": response.answers,
                "error": response.error,
                "model": response.model,
                "usage": response.usage,
                "latency_ms": response.latency_ms,
                "request_id": response.request_id,
                "baseline": frozen["baseline"],
            }
            status = "completed" if response.ok else "failed"
        except Exception as error:
            status, result = "failed", {"error": type(error).__name__}
        async with factory() as session:
            row = await session.get(CitationShadow, identifier)
            row.status, row.result_json = status, result
            await session.commit()
        await context.emit(
            "quality.jev_shadow",
            {
                "shadow_id": str(identifier),
                "status": status,
                "version": "citation-shadow-v3",
                "latency_ms": result.get("latency_ms"),
            },
        )
