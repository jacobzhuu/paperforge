"""任务与事件仓储：checkpoint + SSE 事件源（设计 §4.3 generation_job / job_event）。

Draft-first：任何阶段失败都写 checkpoint + 事件，任务本身仍可继续或交付当前最好稿；
状态机里没有「拒绝产出」终态。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models.paper import GenerationJob, JobEvent, LlmCallLog

JOB_KINDS = frozenset(
    {"search", "ingest", "cards", "outline", "write", "compile", "visual", "full"}
)
JOB_STATUSES = frozenset({"queued", "running", "succeeded", "failed", "cancelled"})


async def create_job(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    kind: str,
    checkpoint: dict[str, Any] | None = None,
) -> GenerationJob:
    if kind not in JOB_KINDS:
        raise ValueError(f"unsupported job kind: {kind}")
    job = GenerationJob(
        project_id=project_id,
        kind=kind,
        status="queued",
        progress=0.0,
        checkpoint_json=checkpoint,
    )
    session.add(job)
    await session.flush()
    return job


async def get_job(session: AsyncSession, job_id: uuid.UUID) -> GenerationJob | None:
    return await session.get(GenerationJob, job_id)


async def list_jobs(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    limit: int = 50,
) -> list[GenerationJob]:
    return list(
        (
            await session.scalars(
                select(GenerationJob)
                .where(GenerationJob.project_id == project_id)
                .order_by(GenerationJob.created_at.desc())
                .limit(limit)
            )
        ).all()
    )


async def update_job(
    session: AsyncSession,
    job: GenerationJob,
    *,
    status: str | None = None,
    stage: str | None = None,
    progress: float | None = None,
    checkpoint: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
) -> GenerationJob:
    if status is not None:
        if status not in JOB_STATUSES:
            raise ValueError(f"unsupported job status: {status}")
        job.status = status
        if status in {"succeeded", "failed", "cancelled"}:
            job.finished_at = datetime.now(UTC)
    if stage is not None:
        job.stage = stage
    if progress is not None:
        job.progress = max(0.0, min(1.0, float(progress)))
    if checkpoint is not None:
        # checkpoint 合并而非替换：断点续跑要能看到此前所有阶段的产物。
        job.checkpoint_json = {**(job.checkpoint_json or {}), **checkpoint}
    if error is not None:
        job.error_json = error
    await session.flush()
    return job


# 用户在润色途中点「跳过」时写进 checkpoint 的开关。API 写、worker 在每节之间读，
# 键名放在数据层是为了两边只认同一个字符串。
POLISH_SKIP_KEY = "polish_skip"


async def request_polish_skip(session: AsyncSession, job: GenerationJob) -> GenerationJob:
    """请求跳过剩余的连贯性润色。幂等；worker 写完当前这一节后才会看到。"""
    return await update_job(session, job, checkpoint={POLISH_SKIP_KEY: True})


def polish_skip_requested(job: GenerationJob | None) -> bool:
    return bool(job is not None and (job.checkpoint_json or {}).get(POLISH_SKIP_KEY))


def lock_generation_job_stmt(job_id: uuid.UUID) -> Select:
    """Serialize event sequence allocation per generation job."""
    return select(GenerationJob.id).where(GenerationJob.id == job_id).with_for_update()


def next_job_event_seq_stmt(job_id: uuid.UUID) -> Select:
    return select(func.coalesce(func.max(JobEvent.seq), 0) + 1).where(JobEvent.job_id == job_id)


async def append_job_event(
    session: AsyncSession,
    *,
    job_id: uuid.UUID,
    event_type: str,
    payload: dict[str, Any] | None = None,
) -> JobEvent:
    """Append an SSE event with a transactionally serialized per-job sequence."""
    locked_job_id = (await session.execute(lock_generation_job_stmt(job_id))).scalar_one_or_none()
    if locked_job_id is None:
        raise ValueError(f"generation job not found: {job_id}")
    seq = int((await session.execute(next_job_event_seq_stmt(job_id))).scalar_one())
    event = JobEvent(
        job_id=job_id,
        seq=seq,
        event_type=event_type,
        payload_json=payload,
    )
    session.add(event)
    await session.flush()
    return event


async def list_job_events(
    session: AsyncSession,
    job_id: uuid.UUID,
    *,
    after_seq: int = 0,
    limit: int = 200,
) -> list[JobEvent]:
    return list(
        (
            await session.scalars(
                select(JobEvent)
                .where(JobEvent.job_id == job_id, JobEvent.seq > after_seq)
                .order_by(JobEvent.seq)
                .limit(limit)
            )
        ).all()
    )


async def record_llm_call(
    session: AsyncSession,
    *,
    project_id: uuid.UUID | None,
    job_id: uuid.UUID | None,
    role: str,
    model: str,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cost_estimate: float | None = None,
    latency_ms: int | None = None,
    error_code: str | None = None,
) -> LlmCallLog:
    """成本记账（设计 §4.9）。所有 LLM 调用都要在此留痕。"""
    row = LlmCallLog(
        project_id=project_id,
        job_id=job_id,
        role=role,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_estimate=cost_estimate,
        latency_ms=latency_ms,
        error_code=error_code,
    )
    session.add(row)
    await session.flush()
    return row


async def project_llm_cost(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> dict[str, Any]:
    row = (
        await session.execute(
            select(
                func.count(LlmCallLog.id),
                func.coalesce(func.sum(LlmCallLog.input_tokens), 0),
                func.coalesce(func.sum(LlmCallLog.output_tokens), 0),
                func.coalesce(func.sum(LlmCallLog.cost_estimate), 0.0),
                func.count(LlmCallLog.error_code),
            ).where(LlmCallLog.project_id == project_id)
        )
    ).one()
    return {
        "call_count": int(row[0] or 0),
        "input_tokens": int(row[1] or 0),
        "output_tokens": int(row[2] or 0),
        "cost_estimate": float(row[3] or 0.0),
        "failed_call_count": int(row[4] or 0),
    }
