import uuid
from unittest.mock import AsyncMock

from db import get_project, update_job
from db.models.paper import GenerationJob
from paperforge_worker.config import WorkerSettings
from paperforge_worker.intake_job import run_intake_pipeline
from paperforge_worker.pipelines.scope import deterministic_scope
from test_projects_api import _create_project, seed
from test_projects_api import client as client  # noqa: F401

from conftest import run_async


def create(client, **kwargs):
    return _create_project(
        client, **{"intake": {}, "writing_mode": "assisted", "language": "zh", **kwargs}
    )


def run_worker(database_url, project, job, monkeypatch, result):
    import paperforge_worker.intake_job as intake_job

    model = AsyncMock(return_value=result)
    monkeypatch.setattr(intake_job, "understand", model)
    run_async(
        run_intake_pipeline(
            {
                "settings": WorkerSettings(
                    _env_file=None, database_url=database_url, llm_default_provider="noop"
                )
            },
            project["id"],
            job["id"],
            version=job["checkpoint"]["intake_version"],
        )
    )
    return model


def understood(**changes):
    return {
        "paper_type": "original",
        "language": "en",
        "summary": "整理已有结果",
        "next_step": "提出研究问题",
        "questions": [],
        "submission_target": "Nature",
        "materials": [],
        "sources": {"language": "explicit", "paper_type": "model"},
        "scope": {
            **deterministic_scope("experimental results", language="en"),
            "generator": "llm:intake:test",
        },
        **changes,
    }


def test_intake_is_opt_in_and_pending_project_cannot_start_downstream(client):
    old = _create_project(client)
    assert old["intake"] is None
    project = create(client)
    assert project["intake"]["status"] == "pending"
    path = f"/api/v1/projects/{project['id']}"
    assert client.post(path + "/search/runs", json={}).status_code == 409
    assert client.post(path + "/generate", json={}).status_code == 409
    assert client.post(path + "/scope/generate", json={}).status_code == 409
    assert (
        client.put(path + "/scope", json={"scope": {"intake": {"status": "ready"}}}).status_code
        == 409
    )


def test_intake_duplicate_and_stale_requests_do_not_create_jobs(client):
    project = create(client)
    path = f"/api/v1/projects/{project['id']}/intake"
    first = client.post(path, json={"version": 0})
    assert first.status_code == 202, first.text
    second = client.post(path, json={"version": 0})
    assert second.status_code == 202 and second.json()["id"] == first.json()["id"]
    assert client.post(path, json={"version": 0, "answer": "new"}).status_code == 409
    assert client.post(path, json={"version": 1, "answer": "new"}).status_code == 409
    assert len(client.queue.calls) == 1


def test_worker_saves_understanding_without_writing_and_protects_scope_state(
    client,
    clean_pg_database_url,
    monkeypatch,
):
    project = create(client)
    path = f"/api/v1/projects/{project['id']}"
    job = client.post(path + "/intake", json={"version": 0}).json()
    model = run_worker(clean_pg_database_url, project, job, monkeypatch, understood())
    model.assert_awaited_once()
    saved = client.get(path).json()
    assert (saved["paper_type"], saved["language"]) == ("original", "en")
    assert saved["intake"]["status"] == "ready"
    assert client.get(path + "/intake").json()["material_issues"]
    state = client.get(path + "/intake").json()
    assert state["type_locked"] is False
    assert (
        client.put(
            path + "/scope", json={"scope": {"topic": "updated", "intake": {"status": "pending"}}}
        ).status_code
        == 200
    )
    assert client.get(path + "/intake").json()["status"] == "ready"
    assert len(client.queue.calls) == 1  # assisted never schedules writing


def test_clarification_and_manual_pins_survive_reload(client, clean_pg_database_url, monkeypatch):
    project = create(client)
    path = f"/api/v1/projects/{project['id']}"
    job = client.post(path + "/intake", json={"version": 0}).json()
    run_worker(
        clean_pg_database_url,
        project,
        job,
        monkeypatch,
        understood(
            paper_type=None, questions=[{"question": "整理结果还是研究方案？", "options": []}]
        ),
    )
    assert client.get(path + "/intake").json()["status"] == "needs_input"
    assert client.post(path + "/search/runs", json={}).status_code == 409
    followup = client.post(
        path + "/intake",
        json={
            "version": 1,
            "answer": "整理已有结果",
            "overrides": {"language": "en", "paper_type": "original"},
        },
    )
    assert followup.status_code == 202, followup.text
    run_worker(
        clean_pg_database_url, project, followup.json(), monkeypatch, understood(language="zh")
    )
    saved = client.get(path).json()
    assert saved["language"] == "en"
    assert client.get(path + "/intake").json()["answers"] == ["整理已有结果"]
    assert client.get(path + "/intake").json()["overrides"]["paper_type"] == "original"


def test_type_locks_after_downstream_job(client, clean_pg_database_url, monkeypatch):
    project = create(client)
    path = f"/api/v1/projects/{project['id']}"
    job = client.post(path + "/intake", json={"version": 0}).json()
    run_worker(clean_pg_database_url, project, job, monkeypatch, understood(paper_type="review"))
    search = client.post(path + "/search/runs", json={}).json()

    async def finish(session):
        row = await session.get(GenerationJob, uuid.UUID(search["id"]))
        await update_job(session, row, status="failed")

    seed(clean_pg_database_url, finish)
    assert client.get(path + "/intake").json()["type_locked"] is True
    response = client.post(
        path + "/intake", json={"version": 1, "overrides": {"paper_type": "original"}}
    )
    assert response.status_code == 409


