"""检索留痕仓储（设计 §4.3 search_run：轻量复现，非守恒账本）。"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models.paper import SearchRun


async def record_search_run(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    provider: str,
    query_text: str,
    filters: dict[str, Any] | None = None,
    hit_count: int | None = None,
    retrieved_count: int | None = None,
    status: str = "succeeded",
    error: str | None = None,
) -> SearchRun:
    run = SearchRun(
        project_id=project_id,
        provider=provider,
        query_text=query_text,
        filters_json=filters,
        hit_count=hit_count,
        retrieved_count=retrieved_count,
        status=status,
        error=error,
    )
    session.add(run)
    await session.flush()
    return run


async def list_search_runs(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    limit: int = 100,
) -> list[SearchRun]:
    return list(
        (
            await session.scalars(
                select(SearchRun)
                .where(SearchRun.project_id == project_id)
                .order_by(SearchRun.executed_at.desc())
                .limit(limit)
            )
        ).all()
    )
