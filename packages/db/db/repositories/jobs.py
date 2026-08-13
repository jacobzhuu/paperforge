"""任务与事件仓储：checkpoint + SSE 事件源（设计 §4.3 generation_job / job_event）。

Draft-first：任何阶段失败都写 checkpoint + 事件，并保留当前最好稿。
严谨质量门在有界修复后仍未收敛时使用 needs_input，不生成新导出件。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models.paper import GenerationJob, JobEvent, LlmCallLog

JOB_KINDS = frozenset(
    {
        "search",
        "ingest",
        "cards",
        "qdecomp",
        "evidence",
        "qmatrix",
        "synth",
        "outline",
        "write",
        "compile",
        "visual",
        "full",
    }
)
JOB_STATUSES = frozenset(
    {"queued", "running", "paused", "succeeded", "failed", "cancelled", "needs_input"}
)

# 「这一轮运行已经结束」的状态。paused 也在内：任务本身没做完，但这一轮确实停了，
# SSE 流要关、前端进度条要收，续跑靠新建一个 job（见 JOB_RESUME_KEY）。
FINISHED_JOB_STATUSES = frozenset({"paused", "succeeded", "failed", "cancelled", "needs_input"})
JOB_EVENT_CHANNEL_PREFIX = "paperforge:job-events:"


def job_event_channel(job_id: uuid.UUID) -> str:
    """Return the shared Redis wake-up channel for one durable DB event stream."""
    return f"{JOB_EVENT_CHANNEL_PREFIX}{job_id}"


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
        if status in FINISHED_JOB_STATUSES:
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
# 一键全流程的首稿完成后，润色变成用户显式决定的独立任务。
# 这些键由 API、worker 和前端共同读取，放在数据层避免三处各写一个魔法字符串。
POLISH_DECISION_KEY = "polish_decision"
POLISH_JOB_ID_KEY = "polish_job_id"
POLISH_SOURCE_JOB_KEY = "polish_source_job_id"
WRITE_DOCUMENT_KEY = "write_document_id"

# 质量修复和润色同形：一键全流程只做一次评估就交付，要不要为发现项再花一轮
# 「重写未达标章节 + 全文重新评估」由用户跑完后决定。QUALITY_FINDING_COUNT_KEY
# 让概览页不必重新拉一遍质量报告就能说清「有几处待处理」。
QUALITY_REPAIR_DECISION_KEY = "quality_repair_decision"
QUALITY_REPAIR_JOB_ID_KEY = "quality_repair_job_id"
QUALITY_REPAIR_SOURCE_JOB_KEY = "quality_repair_source_job_id"
QUALITY_FINDING_COUNT_KEY = "quality_finding_count"


async def request_polish_skip(session: AsyncSession, job: GenerationJob) -> GenerationJob:
    """请求跳过剩余的连贯性润色。幂等；worker 写完当前这一节后才会看到。"""
    return await update_job(session, job, checkpoint={POLISH_SKIP_KEY: True})


def polish_skip_requested(job: GenerationJob | None) -> bool:
    return bool(job is not None and (job.checkpoint_json or {}).get(POLISH_SKIP_KEY))


# 用户点「取消」/「暂停」时写进 checkpoint 的开关，与 POLISH_SKIP_KEY 同一套路：
# API 写、worker 在安全点读。真正的进程级冻结做不到——所有 LLM/检索调用都在
# asyncio.to_thread 里，取消 await 点停不掉底层线程——所以只能协作式停止：
# 正在跑的那一步做完再退出，绝不打断已经付过钱的调用。
JOB_CONTROL_KEY = "control"
#: 「继续」需要知道当初是哪个管线、带什么参数入的队。建 job 时一并落库。
JOB_RESUME_KEY = "resume"
#: 续跑 job 指回它续的是哪一条，便于排查「同一件事怎么有三个 job」。
JOB_RESUMED_FROM_KEY = "resumed_from"

JOB_CONTROL_MODES = frozenset({"cancel", "pause"})


async def request_job_stop(
    session: AsyncSession,
    job: GenerationJob,
    *,
    mode: str,
) -> GenerationJob:
    """请求停止任务。幂等；worker 跑完当前这一步（阶段/章节/条目）才会看到。"""
    if mode not in JOB_CONTROL_MODES:
        raise ValueError(f"unsupported job control mode: {mode}")
    return await update_job(session, job, checkpoint={JOB_CONTROL_KEY: mode})


def job_stop_requested(job: GenerationJob | None) -> str | None:
    """返回 "cancel" / "pause" / None。"""
    if job is None:
        return None
    mode = (job.checkpoint_json or {}).get(JOB_CONTROL_KEY)
    return mode if mode in JOB_CONTROL_MODES else None


def job_resume_spec(job: GenerationJob | None) -> dict[str, Any] | None:
    """取「怎么重放这条任务」：{"function": str, "kwargs": {...}}。"""
    if job is None:
        return None
    spec = (job.checkpoint_json or {}).get(JOB_RESUME_KEY)
    if not isinstance(spec, dict) or not isinstance(spec.get("function"), str):
        return None
    kwargs = spec.get("kwargs")
    return {"function": spec["function"], "kwargs": kwargs if isinstance(kwargs, dict) else {}}


def resume_checkpoint(job: GenerationJob) -> dict[str, Any]:
    """播种给续跑 job 的 checkpoint：带走全部阶段产物，但不带上一轮的停止开关。"""
    seeded = {
        key: value
        for key, value in (job.checkpoint_json or {}).items()
        # 停止开关不能继承，否则续跑的 job 一启动就又把自己停了。
        # 润色跳过也不继承：那是针对上一轮的一次性决定。
        if key not in {JOB_CONTROL_KEY, POLISH_SKIP_KEY}
    }
    seeded[JOB_RESUMED_FROM_KEY] = str(job.id)
    return seeded


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


async def latest_stage_event_payload(
    session: AsyncSession,
    project_id: uuid.UUID,
    stage: str,
) -> dict[str, Any] | None:
    """Return payload from the most recent ``{stage}.completed`` event for a project."""
    event_type = f"{stage}.completed"
    payload = (
        await session.execute(
            select(JobEvent.payload_json)
            .join(GenerationJob, GenerationJob.id == JobEvent.job_id)
            .where(
                GenerationJob.project_id == project_id,
                JobEvent.event_type == event_type,
            )
            .order_by(JobEvent.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if isinstance(payload, dict) and payload:
        return payload
    for job in await list_jobs(session, project_id, limit=30):
        checkpoint = job.checkpoint_json or {}
        stage_payload = checkpoint.get(stage)
        if isinstance(stage_payload, dict) and stage_payload:
            return stage_payload
    return None


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
    provider: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cost_estimate: float | None = None,
    latency_ms: int | None = None,
    error_code: str | None = None,
    metadata: dict[str, Any] | None = None,
    occurred_at: datetime | None = None,
) -> LlmCallLog:
    """成本记账（设计 §4.9）。所有 LLM 调用都要在此留痕。"""
    row = LlmCallLog(
        project_id=project_id,
        job_id=job_id,
        role=role,
        model=model,
        provider=provider,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_estimate=cost_estimate,
        latency_ms=latency_ms,
        error_code=error_code,
        metadata_json=metadata,
        occurred_at=occurred_at or datetime.now(UTC),
    )
    session.add(row)
    await session.flush()
    return row


def unpriced_call_count() -> Any:
    """未能算出金额的**成功**调用数。

    失败调用没有用量也不该有费用，不算进来；剩下的两种成因——没配价格、
    provider 没回 usage——都意味着「不知道花了多少」，在面板上必须与「花了 0」分开。
    """
    return func.count(1).filter(
        LlmCallLog.error_code.is_(None),
        LlmCallLog.cost_estimate.is_(None),
    )


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
                func.count(LlmCallLog.cost_estimate),
                unpriced_call_count(),
            ).where(LlmCallLog.project_id == project_id)
        )
    ).one()
    unpriced = int(row[6] or 0)
    return {
        "call_count": int(row[0] or 0),
        "input_tokens": int(row[1] or 0),
        "output_tokens": int(row[2] or 0),
        "cost_estimate": float(row[3] or 0.0),
        "failed_call_count": int(row[4] or 0),
        "priced_call_count": int(row[5] or 0),
        "unpriced_call_count": unpriced,
        # 金额是否覆盖了全部成功调用。为假时 `cost_estimate` 是**下界**，
        # 界面必须照实说，否则未定价的模型会让账单看起来便宜得多。
        "cost_complete": unpriced == 0,
    }
