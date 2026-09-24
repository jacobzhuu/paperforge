"""Tenant-scoped, metadata-only task inspection; no prompt or checkpoint dump."""

from __future__ import annotations

import uuid

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models.paper import GenerationJob, JobEvent, LlmCallLog

_EVENT_FIELDS = {
    "span_id",
    "parent_span_id",
    "kind",
    "name",
    "node_id",
    "attempt",
    "status",
    "elapsed_ms",
    "error_type",
    "action",
    "stop_reason",
    "reason",
    "rounds_used",
    "policy",
    "total",
    "rewritten",
    "skipped",
    "rejected",
    "concurrency",
}
_CALL_FIELDS = {
    "span_id",
    "parent_span_id",
    "node_id",
    "attempt",
    "local_queue_wait_ms",
    "provider_slot_wait_ms",
    "rate_limit_wait_ms",
}


def event_metadata(event_type: str, payload: dict) -> dict:
    fields = {k: v for k, v in payload.items() if k in _EVENT_FIELDS}
    if event_type == "polish.completed" and "rewritten" not in payload:
        # Legacy events use a boolean skip flag and nested result counts.
        fields.pop("skipped", None)
        done, total = payload.get("done"), payload.get("total")
        if type(done) is int and type(total) is int and 0 <= done <= total:
            fields.update(policy="legacy", rewritten=done, skipped=total - done)
        rejected = (payload.get("results") or {}).get("rejected")
        if type(rejected) is int and rejected >= 0:
            fields["rejected"] = rejected
    return fields


async def task_trace(
    session: AsyncSession, project_id: uuid.UUID, job_id: uuid.UUID
) -> dict | None:
    job = await session.scalar(
        select(GenerationJob).where(
            GenerationJob.id == job_id,
            GenerationJob.project_id == project_id,
        )
    )
    if job is None:
        return None
    checkpoint = job.checkpoint_json or {}
    trace_id = checkpoint.get("agent_trace_id") or str(job.id)
    jobs = list(
        (
            await session.scalars(
                select(GenerationJob)
                .where(
                    GenerationJob.project_id == project_id,
                    or_(
                        GenerationJob.id == job.id,
                        GenerationJob.checkpoint_json["agent_trace_id"].astext == trace_id,
                    ),
                )
                .order_by(GenerationJob.created_at)
            )
        ).all()
    )
    identifiers = [row.id for row in jobs]
    event_query = (
        select(JobEvent)
        .where(
            JobEvent.job_id.in_(identifiers),
            or_(
                JobEvent.event_type.like("agent.%"),
                JobEvent.event_type.like("semantic_repair.%"),
                JobEvent.event_type.like("polish.%"),
            ),
        )
        .order_by(JobEvent.created_at, JobEvent.seq)
    )
    events = list((await session.scalars(event_query.limit(1001))).all())
    call_scope = (
        LlmCallLog.project_id == project_id,
        LlmCallLog.job_id.in_(identifiers),
    )
    totals = (
        (
            await session.execute(
                select(
                    func.count().label("calls"),
                    func.coalesce(func.sum(LlmCallLog.cost_estimate), 0).label("known_cost"),
                    func.count().filter(LlmCallLog.cost_estimate.is_(None)).label("unpriced_calls"),
                    func.coalesce(func.sum(LlmCallLog.input_tokens), 0).label("input_tokens"),
                    func.coalesce(func.sum(LlmCallLog.output_tokens), 0).label("output_tokens"),
                    func.count()
                    .filter(
                        or_(LlmCallLog.input_tokens.is_(None), LlmCallLog.output_tokens.is_(None))
                    )
                    .label("unknown_usage_calls"),
                ).where(*call_scope)
            )
        )
        .mappings()
        .one()
    )
    errors = dict(
        (
            await session.execute(
                select(LlmCallLog.error_code, func.count())
                .where(*call_scope, LlmCallLog.error_code.is_not(None))
                .group_by(LlmCallLog.error_code)
            )
        ).all()
    )
    calls = list(
        (
            await session.scalars(
                select(LlmCallLog)
                .where(*call_scope)
                .order_by(LlmCallLog.occurred_at, LlmCallLog.id)
                .limit(1000)
            )
        ).all()
    )
    latest = jobs[-1].checkpoint_json or {}
    budget = latest.get("agent_budget") or {}
    interruption = latest.get("repair_interrupt")
    return {
        "trace_id": trace_id,
        "jobs": [
            {"id": str(row.id), "status": row.status, "created_at": row.created_at.isoformat()}
            for row in jobs
        ],
        "summary": {**dict(totals), "errors": errors},
        "budget": {
            key: budget[key] for key in ("calls", "reserved_tokens", "started_at") if key in budget
        },
        "engine": latest.get("semantic_repair_engine", "legacy"),
        "interruption": interruption if isinstance(interruption, dict) else None,
        "events_truncated": len(events) > 1000,
        "events": [
            {
                "type": e.event_type,
                "at": e.created_at.isoformat(),
                "job_id": str(e.job_id),
                **event_metadata(e.event_type, e.payload_json or {}),
            }
            for e in events[:1000]
        ],
        "calls": [
            {
                "role": c.role,
                "model": c.model,
                "latency_ms": c.latency_ms,
                "input_tokens": c.input_tokens,
                "output_tokens": c.output_tokens,
                "cost_estimate": c.cost_estimate,
                "error_code": c.error_code,
                "prompt_sha256": c.prompt_sha256,
                **{k: v for k, v in (c.metadata_json or {}).items() if k in _CALL_FIELDS},
            }
            for c in calls[:1000]
        ],
        "calls_truncated": totals["calls"] > 1000,
    }
