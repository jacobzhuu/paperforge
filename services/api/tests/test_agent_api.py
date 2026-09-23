"""Authorization, resume conflicts and task-cost coverage on real PostgreSQL."""

# ruff: noqa: F811
import uuid

from db import create_job, record_llm_call, update_job
from test_projects_api import _create_project, client, seed  # noqa: F401


def test_trace_is_private_and_unknown_cost_is_not_zero(client, clean_pg_database_url):
    first, second = _create_project(client), _create_project(client)

    async def setup(session):
        job = await create_job(session, project_id=uuid.UUID(first["id"]), kind="write")
        await record_llm_call(
            session,
            project_id=job.project_id,
            job_id=job.id,
            role="writer",
            model="test",
            cost_estimate=None,
            metadata={"prompt": "PRIVATE", "api_key": "SECRET"},
        )
        return str(job.id)

    identifier = seed(clean_pg_database_url, setup)
    path = f"/jobs/{identifier}/trace"
    response = client.get(f"/api/v1/projects/{first['id']}" + path)
    assert response.status_code == 200, response.text
    assert response.json()["summary"]["unpriced_calls"] == 1
    assert "PRIVATE" not in response.text and "SECRET" not in response.text
    assert client.get(f"/api/v1/projects/{second['id']}" + path).status_code == 404
    client.cookies.clear()
    assert client.get(f"/api/v1/projects/{first['id']}" + path).status_code == 401


def test_human_response_is_single_use_and_required(client, clean_pg_database_url):
    project = _create_project(client)

    async def setup(session):
        job = await create_job(
            session,
            project_id=uuid.UUID(project["id"]),
            kind="write",
            checkpoint={
                "resume": {"function": "run_write_pipeline", "kwargs": {}},
                "semantic_repair_engine": "langgraph",
                "repair_interrupt": {
                    "id": "pending",
                    "version": "semantic-graph-v1",
                    "document_id": None,
                    "snapshot_hash": None,
                },
            },
        )
        await update_job(session, job, status="paused")
        return str(job.id)

    identifier = seed(clean_pg_database_url, setup)
    path = f"/api/v1/projects/{project['id']}/jobs/{identifier}/resume"
    assert client.post(path).status_code == 409
    assert client.post(path, json={"interrupt_id": "stale", "choice": "finish"}).status_code == 409
    assert (
        client.post(path, json={"interrupt_id": "pending", "choice": "invent"}).status_code == 422
    )
    resumed = client.post(path, json={"interrupt_id": "pending", "choice": "finish"})
    assert resumed.status_code == 202, resumed.text
    assert resumed.json()["checkpoint"]["semantic_repair_engine"] == "langgraph"
    assert (
        client.post(path, json={"interrupt_id": "pending", "choice": "finish"}).status_code == 409
    )


def test_search_empty_and_invalid_query(client):
    project = _create_project(client)
    path = f"/api/v1/projects/{project['id']}/evidence/search"
    assert client.get(path, params={"q": "x"}).status_code == 422
    result = client.get(path, params={"q": "retrieval"})
    assert result.status_code == 200, result.text
    assert result.json()["results"] == []
    assert client.get(path, params={"q": "retrieval", "mode": "bad"}).status_code == 422


def test_trace_totals_cover_calls_beyond_display_limit(client, clean_pg_database_url):
    from db.models.paper import LlmCallLog

    project = _create_project(client)

    async def setup(session):
        job = await create_job(session, project_id=uuid.UUID(project["id"]), kind="write")
        session.add_all(
            [
                LlmCallLog(
                    project_id=job.project_id,
                    job_id=job.id,
                    role="writer",
                    model="test",
                    cost_estimate=0.5,
                    input_tokens=10,
                    output_tokens=5,
                    error_code="test_failure" if index == 1001 else None,
                )
                for index in range(1002)
            ]
        )
        await session.flush()
        return str(job.id)

    identifier = seed(clean_pg_database_url, setup)
    response = client.get(f"/api/v1/projects/{project['id']}/jobs/{identifier}/trace")
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["calls_truncated"] and len(result["calls"]) == 1000
    assert result["summary"]["calls"] == 1002
    assert result["summary"]["known_cost"] == 501
    assert result["summary"]["input_tokens"] == 10020
    assert result["summary"]["errors"] == {"test_failure": 1}


