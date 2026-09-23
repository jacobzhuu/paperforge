# ruff: noqa: F811
import uuid

from db import create_document, upsert_section
from db.session import make_session_factory
from langgraph.checkpoint.memory import InMemorySaver
from paperforge_worker.config import WorkerSettings
from paperforge_worker.context import job_context
from paperforge_worker.orchestration.research_graph import run_research
from paperforge_worker.worker import _finish, _mark_running
from test_projects_api import _create_project, client, seed  # noqa: F401


def start(client, project, **extra):
    asset = client.post(
        f"/api/v1/projects/{project['id']}/assets",
        files={"file": ("data.csv", b"group,x\na,1\na,3\nb,2\n", "text/csv")},
    )
    assert asset.status_code == 201, asset.text
    response = client.post(
        f"/api/v1/projects/{project['id']}/research-runs",
        json={
            "goal": "summarize",
            "asset_id": asset.json()["id"],
            "engine": "deterministic",
            **extra,
        },
    )
    assert response.status_code == 202, response.text
    return response.json()


def execute(url, run, project, saver):
    async def perform(session):
        factory = make_session_factory(session.bind)
        async with job_context(
            project_id=uuid.UUID(project["id"]),
            job_id=uuid.UUID(run["job_id"]),
            settings=WorkerSettings(database_url=url),
            session_factory=factory,
        ) as context:
            await _mark_running(context)
            await run_research(context, run["id"], saver)
            await _finish(context, delivered=True)

    seed(url, perform)


def test_analysis_clarification_and_result_without_modifying_manuscript(
    client, clean_pg_database_url
):
    project = _create_project(client, paper_type="original")
    run = start(client, project)
    saver = InMemorySaver()
    execute(clean_pg_database_url, run, project, saver)
    root = f"/api/v1/projects/{project['id']}"
    result = client.get(f"{root}/analysis-results/{run['id']}").json()
    assert result["status"] == "needs_input"
    response = client.post(
        f"{root}/research-runs/{run['job_id']}/responses",
        json={
            "interrupt_id": result["result"]["interrupt_id"],
            "spec": {"columns": ["x"], "group_by": "group", "units": {"x": "1"}, "confirmed": True},
        },
    )
    assert response.status_code == 202, response.text
    execute(clean_pg_database_url, response.json(), project, saver)
    result = client.get(f"{root}/analysis-results/{run['id']}").json()
    assert result["status"] == "completed"
    assert result["result"]["records"][0]["mean"] == 2
    assert client.get(f"{root}/analysis-results/{run['id']}/chart").content.startswith(b"\x89PNG")
    assert client.get(f"{root}/sections").json() == []
    assert client.post(
        f"{root}/research-runs/{run['job_id']}/responses",
        json={"interrupt_id": "stale", "spec": {"confirmed": True, "columns": ["x"]}},
    ).status_code in {404, 409}


def test_proposal_commit_is_versioned_and_idempotent(client, clean_pg_database_url, monkeypatch):
    project = _create_project(client, paper_type="original")
    from paperforge_api.routers import research

    async def whitelist(*args):
        return {"smith2026longkey": uuid.uuid4()}

    monkeypatch.setattr(research, "get_writing_whitelist", whitelist)

    async def document(session):
        doc = await create_document(session, project_id=uuid.UUID(project["id"]), outline_id=None)
        await upsert_section(
            session,
            document_id=doc.id,
            section_key="results",
            title="Results",
            order_no=1,
            body_ir={"key": "results", "title": "Results", "blocks": []},
            cite_keys=["smith2026longkey"],
        )
        return str(doc.id)

    original = seed(clean_pg_database_url, document)
    run = start(
        client,
        project,
        section_key="results",
        spec={"columns": ["x"], "units": {"x": "1"}, "confirmed": True},
    )
    execute(clean_pg_database_url, run, project, InMemorySaver())
    root = f"/api/v1/projects/{project['id']}"
    result = client.get(f"{root}/analysis-results/{run['id']}").json()
    assert result["status"] == "proposed"
    decision = f"{root}/artifact-proposals/{run['id']}/decision"
    accepted = client.post(decision, json={"choice": "accept"})
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["document_id"] != original
    assert (
        client.post(decision, json={"choice": "accept"}).json()["document_id"]
        == accepted.json()["document_id"]
    )
    assert client.post(decision, json={"choice": "reject"}).status_code == 409
    sections = client.get(f"{root}/sections").json()
    assert len(sections) == 1
    assert len(sections[0]["body_ir"]["blocks"]) == 3


