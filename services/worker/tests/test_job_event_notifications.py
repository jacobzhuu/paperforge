from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from paperforge_worker import context as context_module
from paperforge_worker.context import JobContext


class _Session:
    def __init__(self, order: list[str]) -> None:
        self.order = order

    async def commit(self) -> None:
        self.order.append("commit")

    async def rollback(self) -> None:
        self.order.append("rollback")


class _SessionContext:
    def __init__(self, order: list[str]) -> None:
        self.session = _Session(order)

    async def __aenter__(self) -> _Session:
        return self.session

    async def __aexit__(self, *_args: Any) -> None:
        return None


class _SessionFactory:
    def __init__(self, order: list[str]) -> None:
        self.order = order

    def __call__(self) -> _SessionContext:
        return _SessionContext(self.order)


class _Publisher:
    def __init__(self, order: list[str]) -> None:
        self.order = order
        self.calls: list[tuple[str, str]] = []

    async def publish(self, channel: str, message: str) -> None:
        self.order.append("publish")
        self.calls.append((channel, message))


@pytest.mark.asyncio
async def test_emit_publishes_only_after_the_event_transaction_commits(monkeypatch) -> None:
    order: list[str] = []
    publisher = _Publisher(order)
    job_id = uuid.uuid4()

    async def fake_load_job(_session: object, _job_id: uuid.UUID) -> SimpleNamespace:
        return SimpleNamespace()

    async def fake_update(*_args: Any, **_kwargs: Any) -> None:
        order.append("update")

    async def fake_append(*_args: Any, **_kwargs: Any) -> None:
        order.append("append")

    monkeypatch.setattr(context_module, "_load_job", fake_load_job)
    monkeypatch.setattr(context_module, "update_job", fake_update)
    monkeypatch.setattr(context_module, "append_job_event", fake_append)
    context = JobContext(
        project_id=uuid.uuid4(),
        job_id=job_id,
        settings=SimpleNamespace(),  # type: ignore[arg-type]
        session_factory=_SessionFactory(order),  # type: ignore[arg-type]
        http_client=SimpleNamespace(),  # type: ignore[arg-type]
        scholar_cache=SimpleNamespace(),
        event_publisher=publisher,
    )

    await context.emit("write.completed", {"sections": 7}, stage="write", progress=0.8)

    assert order == ["update", "append", "commit", "publish"]
    assert publisher.calls == [(context_module.job_event_channel(job_id), "1")]
