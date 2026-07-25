"""共享测试夹具：真实 PostgreSQL 会话。

R1 白名单、bibtex_key 唯一性、job_event 序号等不变量都活在 SQL 里，
因此这些契约测试跑真实 Postgres 而不是内存替身。数据库不可达时整体 skip，
以免本地未起基建的开发者被卡住（CI 起了 postgres service，因此始终会跑）。

asyncpg 连接与事件循环绑定，故 engine 按用例创建/销毁；建表只在会话开始做一次。
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

DEFAULT_TEST_DATABASE_URL = (
    "postgresql+asyncpg://paperforge:paperforge@localhost:15432/paperforge_test"
)


def test_database_url() -> str:
    return os.environ.get("PAPERFORGE_TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL)


async def _create_database_if_missing(url: str) -> bool:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    admin_url, _, database = url.rpartition("/")
    engine = create_async_engine(f"{admin_url}/postgres", isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            exists = await connection.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": database},
            )
            if not exists:
                await connection.execute(text(f'CREATE DATABASE "{database}"'))
        return True
    except Exception:  # noqa: BLE001 - 本地无 Postgres 时优雅跳过
        return False
    finally:
        await engine.dispose()


async def _create_schema(url: str) -> None:
    import db.models  # noqa: F401 - 注册全部 19 张表
    from db.base import Base
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
def pg_database_url() -> str:
    url = test_database_url()
    if not asyncio.run(_create_database_if_missing(url)):
        pytest.skip("PostgreSQL not reachable; set PAPERFORGE_TEST_DATABASE_URL to enable")
    asyncio.run(_create_schema(url))
    return url


@pytest_asyncio.fixture
async def pg_engine(pg_database_url: str):
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(pg_database_url)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def session_factory(pg_engine):
    """每个测试用例前清空业务表，保证用例间互不干扰。"""
    from db.base import Base
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker

    table_names = ", ".join(f'"{name}"' for name in Base.metadata.tables)
    async with pg_engine.begin() as connection:
        await connection.execute(text(f"TRUNCATE {table_names} RESTART IDENTITY CASCADE"))
    return async_sessionmaker(pg_engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def session(session_factory) -> AsyncIterator:
    async with session_factory() as session:
        yield session
        await session.commit()


@pytest.fixture
def project_id() -> uuid.UUID:
    return uuid.uuid4()


async def _truncate(url: str) -> None:
    from db.base import Base
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    table_names = ", ".join(f'"{name}"' for name in Base.metadata.tables)
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f"TRUNCATE {table_names} RESTART IDENTITY CASCADE"))
    finally:
        await engine.dispose()


@pytest.fixture
def clean_pg_database_url(pg_database_url: str) -> str:
    """同步夹具：给自带事件循环的测试（如 FastAPI TestClient）用。

    asyncpg 连接与事件循环绑定，因此这里不返回 engine/session，只保证库是干净的，
    由被测组件在自己的循环里建连接。
    """
    asyncio.run(_truncate(pg_database_url))
    return pg_database_url


def run_async(coro):
    """在独立事件循环里跑一段异步准备代码（测试数据播种用）。"""
    return asyncio.run(coro)
