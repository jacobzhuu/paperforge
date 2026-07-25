"""任务进度 SSE（设计 §4.7 `GET /projects/{id}/jobs/{job_id}/events`）。

单向进度流足够，免 WebSocket 复杂度（设计 §4.1）。事件源是 `job_event` 表，
按 seq 单调递增轮询；客户端断线重连时用 `Last-Event-ID` 续传。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from db import get_job, list_job_events
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import async_sessionmaker

from paperforge_api.deps import get_session_factory

router = APIRouter(prefix="/api/v1", tags=["events"])

POLL_INTERVAL_SECONDS = 0.5
# 空闲心跳：穿透代理的空闲超时，同时让前端知道连接仍然活着。
HEARTBEAT_SECONDS = 15.0
TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled"})


@router.get("/projects/{project_id}/jobs/{job_id}/events")
async def stream_job_events(
    project_id: str,
    job_id: str,
    request: Request,
    session_factory: Annotated[async_sessionmaker, Depends(get_session_factory)],
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    try:
        job_uuid = uuid.UUID(job_id)
        project_uuid = uuid.UUID(project_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail="job not found") from error

    async with session_factory() as session:
        job = await get_job(session, job_uuid)
        if job is None or job.project_id != project_uuid:
            raise HTTPException(status_code=404, detail="job not found")

    try:
        after_seq = int(last_event_id) if last_event_id else 0
    except ValueError:
        after_seq = 0

    return StreamingResponse(
        _event_stream(session_factory, job_uuid, after_seq, request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _event_stream(
    session_factory: async_sessionmaker,
    job_id: uuid.UUID,
    after_seq: int,
    request: Request,
) -> AsyncIterator[str]:
    idle = 0.0
    while True:
        if await request.is_disconnected():
            return
        async with session_factory() as session:
            events = await list_job_events(session, job_id, after_seq=after_seq)
            job = await get_job(session, job_id)
        if events:
            idle = 0.0
            for event in events:
                after_seq = event.seq
                yield _format_event(
                    event_id=event.seq,
                    event_type=event.event_type,
                    data={
                        "seq": event.seq,
                        "type": event.event_type,
                        "payload": event.payload_json or {},
                        "stage": job.stage if job else None,
                        "progress": job.progress if job else None,
                        "status": job.status if job else None,
                    },
                )
        else:
            idle += POLL_INTERVAL_SECONDS
            if idle >= HEARTBEAT_SECONDS:
                idle = 0.0
                yield ": heartbeat\n\n"

        if job is not None and job.status in TERMINAL_STATUSES and not events:
            yield _format_event(
                event_id=after_seq,
                event_type="job.closed",
                data={"status": job.status, "progress": job.progress, "stage": job.stage},
            )
            return
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


def _format_event(*, event_id: int, event_type: str, data: dict) -> str:
    return (
        f"id: {event_id}\n"
        f"event: {event_type}\n"
        f"data: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"
    )
