from __future__ import annotations

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
    """
    return create_async_engine(
        database_url,
        pool_pre_ping=True,
        pool_size=2,
        max_overflow=6,
        pool_timeout=30,
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