def test_dependency_rebuild_authorization_and_source_conflict(client, clean_pg_database_url):
    from db import create_outline

    project = _create_project(client)
    other = _create_project(client)

    async def setup(session):
        row = await create_outline(
            session,
            project_id=uuid.UUID(project["id"]),
            tree={"sections": [{"key": "a", "title": "A"}]},
        )
        return str(row.id)

    identifier = seed(clean_pg_database_url, setup)
    source = client.get(f"/api/v1/projects/{project['id']}/outline").json()
    body = {"outline_id": identifier, "content_hash": source["content_hash"]}
    path = f"/api/v1/projects/{project['id']}/outline/dependencies/rebuild"
    assert client.post(path, json={**body, "content_hash": "0" * 64}).status_code == 409
    assert (
        client.post(
            f"/api/v1/projects/{other['id']}/outline/dependencies/rebuild", json=body
        ).status_code
        == 409
    )
    result = client.post(path, json=body)
    assert result.status_code == 202, result.text
    assert result.json()["checkpoint"]["resume"]["function"] == "run_dependency_rebuild_pipeline"
    client.cookies.clear()
    assert client.post(path, json=body).status_code == 401


def test_rebuilt_outline_requires_confirmation_and_preserves_old_version(
    client, clean_pg_database_url
):
    from db import create_outline
    from paperforge_worker.orchestration.dependency_contract import apply_proposals

    project = _create_project(client)
    tree = apply_proposals(
        {"sections": [{"key": "a", "title": "A"}, {"key": "b", "title": "B"}]}, []
    )

    async def setup(session):
        return str(
            (await create_outline(session, project_id=uuid.UUID(project["id"]), tree=tree)).id
        )

    identifier = seed(clean_pg_database_url, setup)
    path = f"/api/v1/projects/{project['id']}"
    assert client.post(path + "/sections/generate", json={"coherence": True}).status_code == 409
    confirmed = client.put(path + "/outline", json={"tree": tree, "status": "confirmed"})
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["outline_id"] == identifier
    tree["sections"][1]["title"] = "Changed argument"
    edited = client.put(path + "/outline", json={"tree": tree, "status": "confirmed"})
    assert edited.status_code == 200, edited.text
    assert edited.json()["outline_id"] != identifier
    assert edited.json()["status"] == "draft"


def test_automatic_contract_does_not_enable_parallel_body_but_pins_polish_choice(
    client, clean_pg_database_url
):
    from db import create_outline
    from paperforge_worker.orchestration.dependency_contract import apply_proposals

    project = _create_project(client)
    tree = apply_proposals({"sections": [{"key": "a", "title": "A"}]}, [])
    tree["dependency_contract"]["requires_confirmation"] = False

    async def setup(session):
        await create_outline(session, project_id=uuid.UUID(project["id"]), tree=tree)

    seed(clean_pg_database_url, setup)
    path = f"/api/v1/projects/{project['id']}/sections/generate"
    assert client.post(path, json={"polish_policy": "unknown"}).status_code == 422
    result = client.post(path, json={"coherence": True, "polish_policy": "selective_parallel"})
    assert result.status_code == 202, result.text
    checkpoint = result.json()["checkpoint"]
    assert checkpoint["writer_polish_policy"] == "selective_parallel"
    assert checkpoint["writer_polish_concurrency"] == 2
    assert "writer_execution_mode" not in checkpoint
    assert checkpoint["writer_outline_id"] and len(checkpoint["writer_outline_hash"]) == 64


def test_legacy_polish_trace_preserves_counts_without_treating_booleans_as_counts():
    from db.repositories.agent_trace import event_metadata

    result = event_metadata(
        "polish.completed",
        {
            "total": 9,
            "done": 6,
            "skipped": True,
            "results": {"accepted": 4, "rejected": 2},
            "prompt": "PRIVATE",
        },
    )
    assert result == {"total": 9, "rewritten": 6, "skipped": 3, "rejected": 2, "policy": "legacy"}
    assert "rewritten" not in event_metadata("polish.completed", {"total": 9, "skipped": True})
