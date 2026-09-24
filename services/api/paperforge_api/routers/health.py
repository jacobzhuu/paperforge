from __future__ import annotations

from fastapi import APIRouter, Response
from observability import render_metrics

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/metrics")
async def metrics() -> Response:
    from datetime import UTC, datetime

    from db.models.paper import GenerationJob
    from observability.metrics import JOB_COUNTS, OLDEST_QUEUED
    from sqlalchemy import func, select

    from paperforge_api.deps import get_session_factory

    async with get_session_factory()() as session:
        counts = dict(
            (
                await session.execute(
                    select(GenerationJob.status, func.count()).group_by(GenerationJob.status)
                )
            ).all()
        )
        oldest = await session.scalar(
            select(func.min(GenerationJob.created_at)).where(GenerationJob.status == "queued")
        )
    for status in (
        "queued",
        "running",
        "paused",
        "succeeded",
        "failed",
        "cancelled",
        "needs_input",
    ):
        JOB_COUNTS.labels(status=status).set(counts.get(status, 0))
    OLDEST_QUEUED.set((datetime.now(UTC) - oldest).total_seconds() if oldest else 0)
    body, content_type = render_metrics()
    return Response(content=body, media_type=content_type)
