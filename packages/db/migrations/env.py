"""Alembic 环境。

约定沿用自 DeepSearch（target_metadata 取 db.base.Base.metadata，导入 db.models 触发注册）。
连接串统一走 DATABASE_URL（asyncpg），迁移在 async engine 上执行。
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

import db.models  # noqa: F401 - 导入以注册全部 19 张表
from alembic import context
from db.base import Base
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

DEFAULT_DATABASE_URL = "postgresql+asyncpg://paperforge:paperforge@localhost:15432/paperforge"


def _database_url() -> str:
    return (
        os.environ.get("DATABASE_URL")
        or config.get_main_option("sqlalchemy.url")
        or (DEFAULT_DATABASE_URL)
    )


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations() -> None:
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = _database_url()
    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(_run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
