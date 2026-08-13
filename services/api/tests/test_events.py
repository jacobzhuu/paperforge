from __future__ import annotations

import asyncio
import inspect
import json
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from paperforge_api import deps
from paperforge_api.routers import events as event_router


class _SessionContext:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *_args: Any) -> None:
        return None


class _SessionFactory:
    def __call__(self) -> _SessionContext:
        return _SessionContext()


class _Request:
    async def is_disconnected(self) -> bool:
        return False


class _PubSub:
    def __init__(self, order: list[str]) -> None:
        self.order = order
        self.messages: asyncio.Queue[dict[str, str]] = asyncio.Queue()
        self.closed = False

    async def subscribe(self, channel: str) -> None:
        self.order.append(f"subscribe:{channel}")

    async def unsubscribe(self, channel: str) -> None:
        self.order.append(f"unsubscribe:{channel}")

    async def aclose(self) -> None:
        self.closed = True

    async def get_message(self, **_kwargs: Any) -> dict[str, str] | None:
        return await self.messages.get()


class _EventBus:
    def __init__(self, pubsub: _PubSub) -> None:
        self._pubsub = pubsub

    def pubsub(self) -> _PubSub:
        return self._pubsub


class _IdlePubSub(_PubSub):
    async def get_message(self, **_kwargs: Any) -> None:
        return None


def _event(seq: int) -> SimpleNamespace:
    return SimpleNamespace(seq=seq, event_type=f"stage.{seq}", payload_json={"seq": seq})


def _sse_data(value: str) -> dict[str, Any]:
    data_line = next(line for line in value.splitlines() if line.startswith("data: "))
    return json.loads(data_line.removeprefix("data: "))


@pytest.mark.asyncio
async def test_pubsub_subscribes_before_replay_and_queries_only_after_wakeup(monkeypatch) -> None:
    """A healthy idle SSE connection must not continuously query the database."""
    job_id = uuid.uuid4()
    order: list[str] = []
    pubsub = _PubSub(order)
    available_seq = 1
    query_after: list[int] = []

    async def fake_list_events(_session: object, _job_id: uuid.UUID, *, after_seq: int, **_kw):
        order.append(f"query:{after_seq}")
        query_after.append(after_seq)
        return [_event(seq) for seq in range(after_seq + 1, available_seq + 1)]

    async def fake_get_job(_session: object, _job_id: uuid.UUID) -> SimpleNamespace:
        return SimpleNamespace(stage="write", progress=0.4, status="running")

    monkeypatch.setattr(event_router, "list_job_events", fake_list_events)
    monkeypatch.setattr(event_router, "get_job", fake_get_job)
    stream = event_router._event_stream(
        _SessionFactory(),  # type: ignore[arg-type]
        job_id,
        0,
        _Request(),  # type: ignore[arg-type]
        _EventBus(pubsub),  # type: ignore[arg-type]
    )

    first = _sse_data(await anext(stream))
    assert first["seq"] == 1
    assert order[:2] == [f"subscribe:{event_router.job_event_channel(job_id)}", "query:0"]
    assert query_after == [0]

    available_seq = 2
    pending = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)
    assert not pending.done()
    assert query_after == [0]
    await pubsub.messages.put({"type": "message"})
    second = _sse_data(await asyncio.wait_for(pending, timeout=1))
    assert second["seq"] == 2
    assert query_after == [0, 1]

    await stream.aclose()
    assert pubsub.closed
    assert order[-1] == f"unsubscribe:{event_router.job_event_channel(job_id)}"


@pytest.mark.asyncio
async def test_status_only_wakeup_closes_terminal_stream(monkeypatch) -> None:
    job_id = uuid.uuid4()
    pubsub = _PubSub([])
    status = "running"
    query_count = 0

    async def fake_list_events(_session: object, _job_id: uuid.UUID, **_kwargs):
        nonlocal query_count
        query_count += 1
        return []

    async def fake_get_job(_session: object, _job_id: uuid.UUID) -> SimpleNamespace:
        return SimpleNamespace(stage="done", progress=1.0, status=status)

    monkeypatch.setattr(event_router, "list_job_events", fake_list_events)
    monkeypatch.setattr(event_router, "get_job", fake_get_job)
    stream = event_router._event_stream(
        _SessionFactory(),  # type: ignore[arg-type]
        job_id,
        0,
        _Request(),  # type: ignore[arg-type]
        _EventBus(pubsub),  # type: ignore[arg-type]
    )
    pending = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)
    status = "succeeded"
    await pubsub.messages.put({"type": "message"})

    closed = _sse_data(await asyncio.wait_for(pending, timeout=1))
    assert closed["type"] == "job.closed"
    assert closed["status"] == "succeeded"
    assert query_count == 2
    await stream.aclose()
    assert pubsub.closed


