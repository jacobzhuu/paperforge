from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from paperforge_api.jobs import ensure_project_job_slot


async def test_project_job_slot_rejects_an_active_mutating_job() -> None:
    project_id = uuid4()
    active = SimpleNamespace(
        id=uuid4(),
        kind="full",
        status="running",
    )

    class _Session:
        def __init__(self) -> None:
            self.calls = 0

        async def scalar(self, statement):
            self.calls += 1
            return project_id if self.calls == 1 else active

    with pytest.raises(HTTPException) as caught:
        await ensure_project_job_slot(_Session(), project_id)  # type: ignore[arg-type]

    assert caught.value.status_code == 409
    assert caught.value.detail == {
        "code": "project_job_active",
        "message": "该项目已有会修改研究产物的任务正在运行",
        "job_id": str(active.id),
        "kind": "full",
        "status": "running",
    }


async def test_project_job_slot_allows_terminal_history() -> None:
    project_id = uuid4()

    class _Session:
        def __init__(self) -> None:
            self.calls = 0

        async def scalar(self, statement):
            self.calls += 1
            return project_id if self.calls == 1 else None

    session = _Session()
    await ensure_project_job_slot(session, project_id)  # type: ignore[arg-type]
    assert session.calls == 2
