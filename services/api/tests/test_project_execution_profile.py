import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import paperforge_api.config as config
import paperforge_api.dispatch as dispatch
import paperforge_api.jobs as jobs
import pytest
from fastapi import HTTPException


@pytest.mark.asyncio
async def test_new_job_snapshots_project_profile_and_retry_preserves_source(monkeypatch):
    project_id = uuid.uuid4()
    source_id = uuid.uuid4()
    project = SimpleNamespace(id=project_id, execution_profile="fast_draft")
    source = SimpleNamespace(
        id=source_id,
        project_id=project_id,
        checkpoint_json={"execution_profile": "standard"},
    )

    class Session:
        async def get(self, model, key):
            return project if key == project_id else source if key == source_id else None

    session = Session()
    create = AsyncMock(
        side_effect=lambda _session, **kwargs: SimpleNamespace(checkpoint_json=kwargs["checkpoint"])
    )
    monkeypatch.setattr(jobs, "require_queue", AsyncMock(return_value=object()))
    monkeypatch.setattr(jobs, "ensure_project_job_slot", AsyncMock())
    monkeypatch.setattr(jobs, "create_job", create)
    monkeypatch.setattr(dispatch, "record_dispatch", AsyncMock())
    monkeypatch.setattr(
        config,
        "get_settings",
        lambda: SimpleNamespace(
            semantic_repair_engine="legacy",
            writer_polish_policy="legacy",
            writer_polish_concurrency=2,
            evidence_retrieval_mode="legacy",
        ),
    )

    fresh = await jobs.start_job(
        session, object(), project_id=project_id, kind="write", function="run_write_pipeline"
    )
    assert fresh.checkpoint_json["execution_profile"] == "fast_draft"
    inherited = await jobs.retry_profile_checkpoint(session, project_id, str(source_id))
    retried = await jobs.start_job(
        session,
        object(),
        project_id=project_id,
        kind="write",
        function="run_write_pipeline",
        checkpoint=inherited,
    )
    assert retried.checkpoint_json["execution_profile"] == "standard"


@pytest.mark.asyncio
async def test_retry_rejects_source_from_another_project():
    source = SimpleNamespace(
        project_id=uuid.uuid4(), checkpoint_json={"execution_profile": "fast_draft"}
    )

    class Session:
        async def get(self, _model, _key):
            return source

    with pytest.raises(HTTPException) as error:
        await jobs.retry_profile_checkpoint(Session(), uuid.uuid4(), str(uuid.uuid4()))
    assert error.value.status_code == 404
