"""共享测试夹具：真实 PostgreSQL 会话。

R1 白名单、bibtex_key 唯一性、job_event 序号等不变量都活在 SQL 里，
因此这些契约测试跑真实 Postgres 而不是内存替身。数据库不可达时整体 skip，
以免本地未起基建的开发者被卡住（CI 起了 postgres service，因此始终会跑）。

asyncpg 连接与事件循环绑定，故 engine 按用例创建/销毁；建表只在会话开始做一次。
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

DEFAULT_TEST_DATABASE_URL = (
    "postgresql+asyncpg://paperforge:paperforge@localhost:15432/paperforge_test"
)

# 部署态环境变量必须与开发者本机隔离。这些变量改的是**应用行为**而非测试数据：
# 本机 .env 一旦切到生产配置（AUTH_COOKIE_SECURE=true + SMTP + HTTPS PUBLIC_APP_URL），
# create_app() 会在夹具里直接抛 RuntimeError，一次打挂 70 多个用例；填了真实
# provider 凭据则让 *_configured 断言翻车。CI 没有 .env，所以这类问题在 CI 上
# 永远不会暴露，只砸本地。需要验证生产配置的用例自己 monkeypatch 覆盖即可。
_DEPLOYMENT_ENV_DEFAULTS = {
    "JOB_DISPATCH_ENABLED": "false",
    "AUTH_COOKIE_SECURE": "false",
    "AUTH_DEV_LOGIN_ENABLED": "true",
    "AUTH_EMAIL_MODE": "file",
    "PUBLIC_APP_URL": "http://localhost:3000",
    "LLM_DEFAULT_PROVIDER": "noop",
    "LLM_OPENAI_API_KEY": "",
    "IMAGE_API_KEY": "",
    "IMAGE_ACCOUNT_ID": "",
    # 本机 .env 可能指向正式 MinIO。测试产物必须始终落到每用例自己的临时目录，
    # 否则下载/视觉测试会误连 localhost:19000，甚至污染正式对象存储。
    "STORAGE_BACKEND": "filesystem",
    # 默认 provider 是 yunwu，它的凭据走独立命名，也必须一起隔离。
    "YUNWU_API_KEY": "",
}

_PROXY_ENV_NAMES = (
    "ALL_PROXY",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "all_proxy",
    "http_proxy",
    "https_proxy",
)


@pytest.fixture(autouse=True)
def isolate_deployment_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    for name, value in _DEPLOYMENT_ENV_DEFAULTS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("STORAGE_FS_ROOT", str(tmp_path / "objects"))
    # API tests inject their queue dependency; lifespan must not contact a real
    # deployment Redis or sleep through ARQ's connection retries.
    async def no_live_queue(_settings):
        return None

    api_main = sys.modules.get("paperforge_api.main")
    if api_main is not None:
        monkeypatch.setattr(api_main, "create_arq_pool", no_live_queue)
    # 开发机可能通过 SOCKS 代理联网，而最小测试依赖没有安装 httpx[socks]。
    # 单元/集成测试使用本地替身，不应继承宿主代理；否则连一个请求都没发就初始化失败。
    for name in _PROXY_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


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
