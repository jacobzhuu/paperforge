"""任务运行上下文：会话、SSE 事件、checkpoint、LLM 记账。

Draft-first 的执行骨架都在这里：``stage()`` 上下文管理器保证任何阶段异常都被
捕获、写 checkpoint、发事件并继续；只有调用方显式判断「没有可交付产物」时
才把任务标记为 failed。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx
from db import (
    append_job_event,
    get_project,
    record_llm_call,
    update_job,
)
from db.session import make_engine, make_session_factory
from llm_runtime import LLMCallRecord, LLMConfig, LLMRunner
from observability import get_logger
from scholar_gateway import InMemoryHttpCache, SqlAlchemyHttpCache
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from paperforge_worker.config import WorkerSettings

logger = get_logger(__name__)

_TERMINAL_JOB_STATUSES = frozenset({"succeeded", "failed", "cancelled"})


@dataclass
class JobContext:
    """一次 generation_job 执行期间共享的资源与进度记录。"""

    project_id: uuid.UUID
    job_id: uuid.UUID | None
    settings: WorkerSettings
    session_factory: async_sessionmaker[AsyncSession]
    http_client: httpx.Client
    scholar_cache: Any
    owner_id: uuid.UUID | None = None
    llm_calls: list[LLMCallRecord] = field(default_factory=list)
    checkpoint: dict[str, Any] = field(default_factory=dict)
    warnings: list[dict[str, Any]] = field(default_factory=list)

    def llm_runner(self, *, config: LLMConfig | None = None) -> LLMRunner:
        """构造带记账回调的 runner；所有管线只能通过它调用 LLM（设计 §4.9）。"""
        return LLMRunner(
            config or self.settings.llm_config(),
            on_call=self.llm_calls.append,
        )

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

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
                    input_tokens=record.input_tokens,
                    output_tokens=record.output_tokens,
                    cost_estimate=record.cost_estimate,
                    latency_ms=record.latency_ms,
                    error_code=record.error_code,
                )

    def warn(self, stage: str, reason: str, detail: dict[str, Any] | None = None) -> None:
        """记录降级标记：任何阶段失败都留痕，但不阻断交付（draft-first）。"""
        self.warnings.append({"stage": stage, "reason": reason, **(detail or {})})


async def _load_job(session: AsyncSession, job_id: uuid.UUID):
    from db.models.paper import GenerationJob

    return await session.get(GenerationJob, job_id)


@asynccontextmanager
async def job_context(
    *,
    project_id: uuid.UUID,
    job_id: uuid.UUID | None,
    settings: WorkerSettings,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    http_client: httpx.Client | None = None,
    scholar_cache: Any | None = None,
) -> AsyncIterator[JobContext]:
    owns_engine = session_factory is None
    engine = make_engine(settings.database_url) if owns_engine else None
    factory = session_factory or make_session_factory(engine)
    owns_client = http_client is None
    client = http_client or httpx.Client(follow_redirects=True)
    cache = scholar_cache or InMemoryHttpCache()
    async with factory() as session:
        project = await get_project(session, project_id)
        if project is None:
            raise ValueError("project not found")
        owner_id = project.owner_id
    context = JobContext(
        project_id=project_id,
        job_id=job_id,
        settings=settings,
        session_factory=factory,
        http_client=client,
        scholar_cache=cache,
        owner_id=owner_id,
    )
    try:
        yield context
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
    task = asyncio.ensure_future(coro)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        return None


async def _mark_interrupted(context: JobContext, error: BaseException) -> None:
    """把被中断的任务落到 failed，并留下一条能解释「为什么没了下文」的事件。"""
    if context.job_id is None:
        return
    reason = type(error).__name__
    try:
        await _shielded(_write_interrupted(context, reason))
    except Exception:  # noqa: BLE001 - 已经在异常路径上，不再制造新的异常
        logger.warning("failed to mark job interrupted", extra={"reason": reason}, exc_info=True)


async def _write_interrupted(context: JobContext, reason: str) -> None:
    if context.job_id is None:
        return
    async with context.session() as session:
        job = await _load_job(session, context.job_id)
        # 已经收过尾的任务不碰：run_full_pipeline 里文献管线先跑完自己那段，
        # 之后阶段抛异常时不该把一个已 succeeded 的任务改写成 failed。
        if job is None or job.status in _TERMINAL_JOB_STATUSES:
            return
        await append_job_event(
            session,
            job_id=job.id,
            event_type="job.interrupted",
            payload={"reason": reason, "warnings": context.warnings},
        )
        await update_job(
            session,
            job,
            status="failed",
            error={"reason": "interrupted", "exception": reason, "warnings": context.warnings},
        )


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
