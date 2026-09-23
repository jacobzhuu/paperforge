import hashlib
import uuid
from datetime import UTC, datetime, timedelta

from db import create_user, create_user_session

# Reuse the authenticated API fixture; pytest injects parameters with the same name.
# ruff: noqa: F811
from db.models.web_research import WebResearchRun
from test_projects_api import (
    _create_project,
    client,  # noqa: F401 - shared authenticated fixture
    seed,
)


def test_project_opt_in_and_job_slot(client):
    project = _create_project(client)
    assert project["web_research_enabled"] is False
    base = f"/api/v1/projects/{project['id']}"
    assert client.post(base + "/web-research/runs").status_code == 409
    updated = client.patch(base, json={"web_research_enabled": True})
    assert updated.status_code == 200 and updated.json()["web_research_enabled"]
    assert client.patch(base, json={"web_research_enabled": None}).status_code == 422
    job = client.post(base + "/web-research/runs")
    assert job.status_code == 202, job.text
    assert job.json()["kind"] == "web_research"
    assert client.post(base + "/web-research/runs").status_code == 409


def test_run_cannot_be_read_through_another_project(client, clean_pg_database_url):
    a, b = _create_project(client), _create_project(client)

    async def setup(session):
        run = WebResearchRun(project_id=uuid.UUID(a["id"]), plan_json={"queries": ["public"]})
        session.add(run)
        await session.flush()
        return str(run.id)

    identifier = seed(clean_pg_database_url, setup)
    path = f"/web-research/runs/{identifier}"
    assert client.get(f"/api/v1/projects/{a['id']}" + path).status_code == 200
    assert client.get(f"/api/v1/projects/{b['id']}" + path).status_code == 404
    client.cookies.clear()
    assert client.get(f"/api/v1/projects/{a['id']}" + path).status_code == 401


def test_disabled_service_and_deleted_project(client, monkeypatch):
    from paperforge_api.config import get_settings

    project = _create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    monkeypatch.setattr(get_settings(), "mcp_web_enabled", False)
    assert client.post(base + "/web-research/runs").status_code == 503
    assert client.get(base + "/web-research/runs").json()["available"] is False
    assert client.delete(base).status_code == 204
    assert client.get(base + "/web-research/runs").status_code == 404


def test_other_user_cannot_read_or_start_research(client, clean_pg_database_url):
    project = _create_project(client)
    token = "other-mcp-test-user"

    async def setup(session):
        owner = await create_user(
            session, email="other-mcp@example.test", password_hash="!test", verified=True
        )
        await create_user_session(
            session,
            user_id=owner.id,
            token_hash=hashlib.sha256(token.encode()).hexdigest(),
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )

    seed(clean_pg_database_url, setup)
    client.cookies.clear()
    client.cookies.set("paperforge_session", token)
    base = f"/api/v1/projects/{project['id']}"
    assert client.get(base + "/web-research/runs").status_code == 404
    assert client.post(base + "/web-research/runs").status_code == 404
    assert client.patch(base, json={"web_research_enabled": True}).status_code == 404
