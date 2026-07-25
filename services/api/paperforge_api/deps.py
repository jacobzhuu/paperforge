"""API 依赖：数据库会话与 ARQ 任务队列。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings
from db.session import make_engine, make_session_factory
from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from paperforge_api.config import Settings, get_settings

_engine = None
_session_factory = None


def get_session_factory():
    global _engine, _session_factory
    if _session_factory is None:
        settings = get_settings()
        _engine = make_engine(settings.database_url)
        _session_factory = make_session_factory(_engine)
    return _session_factory


async def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


async def get_session() -> AsyncIterator[AsyncSession]:
    """请求级会话：正常返回时提交，异常回滚。"""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_queue(request: Request) -> ArqRedis | None:
    """ARQ 连接池。Redis 不可用时返回 None，路由据此降级为 503 而不是崩溃。"""
    pool = getattr(request.app.state, "arq_pool", None)
    return pool


async def create_arq_pool(settings: Settings) -> Any:
    return await create_pool(RedisSettings.from_dsn(settings.redis_url))