def test_failed_model_is_retryable_and_does_not_claim_understanding(
    client,
    clean_pg_database_url,
    monkeypatch,
):
    import paperforge_worker.intake_job as intake_job

    project = create(client)
    path = f"/api/v1/projects/{project['id']}"
    job = client.post(path + "/intake", json={"version": 0}).json()
    failed_model = AsyncMock(side_effect=ValueError("invalid model"))
    monkeypatch.setattr(intake_job, "understand", failed_model)
    run_async(
        run_intake_pipeline(
            {"settings": WorkerSettings(_env_file=None, database_url=clean_pg_database_url)},
            project["id"],
            job["id"],
            version=1,
        )
    )
    failed_model.assert_awaited_once()
    assert client.get(path + "/intake").json()["status"] == "failed"
    assert client.post(path + "/intake", json={"version": 0}).status_code == 409
    retry = client.post(path + "/intake", json={"version": 1})
    assert retry.status_code == 202, retry.text
    assert retry.json()["id"] != job["id"]
    duplicate = client.post(path + "/intake", json={"version": 1})
    assert duplicate.status_code == 202
    assert duplicate.json()["id"] == retry.json()["id"]
    assert len(client.queue.calls) == 2
    successful_model = run_worker(
        clean_pg_database_url, project, retry.json(), monkeypatch, understood(paper_type="review")
    )
    successful_model.assert_awaited_once()
    assert client.get(path + "/intake").json()["status"] == "ready"


def test_stale_result_does_not_overwrite_new_version(client, clean_pg_database_url, monkeypatch):
    project = create(client)
    path = f"/api/v1/projects/{project['id']}"
    job = client.post(path + "/intake", json={"version": 0}).json()

    async def advance(session):
        row = await get_project(session, uuid.UUID(project["id"]))
        row.scope_json = {**row.scope_json, "intake": {**row.scope_json["intake"], "version": 2}}

    seed(clean_pg_database_url, advance)
    model = run_worker(clean_pg_database_url, project, job, monkeypatch, understood())
    model.assert_not_awaited()
    assert client.get(path).json()["paper_type"] == "review"


def test_auto_continues_only_after_direction_and_material_checks(
    client,
    clean_pg_database_url,
    monkeypatch,
):
    import paperforge_worker.worker as worker

    full = AsyncMock(return_value={"started": True})
    monkeypatch.setattr(worker, "run_full_pipeline", full)
    project = create(client, writing_mode="auto")
    path = f"/api/v1/projects/{project['id']}"
    job = client.post(path + "/intake", json={"version": 0}).json()
    run_worker(clean_pg_database_url, project, job, monkeypatch, understood())
    full.assert_not_awaited()  # original research has no method/results
    next_job = client.post(path + "/intake", json={"version": 1}).json()
    run_worker(
        clean_pg_database_url, project, next_job, monkeypatch, understood(paper_type="review")
    )
    full.assert_awaited_once()
    assert full.call_args.kwargs == {"quality_profile": "draft", "regenerate_scope": False}
    assert client.get(path + "/intake").json()["type_locked"] is True


def test_cancelled_intake_never_calls_model_or_full_pipeline(
    client,
    clean_pg_database_url,
    monkeypatch,
):
    project = create(client, writing_mode="auto")
    path = f"/api/v1/projects/{project['id']}"
    job = client.post(path + "/intake", json={"version": 0}).json()
    assert client.post(path + f"/jobs/{job['id']}/cancel").status_code == 200
    model = run_worker(clean_pg_database_url, project, job, monkeypatch, understood())
    model.assert_not_awaited()
    assert client.get(path + "/intake").json()["status"] == "failed"


def test_manual_language_edit_during_model_call_discards_old_result(
    client,
    clean_pg_database_url,
    monkeypatch,
):
    import paperforge_worker.intake_job as intake_job
    import pytest

    project = create(client)
    path = f"/api/v1/projects/{project['id']}"
    job = client.post(path + "/intake", json={"version": 0}).json()

    async def changed_settings(*args, **kwargs):
        assert client.patch(path, json={"language": "en"}).status_code == 200
        return understood(language="zh")

    monkeypatch.setattr(intake_job, "understand", changed_settings)
    with pytest.raises(ValueError, match="手动设置已更新"):
        run_async(
            run_intake_pipeline(
                {"settings": WorkerSettings(_env_file=None, database_url=clean_pg_database_url)},
                project["id"],
                job["id"],
                version=1,
            )
        )
    assert client.get(path).json()["language"] == "en"
    assert client.get(path + "/intake").json()["status"] == "failed"


def test_opt_in_defaults_never_silently_choose_automation(client):
    response = client.post("/api/v1/projects", json={
        "title": "研究目标", "paper_type": "review", "intake": {},
    })
    assert response.status_code == 201
    project = response.json()
    assert (project["writing_mode"], project["language"]) == ("assisted", "zh")
    assert (project["citation_style"], project["venue_template"]) == ("gbt7714", "article")
