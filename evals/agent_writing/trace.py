"""Read-only export of an Agent trace from PostgreSQL, without prompts/manuscript text."""

import argparse
import asyncio
import json
import uuid
from pathlib import Path

from db.models.paper import GenerationJob, JobEvent, LlmCallLog
from db.session import make_engine, make_session_factory
from paperforge_worker.config import WorkerSettings
from sqlalchemy import select


async def export_trace(job_id: uuid.UUID) -> dict:
    engine = make_engine(WorkerSettings().database_url)
    try:
        async with make_session_factory(engine)() as session:
            job = await session.get(GenerationJob, job_id)
            if job is None:
                raise ValueError("job not found")
            trace_id = (job.checkpoint_json or {}).get("agent_trace_id", str(job.id))
            jobs = list(
                (
                    await session.scalars(
                        select(GenerationJob.id).where(
                            GenerationJob.project_id == job.project_id,
                            GenerationJob.checkpoint_json["agent_trace_id"].astext == trace_id,
                        )
                    )
                ).all()
            ) or [job.id]
            events = list(
                (
                    await session.scalars(
                        select(JobEvent)
                        .where(JobEvent.job_id.in_(jobs), JobEvent.event_type.like("agent.%"))
                        .order_by(JobEvent.created_at, JobEvent.seq)
                    )
                ).all()
            )
            calls = list(
                (
                    await session.scalars(
                        select(LlmCallLog)
                        .where(LlmCallLog.job_id.in_(jobs))
                        .order_by(LlmCallLog.occurred_at)
                    )
                ).all()
            )
            finished = {
                e.payload_json.get("span_id")
                for e in events
                if e.event_type == "agent.span_finished"
            }
            logged = {(c.metadata_json or {}).get("span_id") for c in calls}
            return {
                "trace_id": trace_id,
                "job_ids": [str(i) for i in jobs],
                "budget": (job.checkpoint_json or {}).get("agent_budget"),
                "unfinished_spans": [
                    e.payload_json
                    for e in events
                    if e.event_type == "agent.span_started"
                    and e.payload_json.get("span_id") not in finished
                ],
                "unreconciled_calls": [
                    e.payload_json
                    for e in events
                    if e.event_type == "agent.call_reserved"
                    and e.payload_json.get("span_id") not in logged
                ],
                "events": [
                    {"type": e.event_type, "at": str(e.created_at), **(e.payload_json or {})}
                    for e in events
                ],
                "calls": [
                    {
                        "model": c.model,
                        "role": c.role,
                        "latency_ms": c.latency_ms,
                        "input_tokens": c.input_tokens,
                        "output_tokens": c.output_tokens,
                        "cost_estimate": c.cost_estimate,
                        "error_code": c.error_code,
                        "prompt_sha256": c.prompt_sha256,
                        **(c.metadata_json or {}),
                    }
                    for c in calls
                ],
                "note": "An unreconciled reservation is ambiguous, not proof of a paid call.",
            }
    finally:
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-id", type=uuid.UUID, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.write_text(json.dumps(asyncio.run(export_trace(args.job_id)), indent=2))


if __name__ == "__main__":
    main()