def test_cross_project_analysis_access_is_denied(client, clean_pg_database_url):
    one = _create_project(client, paper_type="original")
    two = _create_project(client, paper_type="original")
    run = start(client, one)
    assert (
        client.get(f"/api/v1/projects/{two['id']}/analysis-results/{run['id']}").status_code == 404
    )
    assert (
        client.post(
            f"/api/v1/projects/{two['id']}/artifact-proposals/{run['id']}/decision",
            json={"choice": "accept"},
        ).status_code
        == 404
    )
    client.cookies.clear()
    assert (
        client.get(f"/api/v1/projects/{one['id']}/analysis-results/{run['id']}").status_code == 401
    )


def test_changed_manuscript_cannot_accept_old_proposal(client, clean_pg_database_url):
    project = _create_project(client, paper_type="original")

    async def setup(session):
        doc = await create_document(session, project_id=uuid.UUID(project["id"]), outline_id=None)
        await upsert_section(
            session,
            document_id=doc.id,
            section_key="results",
            title="Results",
            order_no=1,
            body_ir={"key": "results", "title": "Results", "blocks": []},
            cite_keys=[],
        )
        return doc.id

    doc_id = seed(clean_pg_database_url, setup)
    run = start(
        client,
        project,
        section_key="results",
        spec={"columns": ["x"], "units": {"x": "1"}, "confirmed": True},
    )
    execute(clean_pg_database_url, run, project, InMemorySaver())

    async def edit(session):
        await upsert_section(
            session,
            document_id=doc_id,
            section_key="results",
            title="Changed",
            order_no=1,
            body_ir={"key": "results", "title": "Changed", "blocks": []},
            cite_keys=[],
        )

    seed(clean_pg_database_url, edit)
    decision = client.post(
        f"/api/v1/projects/{project['id']}/artifact-proposals/{run['id']}/decision",
        json={"choice": "accept"},
    )
    assert decision.status_code == 409, decision.text


def test_postgres_graph_reconnect_preserves_pending_input(client, clean_pg_database_url):
    from paperforge_worker.orchestration.semantic_graph import setup_graph_store
    from paperforge_worker.worker import run_research_pipeline

    project = _create_project(client, paper_type="original")
    run = start(client, project)

    def worker(current):
        async def perform(session):
            await setup_graph_store(clean_pg_database_url)
            await run_research_pipeline(
                {
                    "settings": WorkerSettings(database_url=clean_pg_database_url),
                    "session_factory": make_session_factory(session.bind),
                },
                project["id"],
                current["job_id"],
                run["id"],
            )

        seed(clean_pg_database_url, perform)

    worker(run)
    root = f"/api/v1/projects/{project['id']}"
    pending = client.get(f"{root}/analysis-results/{run['id']}").json()
    response = client.post(
        f"{root}/research-runs/{run['job_id']}/responses",
        json={
            "interrupt_id": pending["result"]["interrupt_id"],
            "spec": {"columns": ["x"], "units": {"x": "1"}, "confirmed": True},
        },
    )
    assert response.status_code == 202, response.text
    worker(response.json())
    result = client.get(f"{root}/analysis-results/{run['id']}").json()
    assert result["status"] == "completed"
    archive = client.get(f"{root}/analysis-results/{run['id']}/reproducibility")
    assert archive.status_code == 200
    import io
    import zipfile

    with zipfile.ZipFile(io.BytesIO(archive.content)) as bundle:
        assert {"input.csv", "research.py", "reproduce.py", "spec.json", "result.json"} <= set(
            bundle.namelist()
        )