@pytest.mark.asyncio
async def test_finished_job_replay_closes_without_waiting_for_new_wakeup(monkeypatch) -> None:
    job_id = uuid.uuid4()
    pubsub = _PubSub([])
    query_count = 0

    async def fake_list_events(_session: object, _job_id: uuid.UUID, *, after_seq: int, **_kw):
        nonlocal query_count
        query_count += 1
        return [_event(1)] if after_seq == 0 else []

    async def fake_get_job(_session: object, _job_id: uuid.UUID) -> SimpleNamespace:
        return SimpleNamespace(stage="done", progress=1.0, status="succeeded")

    monkeypatch.setattr(event_router, "list_job_events", fake_list_events)
    monkeypatch.setattr(event_router, "get_job", fake_get_job)
    stream = event_router._event_stream(
        _SessionFactory(),  # type: ignore[arg-type]
        job_id,
        0,
        _Request(),  # type: ignore[arg-type]
        _EventBus(pubsub),  # type: ignore[arg-type]
    )

    replayed = _sse_data(await anext(stream))
    closed = _sse_data(await asyncio.wait_for(anext(stream), timeout=1))
    assert replayed["seq"] == 1
    assert closed["type"] == "job.closed"
    assert query_count == 2
    await stream.aclose()


@pytest.mark.asyncio
async def test_safety_refresh_replays_events_from_pre_notification_worker(monkeypatch) -> None:
    """Old blue/green workers remain observable while their in-flight jobs drain."""
    job_id = uuid.uuid4()
    pubsub = _IdlePubSub([])
    query_count = 0

    async def fake_list_events(_session: object, _job_id: uuid.UUID, **_kwargs):
        nonlocal query_count
        query_count += 1
        return [] if query_count == 1 else [_event(1)]

    async def fake_get_job(_session: object, _job_id: uuid.UUID) -> SimpleNamespace:
        return SimpleNamespace(stage="write", progress=0.5, status="running")

    monkeypatch.setattr(event_router, "HEARTBEAT_SECONDS", 0.0)
    monkeypatch.setattr(event_router, "list_job_events", fake_list_events)
    monkeypatch.setattr(event_router, "get_job", fake_get_job)
    stream = event_router._event_stream(
        _SessionFactory(),  # type: ignore[arg-type]
        job_id,
        0,
        _Request(),  # type: ignore[arg-type]
        _EventBus(pubsub),  # type: ignore[arg-type]
    )

    replayed = _sse_data(await asyncio.wait_for(anext(stream), timeout=1))
    assert replayed["seq"] == 1
    assert query_count == 2
    await stream.aclose()


@pytest.mark.asyncio
async def test_stream_authorization_closes_owned_session_before_return(monkeypatch) -> None:
    calls: list[str] = []

    class _Session:
        info: dict[str, Any] = {}

        async def commit(self) -> None:
            calls.append("commit")

        async def rollback(self) -> None:
            calls.append("rollback")

    class _OwnedContext:
        async def __aenter__(self) -> _Session:
            calls.append("enter")
            return _Session()

        async def __aexit__(self, *_args: Any) -> None:
            calls.append("exit")

    class _OwnedFactory:
        def __call__(self) -> _OwnedContext:
            return _OwnedContext()

    auth = SimpleNamespace(user=SimpleNamespace(id=uuid.uuid4()))

    async def fake_authenticate(*_args: Any, **_kwargs: Any) -> Any:
        calls.append("authenticate")
        return auth

    async def fake_authorize(*_args: Any, **_kwargs: Any) -> None:
        calls.append("authorize")

    monkeypatch.setattr(deps, "get_session_factory", lambda: _OwnedFactory())
    monkeypatch.setattr(deps, "_authenticate_session", fake_authenticate)
    monkeypatch.setattr(deps, "_authorize_request_project", fake_authorize)

    result = await deps.authorize_stream_project_request(
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
    )

    assert result is auth
    assert calls == ["enter", "authenticate", "authorize", "commit", "exit"]
    assert not inspect.isasyncgenfunction(deps.authorize_stream_project_request)
