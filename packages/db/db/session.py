from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


def make_engine(database_url: str, *, application_name: str = "paperforge"):
    """Create a bounded asyncpg engine suitable for blue/green deployment.

    A default QueuePool keeps five idle connections per process. This host
    intentionally drains many old API/worker containers, so those defaults can
    exhaust PostgreSQL even when no query is active. Keep only two warm
    connections; short concurrent card/matrix batches may use overflow slots,
    which are closed again when the batch finishes.

    ``max_overflow`` 从 6 提到 12（2026-09-07）。担心的东西没变——蓝绿部署时大量
    待排空容器会把 PostgreSQL 的连接数吃光——但吃光它的是**常驻**连接，而常驻的
    只有 ``pool_size=2``；overflow 槽用完即关，提高它不改变稳态占用。
    提高的理由是管线并发化：并发任务的落库与 ``context.emit`` 各要占一条，
    原来的 8 条上限在 ``card_concurrency`` 钳到 12 时就已经不够，
    表现为 30 秒的 ``pool_timeout`` 停顿而不是报错。
    """
    pool_size = int(os.environ.get("DB_POOL_SIZE", "2"))
    overflow = int(os.environ.get("DB_MAX_OVERFLOW", "12"))
    timeout = float(os.environ.get("DB_POOL_TIMEOUT", "30"))
    if pool_size < 1 or overflow < 0 or timeout <= 0:
        raise ValueError("database pools must have positive size/timeout and bounded overflow")
    return create_async_engine(
        database_url,
        pool_pre_ping=True,
        pool_size=pool_size,
        max_overflow=overflow,
        pool_timeout=timeout,
        pool_use_lifo=True,
        connect_args={"server_settings": {"application_name": application_name}},
        future=True,
    )


def make_session_factory(engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@asynccontextmanager
async def session_scope(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """事务性会话上下文：提交或回滚后关闭。"""
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
