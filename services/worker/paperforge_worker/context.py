"""任务运行上下文：会话、SSE 事件、checkpoint、LLM 记账。

Draft-first 的执行骨架都在这里：``stage()`` 上下文管理器保证任何阶段异常都被
捕获、写 checkpoint、发事件并继续；只有调用方显式判断「没有可交付产物」时
才把任务标记为 failed。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from typing import Any

import httpx
from db import (
    FINISHED_JOB_STATUSES,
    append_job_event,
    get_project,
    job_event_channel,
    job_resume_spec,
    job_stop_requested,
    record_llm_call,
    update_job,
)
from db.session import make_engine, make_session_factory
from llm_runtime import LLMCallRecord, LLMConfig, LLMRunner
from observability import get_logger
from scholar_gateway import InMemoryHttpCache, SqlAlchemyHttpCache
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from paperforge_worker.config import WorkerSettings

logger = get_logger(__name__)

# 「已经收过尾」的状态，不该再被改写。定义在数据层（db.repositories.jobs），
# 这里只取别名——paused 也在内：暂停是用户的主动选择，之后任何收尾逻辑都不能
# 把它翻成 failed，否则「继续」按钮就没了。
_TERMINAL_JOB_STATUSES = FINISHED_JOB_STATUSES

_PDF_JOB_RECOVERY = {
    "run_pdf_match_pipeline": (frozenset({"matching"}), "match_failed"),
    "match_literature_pdf": (frozenset({"matching"}), "match_failed"),
    "run_uploaded_pdf_pipeline": (
        frozenset({"parsing", "extracting"}),
        "parse_failed",
    ),
    "parse_literature_pdf": (
        frozenset({"parsing", "extracting"}),
        "parse_failed",
    ),
}


@asynccontextmanager
async def _committing_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """提交/回滚语义与 `JobContext.session()` 相同，但不需要 context。

    收尾写入可能发生在 context 建好之前（进入阶段被取消），因此这段逻辑必须
    只依赖 session factory。
    """
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


#: 停止开关的轮询节流窗口（秒）。检查点埋在分节/逐条循环里，
#: 不节流就会变成每写一节打好几次库。
_STOP_POLL_TTL_SECONDS = 2.0


def _failure_signal(
    event_type: str,
    payload: dict[str, Any],
    stage: str | None,
) -> dict[str, Any] | None:
    """Extract a compact, persistable reason from a stage event that reports a failure.

    Stages signal trouble in two shapes: an explicit ``status: "failed"`` or an ``error_code`` /
    ``error`` field.  Both are kept small on purpose — this ends up in ``generation_job.error_json``
    and is meant to name the cause, not to duplicate the event stream.
    """
    error_code = payload.get("error_code") or payload.get("error")
    if str(payload.get("status") or "") != "failed" and not error_code:
        return None
    signal: dict[str, Any] = {"event_type": event_type}
    if stage:
        signal["stage"] = stage
    if error_code:
        signal["error_code"] = str(error_code)[:200]
    for key in ("message", "detail", "reason"):
        value = payload.get(key)
        if value:
            signal[key] = str(value)[:300]
            break
    return signal


async def _publish_job_event(publisher: Any | None, job_id: uuid.UUID | None) -> None:
    """Best-effort Redis wake-up; the committed database row remains authoritative."""
    if publisher is None or job_id is None:
        return
    try:
        await publisher.publish(job_event_channel(job_id), "1")
    except Exception:  # noqa: BLE001 - losing a wake-up must not fail a generation job
        logger.warning(
            "job event notification failed",
            extra={"job_id": str(job_id)},
            exc_info=True,
        )


class JobStopped(Exception):
    """用户请求停止本次运行。

    不是失败：不记降级 warning、不进 `context.warnings`、也不该让 arq 打错误栈。
    ``mode`` 决定收尾姿势——``cancel`` 落 cancelled，``pause`` 落 paused 并保留
    checkpoint 供「继续」续跑。
    """

    def __init__(self, mode: str) -> None:
        super().__init__(f"job stopped by user: {mode}")
        self.mode = mode


@dataclass
class JobContext:
    """一次 generation_job 执行期间共享的资源与进度记录。"""

    project_id: uuid.UUID
    job_id: uuid.UUID | None
    settings: WorkerSettings
    session_factory: async_sessionmaker[AsyncSession]
    http_client: httpx.Client
    scholar_cache: Any
    event_publisher: Any | None = field(default=None, repr=False)
    owner_id: uuid.UUID | None = None
    llm_calls: list[LLMCallRecord] = field(default_factory=list)
    #: 本次运行开始时 job 已有的 checkpoint（续跑 job 会被播种上一轮的阶段产物），
    #: 加上本次运行陆续写进去的。`_run_stage` 据此判断哪些阶段可以跳过。
    checkpoint: dict[str, Any] = field(default_factory=dict)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    #: Semantic claim verification can run several times while one quality-repair job converges.
    #: Cache exact claim/excerpt verdicts for this job so unchanged anchors do not incur repeated
    #: model calls.  Keys include the verifier version and source text hash (see quality.py).
    claim_verification_cache: dict[str, dict[str, Any]] = field(
        default_factory=dict,
        repr=False,
    )
    #: The most recent failure a stage reported through ``emit``.  ``_finish`` persists it so a
    #: failed job explains itself on its own row instead of only in the event stream.
    last_failure: dict[str, Any] | None = field(default=None, repr=False)
    #: 停止开关的轮询缓存：(读到的时刻, 结果)。
    _stop_cache: tuple[float, str | None] | None = field(default=None, repr=False)

    def llm_runner(self, *, config: LLMConfig | None = None) -> LLMRunner:
        """构造带记账回调的 runner；所有管线只能通过它调用 LLM（设计 §4.9）。"""
        return LLMRunner(
            config or self.settings.llm_config(),
            on_call=self.llm_calls.append,
        )

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with _committing_session(self.session_factory) as session:
            yield session

    async def emit(
        self,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        stage: str | None = None,
        progress: float | None = None,
        checkpoint: dict[str, Any] | None = None,
    ) -> None:
        """写 job_event（SSE 源）并顺带更新任务阶段/进度/checkpoint。"""
        if checkpoint:
            self.checkpoint.update(checkpoint)
        # Stages already report *why* they failed, but only into the event stream; the terminal
        # job row kept none of it.  Remember the last such signal so `_finish` can persist it.
        # ``job.finished`` is skipped deliberately: it is the generic terminal event and would
        # otherwise overwrite the specific stage failure with "failed, no detail".
        if payload and event_type != "job.finished":
            signal = _failure_signal(event_type, payload, stage)
            if signal is not None:
                self.last_failure = signal
        if self.job_id is None:
            logger.info("job_event", extra={"event_type": event_type, "payload": payload})
            return
        async with self.session() as session:
            job = await _load_job(session, self.job_id)
            if job is not None:
                await update_job(
                    session,
                    job,
                    stage=stage,
                    progress=progress,
                    checkpoint=checkpoint,
                )
            await append_job_event(
                session,
                job_id=self.job_id,
                event_type=event_type,
                payload=payload or {},
            )
        # Publish only after the transaction commits. Publishing inside the session block lets an
        # API subscriber wake before the row is visible and then sleep through the real event.
        await self.notify_event()

    async def notify_event(self) -> None:
        """Wake SSE subscribers after a committed event or status-only transition."""
        await _publish_job_event(self.event_publisher, self.job_id)

    async def flush_llm_calls(self) -> None:
        """把本次运行的 LLM 调用写入 llm_call_log（成本面板数据源）。"""
        if not self.llm_calls:
            return
        pending = list(self.llm_calls)
        self.llm_calls.clear()
        async with self.session() as session:
            for record in pending:
                await record_llm_call(
                    session,
                    project_id=self.project_id,
                    job_id=self.job_id,
                    role=record.role,
                    model=record.model,
                    provider=record.provider,
                    input_tokens=record.input_tokens,
                    output_tokens=record.output_tokens,
                    cost_estimate=record.cost_estimate,
                    latency_ms=record.latency_ms,
                    error_code=record.error_code,
                    metadata=record.metadata,
                    occurred_at=record.occurred_at,
                )

    async def stop_requested(self) -> str | None:
        """用户是否请求了停止（标记写在 job.checkpoint_json 上）。

        返回 "cancel" / "pause" / None。带 2 秒节流：调用点埋在分节与逐条循环里，
        每次都打库是纯浪费。读不到就当没按过——把开关读失败升级成任务中断，
        比漏掉一次停止请求糟糕得多。
        """
        if self.job_id is None:
            return None
        now = time.monotonic()
        if self._stop_cache is not None and now - self._stop_cache[0] < _STOP_POLL_TTL_SECONDS:
            return self._stop_cache[1]
        try:
            async with self.session() as session:
                mode = job_stop_requested(await _load_job(session, self.job_id))
        except Exception:  # noqa: BLE001 - 读不到开关就继续跑
            logger.warning("failed to read job control flag", exc_info=True)
            return None
        self._stop_cache = (now, mode)
        return mode

    async def raise_if_stopped(self) -> None:
        """在安全点调用：用户按过停止就抛 JobStopped，当前这一步的产物已经落库。"""
        mode = await self.stop_requested()
        if mode is not None:
            raise JobStopped(mode)

    def stage_completed(self, stage: str) -> bool:
        """该阶段在此前某一轮里已经成功跑完（`_run_stage` 成功时写 `{stage: payload}`）。

        只认成功：`{stage}_failed` 是降级标记，说明那一轮这个阶段挂了，续跑要重跑它。
        """
        if f"{stage}_failed" in self.checkpoint:
            return False
        value = self.checkpoint.get(stage)
        return isinstance(value, dict) or value is True

    def stage_payload(self, stage: str) -> dict[str, Any]:
        """已完成阶段落在 checkpoint 里的 `to_payload()`。跳过该阶段时用它补下游标量。"""
        value = self.checkpoint.get(stage)
        return dict(value) if isinstance(value, dict) else {}

    def warn(self, stage: str, reason: str, detail: dict[str, Any] | None = None) -> None:
        """记录降级标记：任何阶段失败都留痕，但不阻断交付（draft-first）。"""
        self.warnings.append({"stage": stage, "reason": reason, **(detail or {})})


async def _load_job(session: AsyncSession, job_id: uuid.UUID):
    from db.models.paper import GenerationJob

    return await session.get(GenerationJob, job_id)


async def _lock_job(session: AsyncSession, job_id: uuid.UUID):
    from db.models.paper import GenerationJob

    return await session.scalar(
        select(GenerationJob).where(GenerationJob.id == job_id).with_for_update()
    )


@asynccontextmanager
async def job_context(
    *,
    project_id: uuid.UUID,
    job_id: uuid.UUID | None,
    settings: WorkerSettings,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    http_client: httpx.Client | None = None,
    scholar_cache: Any | None = None,
    event_publisher: Any | None = None,
) -> AsyncIterator[JobContext]:
    owns_engine = session_factory is None
    engine = (
        make_engine(settings.database_url, application_name="paperforge-worker-job")
        if owns_engine
        else None
    )
    factory = session_factory or make_session_factory(engine)
    owns_client = http_client is None
    client = http_client or httpx.Client(follow_redirects=True)
    cache = scholar_cache or InMemoryHttpCache()
    try:
        async with factory() as session:
            project = await get_project(session, project_id)
            if project is None:
                raise ValueError("project not found")
            owner_id = project.owner_id
            # 续跑 job 的 checkpoint 已被播种上一轮的阶段产物；此前这里只写不读，
            # 于是「断点」永远没人看。读进来 `_run_stage` 才能跳过已完成的阶段。
            seeded_job = await _load_job(session, job_id) if job_id else None
            seeded = dict(seeded_job.checkpoint_json or {}) if seeded_job is not None else {}
    except (Exception, asyncio.CancelledError) as error:
        # 进入阶段也会 await（两次数据库往返）。worker 停机时的 task.cancel() 可能
        # 正好落在这里——此前这一段完全没有收尾，任务就永远停在 running，
        # 正是本模块要防的那种僵尸，只是窗口更窄所以更难复现。
        pending = await _mark_job_interrupted(
            factory,
            job_id,
            type(error).__name__,
            event_publisher=event_publisher,
        )
        if owns_client:
            client.close()
        if engine is not None:
            await _shielded(_dispose_after(pending, engine))
        raise
    context = JobContext(
        project_id=project_id,
        job_id=job_id,
        settings=settings,
        session_factory=factory,
        http_client=client,
        scholar_cache=cache,
        event_publisher=event_publisher,
        owner_id=owner_id,
        checkpoint=seeded,
    )
    try:
        # 这里**不能**先查停止开关再 yield：@asynccontextmanager 的生成器一旦在
        # yield 之前抛异常，__aenter__ 会变成 RuntimeError("generator didn't yield")。
        # 排队期间被取消的任务由第一个 `_run_stage` 的检查点兜住——所有管线的第一个
        # 动作都是 `_run_stage`，代价只是多跑一次 `_mark_running`（它有终态守卫）。
        yield context
    except JobStopped as stop:
        # 不 re-raise：用户主动停止不是失败，抛出去只会让 arq 记一条错误栈，
        # 也会让 `_mark_interrupted` 把任务改写成 failed。异常在这里被吞掉后，
        # `async with` 块剩下的代码不再执行，各 `run_*_pipeline` 返回 None——
        # run_full_pipeline 依赖这一点做早退（它分两段开 job_context）。
        await _mark_stopped(context, stop.mode)
    except (Exception, asyncio.CancelledError) as error:
        # CancelledError 必须单列（它不是 Exception）：arq 的 job_timeout 走
        # asyncio.wait_for、worker 停机走 task.cancel()，两条路都绕开 `_finish`，
        # 任务于是永远停在 running——前端的「进行中」再也不消失，用户也分不清
        # 这轮到底是跑完了还是被掐了。GeneratorExit/KeyboardInterrupt 不接：
        # 那种场景下再 await 只会把退出流程搅得更乱。
        await _mark_interrupted(context, error)
        raise
    finally:
        # 取消只投递一次，被取消后继续 await 通常还能跑完；但再来一次取消
        # （worker 连收两个停机信号就是这样）会把收尾打断。收尾动作因此都放进
        # 独立 task 并 shield，免得这一轮的 LLM 记账——成本面板的数据源——跟着丢。
        try:
            await _shielded(context.flush_llm_calls())
        except Exception:  # noqa: BLE001 - 记账失败不该淹没任务本身的结果
            logger.warning("llm_call_log flush failed", exc_info=True)
        if owns_client:
            client.close()
        if engine is not None:
            await engine.dispose()


async def _shielded(coro: Any) -> Any:
    """把收尾动作放进独立 task 并 shield，外层再被取消也让它在后台把话说完。"""
    await _shielded_task(coro)


async def _shielded_task(coro: Any) -> asyncio.Task:
    """同 `_shielded`，但把 task 交出来，供调用方等它落定后再释放资源。"""
    task = asyncio.ensure_future(coro)
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        pass
    return task


async def _dispose_after(task: asyncio.Task | None, engine: Any) -> None:
    """等收尾写入落定再释放连接池。

    engine 一旦 dispose，那次还在后台跑的收尾写入就没有连接可用了——任务又会
    停在 running。
    """
    if task is not None:
        with suppress(Exception, asyncio.CancelledError):
            await task
    await engine.dispose()


#: 停止模式 → 落库状态。
_STOP_STATUS = {"cancel": "cancelled", "pause": "paused"}


async def _mark_stopped(context: JobContext, mode: str) -> None:
    """把用户停下的任务落到 cancelled/paused，并留一条能解释「为什么没下文」的事件。"""
    if context.job_id is None:
        return
    try:
        await _shielded(_write_stopped(context, mode))
    except Exception:  # noqa: BLE001 - 收尾失败不再制造新的异常
        logger.warning("failed to mark job stopped", extra={"mode": mode}, exc_info=True)


async def _write_stopped(context: JobContext, mode: str) -> None:
    if context.job_id is None:
        return
    status = _STOP_STATUS[mode]
    async with context.session() as session:
        # Lock job before upload. API cancellation uses the same order; reversing
        # it here would deadlock with a request that owns the job row and is
        # waiting to recover this upload. The lock also makes the terminal guard
        # authoritative when a duplicate worker is finishing concurrently.
        job = await _lock_job(session, context.job_id)
        if job is None or job.status in _TERMINAL_JOB_STATUSES:
            return
        await _recover_pdf_upload(
            session,
            job,
            termination=status,
            exception=None,
        )
        await append_job_event(
            session,
            job_id=job.id,
            event_type=f"job.{status}",
            payload={
                "stage": job.stage,
                "warnings": context.warnings,
                # 暂停时前端要显示「从哪一步接着跑」，取的就是这个。
                "resumable": mode == "pause",
            },
        )
        await update_job(session, job, status=status)
    await context.notify_event()


async def _mark_interrupted(context: JobContext, error: BaseException) -> None:
    """把被中断的任务落到 failed，并留下一条能解释「为什么没了下文」的事件。"""
    await _mark_job_interrupted(
        context.session_factory,
        context.job_id,
        type(error).__name__,
        warnings=context.warnings,
        event_publisher=context.event_publisher,
    )


async def _mark_job_interrupted(
    session_factory: async_sessionmaker[AsyncSession],
    job_id: uuid.UUID | None,
    reason: str,
    *,
    warnings: list[dict[str, Any]] | None = None,
    event_publisher: Any | None = None,
) -> asyncio.Task | None:
    """收尾的落库部分。

    只依赖 session factory 而不是 `JobContext`，因为进入阶段（context 还没建好）
    被取消时同样需要收尾。返回后台 task，调用方据此决定何时释放连接池。
    """
    if job_id is None:
        return None
    try:
        return await _shielded_task(
            _write_interrupted(
                session_factory,
                job_id,
                reason,
                warnings,
                event_publisher=event_publisher,
            )
        )
    except Exception:  # noqa: BLE001 - 已经在异常路径上，不再制造新的异常
        logger.warning("failed to mark job interrupted", extra={"reason": reason}, exc_info=True)
        return None


async def _write_interrupted(
    session_factory: async_sessionmaker[AsyncSession],
    job_id: uuid.UUID,
    reason: str,
    warnings: list[dict[str, Any]] | None = None,
    *,
    event_publisher: Any | None = None,
) -> None:
    context_warnings = warnings or []
    async with _committing_session(session_factory) as session:
        job = await _lock_job(session, job_id)
        # 已经收过尾的任务不碰：run_full_pipeline 里文献管线先跑完自己那段，
        # 之后阶段抛异常时不该把一个已 succeeded 的任务改写成 failed。
        if job is None or job.status in _TERMINAL_JOB_STATUSES:
            return
        await _recover_pdf_upload(
            session,
            job,
            termination="failed",
            exception=reason,
        )
        await append_job_event(
            session,
            job_id=job.id,
            event_type="job.interrupted",
            payload={"reason": reason, "warnings": context_warnings},
        )
        await update_job(
            session,
            job,
            status="failed",
            error={"reason": "interrupted", "exception": reason, "warnings": context_warnings},
        )
    await _publish_job_event(event_publisher, job_id)


async def _recover_pdf_upload(
    session: AsyncSession,
    job: Any,
    *,
    termination: str,
    exception: str | None,
) -> bool:
    """Move an abandoned PDF operation to a retryable state without regression."""
    target = _pdf_recovery_target(job)
    if target is None:
        return False
    upload_id, active_statuses, failed_status = target

    from db.models.library import LiteraturePdfUpload
    from db.models.paper import GenerationJob

    upload = await session.scalar(
        select(LiteraturePdfUpload)
        .where(
            LiteraturePdfUpload.id == upload_id,
            LiteraturePdfUpload.project_id == job.project_id,
        )
        .with_for_update()
    )
    if upload is None or upload.status not in active_statuses:
        return False

    # A retry creates a new job and moves the upload back into the same active
    # state.  If that newer job already exists, the old job no longer owns the
    # state and must not fail it underneath the new worker.
    newer_jobs = list(
        (
            await session.scalars(
                select(GenerationJob)
                .where(
                    GenerationJob.project_id == job.project_id,
                    GenerationJob.id != job.id,
                    GenerationJob.created_at >= job.created_at,
                )
                .order_by(GenerationJob.created_at.desc())
            )
        ).all()
    )
    if any(_pdf_job_upload_id(candidate) == upload_id for candidate in newer_jobs):
        return False

    upload.status = failed_status
    upload.error_json = {
        "reason": "worker_job_terminated",
        "retryable": True,
        "termination": termination,
        "exception": exception,
        "job_id": str(job.id),
    }
    return True


def _pdf_recovery_target(
    job: Any,
) -> tuple[uuid.UUID, frozenset[str], str] | None:
    spec = job_resume_spec(job)
    if spec is None:
        return None
    recovery = _PDF_JOB_RECOVERY.get(str(spec.get("function") or ""))
    if recovery is None:
        return None
    upload_id = _uuid_or_none((spec.get("kwargs") or {}).get("upload_id"))
    if upload_id is None:
        return None
    return upload_id, recovery[0], recovery[1]


def _pdf_job_upload_id(job: Any) -> uuid.UUID | None:
    target = _pdf_recovery_target(job)
    return target[0] if target is not None else None


def _uuid_or_none(value: Any) -> uuid.UUID | None:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def build_scholar_cache(settings: WorkerSettings) -> Any:
    """优先用 DB 持久化缓存（跨任务复用配额），不可用时退回进程内缓存。"""
    try:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        # SQLAlchemy 默认的 postgresql:// 走 psycopg2；本仓库装的是 psycopg 3。
        sync_url = settings.database_url.replace("+asyncpg", "+psycopg")
        engine = create_engine(sync_url, pool_pre_ping=True)
        # 立即探活：连不上就退回内存缓存，而不是等到第一次检索时才炸。
        with engine.connect():
            pass
        return SqlAlchemyHttpCache(sessionmaker(engine))
    except Exception as error:  # noqa: BLE001 - 无同步驱动/DB 时退回内存缓存
        logger.info(
            "falling back to in-memory scholar http cache",
            extra={"error": type(error).__name__},
        )
        return InMemoryHttpCache()


StageRunner = Callable[..., Any]
