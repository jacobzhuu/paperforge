"""M1 API 契约测试（设计 §4.7）。

覆盖项目 CRUD、SCOPE、文献库圈选与白名单，并验证 R1 在 API 边界的拦截：
不能凭一个 work id 把未核验/不存在的文献塞进项目。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest
from db import upsert_entry, upsert_work
from db.session import make_engine, make_session_factory
from fastapi.testclient import TestClient
from paperforge_api.deps import get_queue
from paperforge_api.main import create_app

from conftest import run_async


@dataclass(frozen=True)
class _Author:
    author_name: str
    author_order: int
    raw_affiliation: str | None = None


@dataclass(frozen=True)
class _Candidate:
    title: str = "Retrieval Augmented Generation"
    normalized_title_hash: str = "hash-rag"
    abstract: str | None = "We survey RAG."
    publication_year: int | None = 2023
    publication_date: str | None = "2023-05-02"
    work_type: str | None = "journal-article"
    venue_name: str | None = "JMLR"
    publisher: str | None = "ACME"
    language: str | None = "en"
    doi: str | None = "10.1000/rag"
    pmid: str | None = None
    pmcid: str | None = None
    arxiv_id: str | None = None
    openalex_id: str | None = None
    semantic_scholar_id: str | None = None
    corpus_id: str | None = None
    oa_status: str | None = "gold"
    license: str | None = None
    is_retracted: bool = False
    citation_count: int | None = 42
    influential_citation_count: int | None = None
    identifiers: tuple[Any, ...] = ()
    links: tuple[Any, ...] = ()
    authors: tuple[Any, ...] = (_Author("Ada Lovelace", 1),)
    raw_provider_metadata: dict[str, Any] = field(default_factory=dict)


class _FakeQueue:
    """记录入队调用的替身：API 测试不依赖真实 Redis/worker。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    async def enqueue_job(self, function: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((function, args, kwargs))


@pytest.fixture
def client(clean_pg_database_url, monkeypatch):
    """TestClient 自带事件循环：让 API 在该循环内建自己的 asyncpg 连接。"""
    monkeypatch.setenv("DATABASE_URL", clean_pg_database_url)
    # 测试必须与开发者的真实 provider 配置隔离：既保证断言稳定，也不花钱。
    monkeypatch.setenv("LLM_DEFAULT_PROVIDER", "noop")
    monkeypatch.setenv("LLM_OPENAI_API_KEY", "")
    import paperforge_api.config as api_config
    import paperforge_api.deps as api_deps

    api_config._settings = None
    api_deps._engine = None
    api_deps._session_factory = None

    app = create_app()
    queue = _FakeQueue()
    app.dependency_overrides[get_queue] = lambda: queue
    with TestClient(app) as test_client:
        test_client.queue = queue  # type: ignore[attr-defined]
        yield test_client
    app.dependency_overrides.clear()
    api_config._settings = None
    api_deps._engine = None
    api_deps._session_factory = None


def seed(database_url: str, coro_factory):
    """在独立循环里写入测试数据（TestClient 的循环不可复用）。"""

    async def _run():
        engine = make_engine(database_url)
        factory = make_session_factory(engine)
        try:
            async with factory() as session:
                result = await coro_factory(session)
                await session.commit()
                return result
        finally:
            await engine.dispose()

    return run_async(_run())


def _create_project(client: TestClient, **overrides: Any) -> dict[str, Any]:
    body = {
        "title": "RAG for science",
        "paper_type": "review",
        "writing_mode": "auto",
        "language": "en",
        "topic": "retrieval augmented generation",
        "citation_style": "author_year",
    }
    body.update(overrides)
    response = client.post("/api/v1/projects", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def test_health_endpoint(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}


def test_create_and_list_projects(client: TestClient) -> None:
    created = _create_project(client)
    assert created["status"] == "draft"
    assert created["topic"] == "retrieval augmented generation"
    assert created["library_count"] == 0

    listed = client.get("/api/v1/projects").json()
    assert [item["id"] for item in listed] == [created["id"]]

    fetched = client.get(f"/api/v1/projects/{created['id']}").json()
    assert fetched["title"] == "RAG for science"


def test_create_project_rejects_invalid_paper_type(client: TestClient) -> None:
    response = client.post(
        "/api/v1/projects",
        json={"title": "x", "paper_type": "monograph", "citation_style": "author_year"},
    )
    assert response.status_code == 422


def test_unknown_project_returns_404(client: TestClient) -> None:
    assert client.get("/api/v1/projects/not-a-uuid").status_code == 404


def test_scope_generate_and_edit_roundtrip(client: TestClient) -> None:
    project = _create_project(client)
    generated = client.post(
        f"/api/v1/projects/{project['id']}/scope/generate",
        json={},
    )
    assert generated.status_code == 200
    scope = generated.json()["scope"]
    # provider 置为 noop：走确定性回退，SCOPE 仍然有产物（draft-first）。
    assert scope["generator"] == "deterministic"
    assert scope["keyword_groups"]

    edited = {**scope, "research_question": "edited question"}
    put = client.put(f"/api/v1/projects/{project['id']}/scope", json={"scope": edited})
    assert put.status_code == 200
    assert put.json()["scope"]["research_question"] == "edited question"

    assert (
        client.get(f"/api/v1/projects/{project['id']}/scope").json()["scope"][
            "research_question"
        ]
        == "edited question"
    )


def test_search_enqueues_job(client: TestClient) -> None:
    project = _create_project(client)
    response = client.post(
        f"/api/v1/projects/{project['id']}/search/runs",
        json={"providers": ["openalex"], "regenerate_scope": True},
    )
    assert response.status_code == 202
    job = response.json()
    assert job["kind"] == "search"
    assert job["status"] == "queued"

    function, args, kwargs = client.queue.calls[-1]  # type: ignore[attr-defined]
    assert function == "run_library_pipeline"
    assert args[0] == project["id"]
    assert kwargs["providers"] == ["openalex"]

    jobs = client.get(f"/api/v1/projects/{project['id']}/jobs").json()
    assert [item["id"] for item in jobs] == [job["id"]]


def test_import_requires_dois_or_bibtex(client: TestClient) -> None:
    project = _create_project(client)
    response = client.post(f"/api/v1/projects/{project['id']}/library/import", json={})
    assert response.status_code == 422


def test_import_enqueues_verification_job(client: TestClient) -> None:
    project = _create_project(client)
    response = client.post(
        f"/api/v1/projects/{project['id']}/library/import",
        json={"dois": ["10.1000/rag"]},
    )
    assert response.status_code == 202
    function, _args, kwargs = client.queue.calls[-1]  # type: ignore[attr-defined]
    # R1：导入必须走反查任务，API 不直接写库。
    assert function == "run_import_pipeline"
    assert kwargs["dois"] == ["10.1000/rag"]


def test_library_selection_assigns_keys_and_populates_whitelist(
    client: TestClient,
    clean_pg_database_url: str,
) -> None:
    project = _create_project(client)

    async def _seed(session):
        work, _ = await upsert_work(session, _Candidate())
        await upsert_entry(
            session,
            project_id=uuid.UUID(project["id"]),
            work_id=work.id,
            added_via="search",
            status="candidate",
            relevance_score=0.8,
            verified=True,
        )
        return str(work.id)

    work_id = seed(clean_pg_database_url, _seed)

    library = client.get(f"/api/v1/projects/{project['id']}/library").json()
    assert len(library) == 1
    assert library[0]["status"] == "candidate"
    assert library[0]["bibtex_key"] is None
    # 未 selected 时白名单为空。
    assert client.get(f"/api/v1/projects/{project['id']}/library/whitelist").json()[
        "cite_keys"
    ] == []

    selected = client.post(
        f"/api/v1/projects/{project['id']}/library/entries",
        json={"work_ids": [work_id], "status": "selected"},
    )
    assert selected.status_code == 200

    whitelist = client.get(f"/api/v1/projects/{project['id']}/library/whitelist").json()
    assert len(whitelist["cite_keys"]) == 1
    assert whitelist["cite_keys"][0].startswith("lovelace2023")


def test_selecting_unknown_work_is_rejected(client: TestClient) -> None:
    """R1：API 不接受任意 work id——只能在已核验候选之间切换状态。"""
    project = _create_project(client)
    response = client.post(
        f"/api/v1/projects/{project['id']}/library/entries",
        json={"work_ids": ["00000000-0000-0000-0000-000000000001"], "status": "selected"},
    )
    assert response.status_code == 404
    assert "verified candidate" in response.json()["detail"]


def test_library_status_filter_is_validated(client: TestClient) -> None:
    project = _create_project(client)
    response = client.get(f"/api/v1/projects/{project['id']}/library?status=bogus")
    assert response.status_code == 422


def test_cost_endpoint_starts_at_zero(client: TestClient) -> None:
    project = _create_project(client)
    cost = client.get(f"/api/v1/projects/{project['id']}/cost").json()
    assert cost == {
        "project_id": project["id"],
        "call_count": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_estimate": 0.0,
        # 失败调用数：draft-first 下失败会静默降级，面板必须能看见它。
        "failed_call_count": 0,
    }


def test_search_runs_empty_for_new_project(client: TestClient) -> None:
    project = _create_project(client)
    assert client.get(f"/api/v1/projects/{project['id']}/search/runs").json() == []
