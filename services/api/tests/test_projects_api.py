"""M1 API 契约测试（设计 §4.7）。

覆盖项目 CRUD、SCOPE、文献库圈选与白名单，并验证 R1 在 API 边界的拦截：
不能凭一个 work id 把未核验/不存在的文献塞进项目。
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from db import (
    create_document,
    create_quality_report,
    create_user,
    create_user_session,
    upsert_entry,
    upsert_section,
    upsert_work,
)
from db.repositories.visuals import document_snapshot_hash
from db.session import make_engine, make_session_factory
from fastapi.testclient import TestClient
from paperforge_api.deps import get_queue
from paperforge_api.main import create_app
from paperforge_api.schemas import LiteratureCardResponse

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


def test_literature_card_response_preserves_structured_fulltext_locator() -> None:
    payload = LiteratureCardResponse(
        quotable_points=[
            {
                "text": "Located evidence excerpt.",
                "page": 4,
                "section": "Results",
                "paragraph": 2,
            }
        ],
        fulltext_used=True,
    ).model_dump(mode="json")

    assert payload["quotable_points"] == [
        {
            "text": "Located evidence excerpt.",
            "page": 4,
            "section": "Results",
            "paragraph": 2,
        }
    ]


@pytest.fixture
def client(clean_pg_database_url, monkeypatch):
    """TestClient 自带事件循环：让 API 在该循环内建自己的 asyncpg 连接。"""
    monkeypatch.setenv("DATABASE_URL", clean_pg_database_url)
    # 测试必须与开发者的真实 provider 配置隔离：既保证断言稳定，也不花钱。
    monkeypatch.setenv("LLM_DEFAULT_PROVIDER", "noop")
    monkeypatch.setenv("LLM_OPENAI_API_KEY", "")
    monkeypatch.setenv("VISUALS_ENABLED", "true")
    monkeypatch.setenv("AI_IMAGES_ENABLED", "false")
    monkeypatch.setenv("IMAGE_API_KEY", "")
    monkeypatch.setenv("AUTH_RATE_LIMIT_ENABLED", "false")
    import paperforge_api.config as api_config
    import paperforge_api.deps as api_deps

    api_config._settings = None
    api_deps._engine = None
    api_deps._session_factory = None

    app = create_app()
    queue = _FakeQueue()
    app.dependency_overrides[get_queue] = lambda: queue
    with TestClient(app, headers={"Origin": "http://localhost:3000"}) as test_client:
        raw_session = "integration-test-session-token"

        async def _auth_user(session):
            user = await create_user(
                session,
                email="owner@example.test",
                password_hash="!test-only",
                display_name="Test owner",
                verified=True,
            )
            await create_user_session(
                session,
                user_id=user.id,
                token_hash=hashlib.sha256(raw_session.encode()).hexdigest(),
                expires_at=datetime.now(UTC) + timedelta(days=30),
            )
            return user.id

        test_client.owner_id = seed(clean_pg_database_url, _auth_user)  # type: ignore[attr-defined]
        test_client.cookies.set("paperforge_session", raw_session)
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


def test_patch_project_updates_only_sent_fields(client: TestClient) -> None:
    """PATCH 是局部更新：没出现在 body 里的字段一个都不能动。"""
    project = _create_project(client)

    patched = client.patch(
        f"/api/v1/projects/{project['id']}",
        json={"title": "  扩散模型在医学影像分割中的研究综述  "},
    )
    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert body["title"] == "扩散模型在医学影像分割中的研究综述"  # 首尾空白被裁掉
    # 没传的字段维持原值——尤其是 topic：它和 title 同住 scope_json，
    # 早期实现整体替换 scope 时会把它一起抹掉。
    assert body["topic"] == "retrieval augmented generation"
    assert body["language"] == "en"
    assert body["citation_style"] == "author_year"
    assert body["writing_mode"] == "auto"


def test_publication_metadata_is_separate_from_internal_project_title(client: TestClient) -> None:
    project = _create_project(client)
    response = client.patch(
        f"/api/v1/projects/{project['id']}",
        json={
            "publication_title": "A Submission-ready Review",
            "authors": ["Ada Lovelace"],
            "keywords": ["evidence", "review"],
            "metadata_confirmed": True,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["title"] == "RAG for science"
    assert body["publication_title"] == "A Submission-ready Review"
    assert body["metadata_confirmed"] is True

    changed = client.patch(
        f"/api/v1/projects/{project['id']}",
        json={"publication_title": "A Revised Submission-ready Review"},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["metadata_confirmed"] is False


def test_structured_authors_preserve_order_and_derive_legacy_names(client: TestClient) -> None:
    project = _create_project(client)
    details = [
        {
            "id": "grace",
            "name": "Grace Hopper",
            "affiliations": ["Yale University"],
            "email": "grace@example.org",
            "orcid": "0000-0002-1825-0097",
            "corresponding": True,
        },
        {
            "id": "ada",
            "name": "Ada Lovelace",
            "affiliations": ["Analytical Engine Lab"],
            "corresponding": False,
        },
    ]
    response = client.patch(
        f"/api/v1/projects/{project['id']}",
        json={"author_details": details, "metadata_confirmed": True},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["authors"] == ["Grace Hopper", "Ada Lovelace"]
    assert [author["id"] for author in body["author_details"]] == ["grace", "ada"]
    assert body["author_details"][0]["orcid"] == "0000-0002-1825-0097"

    fetched = client.get(f"/api/v1/projects/{project['id']}").json()
    assert fetched["authors"] == ["Grace Hopper", "Ada Lovelace"]
    assert [author["name"] for author in fetched["author_details"]] == fetched["authors"]


def test_project_rejects_conflicting_or_invalid_author_sources(client: TestClient) -> None:
    project = _create_project(client)
    both = client.patch(
        f"/api/v1/projects/{project['id']}",
        json={
            "authors": ["Legacy Name"],
            "author_details": [{"id": "new", "name": "Structured Name"}],
        },
    )
    assert both.status_code == 422

    missing_email = client.patch(
        f"/api/v1/projects/{project['id']}",
        json={
            "author_details": [{"id": "corresponding", "name": "No Email", "corresponding": True}]
        },
    )
    assert missing_email.status_code == 422

    invalid_orcid = client.patch(
        f"/api/v1/projects/{project['id']}",
        json={
            "author_details": [
                {"id": "bad-orcid", "name": "Invalid", "orcid": "0000-0000-0000-0000"}
            ]
        },
    )
    assert invalid_orcid.status_code == 422


def test_academic_profile_is_reusable_account_data(client: TestClient) -> None:
    assert client.get("/api/v1/auth/me/academic-profile").json() == {"profile": None}
    profile = {
        "id": "identity",
        "name": "Test Researcher",
        "affiliations": ["Paper Forge Lab"],
        "email": "researcher@example.org",
        "orcid": "0000-0002-1825-0097",
        "corresponding": True,
    }
    saved = client.patch(
        "/api/v1/auth/me/academic-profile",
        json={"profile": profile},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["profile"] == profile
    assert client.get("/api/v1/auth/me/academic-profile").json()["profile"] == profile


def test_claim_evidence_rejects_malformed_report_id(client: TestClient) -> None:
    project = _create_project(client)
    response = client.get(
        f"/api/v1/projects/{project['id']}/quality/evidence",
        params={"report_id": "not-a-uuid"},
    )
    assert response.status_code == 404


def test_full_generation_accepts_quality_and_review_modes(client: TestClient) -> None:
    project = _create_project(client)
    response = client.post(
        f"/api/v1/projects/{project['id']}/generate",
        json={"quality_profile": "submission", "review_style": "systematic"},
    )
    assert response.status_code == 202, response.text
    function, _args, kwargs = client.queue.calls[-1]  # type: ignore[attr-defined]
    assert function == "run_full_pipeline"
    assert kwargs == {"quality_profile": "submission", "review_style": "systematic"}


def test_submission_export_without_current_quality_report_is_blocked(
    client: TestClient,
    clean_pg_database_url: str,
) -> None:
    project = _create_project(client)

    async def _seed_document(session):
        document = await create_document(
            session,
            project_id=uuid.UUID(project["id"]),
            outline_id=None,
        )
        await upsert_section(
            session,
            document_id=document.id,
            section_key="s1",
            title="Body",
            order_no=0,
            body_ir={
                "key": "s1",
                "level": 1,
                "title": "Body",
                "blocks": [{"type": "paragraph", "runs": [{"t": "text", "v": "Body"}]}],
            },
            cite_keys=[],
        )

    seed(clean_pg_database_url, _seed_document)
    response = client.post(
        f"/api/v1/projects/{project['id']}/exports",
        json={"formats": ["pdf"], "quality_profile": "submission"},
    )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "submission_quality_gate_failed"
    assert detail["blockers"][0]["code"] == "quality_report_missing"
    assert client.get(f"/api/v1/projects/{project['id']}/exports").json() == []


def test_submission_quality_report_becomes_stale_after_section_edit(
    client: TestClient,
    clean_pg_database_url: str,
) -> None:
    project = _create_project(client)

    async def _seed_current_report(session):
        document = await create_document(
            session,
            project_id=uuid.UUID(project["id"]),
            outline_id=None,
        )
        section = await upsert_section(
            session,
            document_id=document.id,
            section_key="s1",
            title="Body",
            order_no=0,
            body_ir={
                "key": "s1",
                "level": 1,
                "title": "Body",
                "blocks": [{"type": "paragraph", "runs": [{"t": "text", "v": "Body"}]}],
            },
            cite_keys=[],
        )
        snapshot = document_snapshot_hash([section])
        await create_quality_report(
            session,
            project_id=uuid.UUID(project["id"]),
            document_id=document.id,
            document_version=document.version,
            paper_snapshot_hash=snapshot,
            quality_profile="submission",
            review_style="narrative",
            readiness_status="preflight_ready",
            blockers=[],
            warnings=[],
            scores={},
            metrics={},
        )
        return section.updated_at

    updated_at = seed(clean_pg_database_url, _seed_current_report)
    current = client.get(
        f"/api/v1/projects/{project['id']}/quality",
        params={"quality_profile": "submission"},
    )
    assert current.status_code == 200, current.text
    assert current.json()["stale"] is False

    edited = client.put(
        f"/api/v1/projects/{project['id']}/sections/s1",
        json={
            "body_ir": {
                "key": "s1",
                "level": 1,
                "title": "Body",
                "blocks": [{"type": "paragraph", "runs": [{"t": "text", "v": "Revised body"}]}],
            },
            "expected_updated_at": updated_at.isoformat(),
        },
    )
    assert edited.status_code == 200, edited.text
    stale = client.get(
        f"/api/v1/projects/{project['id']}/quality",
        params={"quality_profile": "submission"},
    )
    assert stale.json()["stale"] is True
    blocked = client.post(
        f"/api/v1/projects/{project['id']}/exports",
        json={"formats": ["pdf"], "quality_profile": "submission"},
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["blockers"][0]["code"] == "quality_report_stale"
    assert client.get(f"/api/v1/projects/{project['id']}/exports").json() == []


def test_patch_project_preserves_generated_scope(client: TestClient) -> None:
    """改 topic 不能连带清掉 SCOPE 生成出来的关键词矩阵。"""
    project = _create_project(client)
    client.put(
        f"/api/v1/projects/{project['id']}/scope",
        json={"scope": {"topic": "old", "keyword_groups": [["diffusion", "segmentation"]]}},
    )

    client.patch(f"/api/v1/projects/{project['id']}", json={"topic": "医学影像分割"})

    scope = client.get(f"/api/v1/projects/{project['id']}/scope").json()["scope"]
    assert scope["topic"] == "医学影像分割"
    assert scope["keyword_groups"] == [["diffusion", "segmentation"]]


def test_patch_project_can_clear_topic_but_not_title(client: TestClient) -> None:
    """`null` 与「不传」不是一回事：前者是显式清空，对 title 则应当被拒。"""
    project = _create_project(client)

    cleared = client.patch(f"/api/v1/projects/{project['id']}", json={"topic": None})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["topic"] is None

    for bad in ({"title": None}, {"title": "   "}):
        response = client.patch(f"/api/v1/projects/{project['id']}", json=bad)
        assert response.status_code == 422, f"{bad} -> {response.text}"
    # 被拒之后题目仍是原来的，没有被写成空串。
    assert client.get(f"/api/v1/projects/{project['id']}").json()["title"] == "RAG for science"


def test_patch_project_rejects_bad_enum_and_paper_type(client: TestClient) -> None:
    project = _create_project(client)

    assert (
        client.patch(f"/api/v1/projects/{project['id']}", json={"language": "fr"}).status_code
        == 422
    )
    # paper_type 不在 UpdateProjectRequest 里，pydantic 默认忽略多余字段，
    # 所以这里断言的是「改不动」而不是「报错」——论文类型决定管线形状，
    # 换类型等于新建项目。
    client.patch(f"/api/v1/projects/{project['id']}", json={"paper_type": "original"})
    assert client.get(f"/api/v1/projects/{project['id']}").json()["paper_type"] == "review"


def test_patch_unknown_project_returns_404(client: TestClient) -> None:
    assert client.patch(f"/api/v1/projects/{uuid.uuid4()}", json={"title": "x"}).status_code == 404


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
        client.get(f"/api/v1/projects/{project['id']}/scope").json()["scope"]["research_question"]
        == "edited question"
    )
    # 手改过的 scope 打上 generator='user'：SEARCH 会重生成确定性回退留下的降级
    # scope，但绝不能覆盖用户自己调好的关键词。
    assert put.json()["scope"]["generator"] == "user"


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


def test_skip_polish_flags_the_running_job(client: TestClient) -> None:
    """跳过润色只写一个开关，任务继续跑——worker 写完当前这节才会看到。

    润色前稿件已完整落库，等不等它应该是用户当场的选择；此前唯一的出路是
    干等十几分钟或者把整个任务杀掉。
    """
    project = _create_project(client)
    started = client.post(
        f"/api/v1/projects/{project['id']}/search/runs",
        json={"providers": ["openalex"]},
    ).json()

    response = client.post(f"/api/v1/projects/{project['id']}/jobs/{started['id']}/polish/skip")
    assert response.status_code == 200
    assert response.json()["checkpoint"]["polish_skip"] is True
    # 幂等：重复点击不该报错。
    assert (
        client.post(
            f"/api/v1/projects/{project['id']}/jobs/{started['id']}/polish/skip"
        ).status_code
        == 200
    )
    # 任务状态不变——跳过的是一道工序，不是取消任务。
    assert (
        client.get(f"/api/v1/projects/{project['id']}/jobs/{started['id']}").json()["status"]
        == "queued"
    )


def test_skip_polish_rejects_finished_and_foreign_jobs(
    client: TestClient,
    clean_pg_database_url: str,
) -> None:
    project = _create_project(client)
    other = _create_project(client, title="另一个项目")
    started = client.post(
        f"/api/v1/projects/{project['id']}/search/runs",
        json={"providers": ["openalex"]},
    ).json()

    # 已经跑完的任务没有「剩余润色」可跳，给 409 而不是假装成功。
    async def _finish(session):
        from db import update_job
        from db.models.paper import GenerationJob

        job = await session.get(GenerationJob, uuid.UUID(started["id"]))
        await update_job(session, job, status="succeeded")

    seed(clean_pg_database_url, _finish)
    assert (
        client.post(
            f"/api/v1/projects/{project['id']}/jobs/{started['id']}/polish/skip"
        ).status_code
        == 409
    )

    # 别的项目的任务不可跨项目操作。
    assert (
        client.post(f"/api/v1/projects/{other['id']}/jobs/{started['id']}/polish/skip").status_code
        == 404
    )
    assert (
        client.post(f"/api/v1/projects/{project['id']}/jobs/{uuid.uuid4()}/polish/skip").status_code
        == 404
    )


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
    assert (
        client.get(f"/api/v1/projects/{project['id']}/library/whitelist").json()["cite_keys"] == []
    )

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


# ---- 导出产物下载 / 预览 ----


def _seed_artifact(
    database_url: str,
    project_id: str,
    *,
    fmt: str = "pdf",
    payload: bytes = b"%PDF-1.7 fake",
    root: str = "./data/objects",
) -> str:
    """写入一个产物（对象文件 + 登记行），返回 artifact id。"""
    import uuid as _uuid

    from db.models.paper import ExportArtifact
    from storage import FilesystemObjectStore

    key = f"projects/{project_id}/exports/test-{_uuid.uuid4().hex[:8]}.bin"
    FilesystemObjectStore(root).put(key, payload)

    async def _seed(session):
        row = ExportArtifact(
            project_id=_uuid.UUID(project_id),
            document_version=2,
            format=fmt,
            object_key=key,
            content_hash="deadbeef",
        )
        session.add(row)
        await session.flush()
        return str(row.id)

    return seed(database_url, _seed)


def test_export_download_is_an_attachment_with_a_readable_filename(
    client: TestClient, clean_pg_database_url, tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("STORAGE_FS_ROOT", str(tmp_path))
    import paperforge_api.config as api_config

    api_config._settings = None
    project = _create_project(client, title="面向序列推荐的投毒攻击综述", language="zh")
    artifact_id = _seed_artifact(clean_pg_database_url, project["id"], root=str(tmp_path))

    response = client.get(f"/api/v1/projects/{project['id']}/exports/{artifact_id}/download")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    disposition = response.headers["content-disposition"]
    # `paperforge-v1.pdf` 在下载目录里认不出是哪篇论文、哪一次导出。
    assert disposition.startswith("attachment;")
    assert "filename*=UTF-8''" in disposition
    assert "%E9%9D%A2%E5%90%91" in disposition  # 「面向」的 UTF-8 百分号编码
    assert "-v2-" in disposition
    # 头部只能承载 latin-1：中文进了 filename= 会让整个下载 500。
    disposition.encode("latin-1")


def test_export_preview_is_inline_so_the_iframe_renders_instead_of_downloading(
    client: TestClient, clean_pg_database_url, tmp_path, monkeypatch
) -> None:
    """`?disposition=inline`：预览框靠它渲染，否则打开导出中心就等于凭空下载。"""
    monkeypatch.setenv("STORAGE_FS_ROOT", str(tmp_path))
    import paperforge_api.config as api_config

    api_config._settings = None
    project = _create_project(client)
    artifact_id = _seed_artifact(clean_pg_database_url, project["id"], root=str(tmp_path))

    response = client.get(
        f"/api/v1/projects/{project['id']}/exports/{artifact_id}/download",
        params={"disposition": "inline"},
    )
    assert response.status_code == 200
    assert response.headers["content-disposition"].startswith("inline;")
    assert response.content == b"%PDF-1.7 fake"


def test_export_disposition_only_honours_inline(
    client: TestClient, clean_pg_database_url, tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("STORAGE_FS_ROOT", str(tmp_path))
    import paperforge_api.config as api_config

    api_config._settings = None
    project = _create_project(client)
    artifact_id = _seed_artifact(clean_pg_database_url, project["id"], root=str(tmp_path))
    response = client.get(
        f"/api/v1/projects/{project['id']}/exports/{artifact_id}/download",
        params={"disposition": "inline; rm -rf"},
    )
    # 任意取值一律回落到 attachment，不把用户输入拼进响应头。
    assert response.headers["content-disposition"].startswith("attachment;")


def test_export_filename_is_readable_for_every_format() -> None:
    from datetime import datetime

    from paperforge_api.routers.writing import export_filename

    at = datetime(2026, 7, 26)
    assert (
        export_filename(
            "RAG for Science", fmt="pdf", suffix="pdf", document_version=2, created_at=at
        )
        == "RAG-for-Science-v2-20260726.pdf"
    )
    assert (
        export_filename("综述", fmt="compile_log", suffix="log", document_version=1, created_at=at)
        == "综述-编译日志-v1-20260726.log"
    )
    # 路径穿越与空标题都不能穿透到文件名。
    assert (
        export_filename("../../etc/passwd", fmt="pdf", suffix="pdf", document_version=1)
        == "etc-passwd-v1.pdf"
    )
    assert (
        export_filename("", fmt="pdf", suffix="pdf", document_version=None) == "paperforge-v1.pdf"
    )


# ---- M8 视觉资产契约 ----


def _create_chart_visual(client: TestClient, project_id: str, asset_ref: str) -> dict[str, Any]:
    response = client.post(
        f"/api/v1/projects/{project_id}/visuals",
        json={
            "title": "Result comparison",
            "caption": "Scores by method",
            "alt_text": "Bar chart comparing method scores",
            "spec": {
                "kind": "chart",
                "chart_type": "bar",
                "source_asset_ref": asset_ref,
                "x": "method",
                "y": ["score"],
            },
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_visual_chart_source_is_project_scoped_and_delete_protected(
    client: TestClient, tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("STORAGE_FS_ROOT", str(tmp_path))
    import paperforge_api.config as api_config

    api_config._settings = None
    first = _create_project(client, paper_type="original")
    upload = client.post(
        f"/api/v1/projects/{first['id']}/assets",
        files={"file": ("results.csv", b"method,score\nA,0.8\nB,0.9\n", "text/csv")},
    )
    assert upload.status_code == 201, upload.text
    asset = upload.json()
    visual = _create_chart_visual(client, first["id"], asset["asset_ref"])
    assert visual["generation_status"] == "proposed"
    assert visual["figure_label"] == f"fig:{visual['asset_ref']}"

    blocked = client.delete(f"/api/v1/projects/{first['id']}/assets/{asset['id']}")
    assert blocked.status_code == 409

    second = _create_project(client, title="Another project")
    cross_project = client.post(
        f"/api/v1/projects/{second['id']}/visuals",
        json={
            "spec": {
                "kind": "chart",
                "chart_type": "line",
                "source_asset_ref": asset["asset_ref"],
                "x": "method",
                "y": ["score"],
            }
        },
    )
    assert cross_project.status_code == 422


def test_visual_approval_is_idempotent_and_revision_replacement_is_atomic(
    client: TestClient, clean_pg_database_url, tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("STORAGE_FS_ROOT", str(tmp_path))
    import paperforge_api.config as api_config

    api_config._settings = None
    project = _create_project(client, paper_type="original")
    upload = client.post(
        f"/api/v1/projects/{project['id']}/assets",
        files={"file": ("results.csv", b"method,score\nA,0.8\n", "text/csv")},
    ).json()
    first = _create_chart_visual(client, project["id"], upload["asset_ref"])

    async def _seed_document(session):
        from db import create_document, get_visual, upsert_section

        document = await create_document(
            session, project_id=uuid.UUID(project["id"]), outline_id=None
        )
        await upsert_section(
            session,
            document_id=document.id,
            section_key="results",
            title="Results",
            order_no=1,
            body_ir={
                "key": "results",
                "level": 1,
                "title": "Results",
                "blocks": [{"type": "paragraph", "runs": [{"t": "text", "v": "Body"}]}],
            },
            cite_keys=[],
        )
        visual = await get_visual(session, uuid.UUID(first["id"]))
        visual.generation_status = "ready"

    seed(clean_pg_database_url, _seed_document)
    endpoint = f"/api/v1/projects/{project['id']}/visuals/{first['id']}/approve"
    for _ in range(2):
        response = client.post(endpoint, json={"section_key": "results", "block_index": 1})
        assert response.status_code == 200, response.text

    revision = client.post(
        f"/api/v1/projects/{project['id']}/visuals/{first['id']}/regenerate",
        json={"caption": "Updated scores"},
    )
    assert revision.status_code == 201, revision.text
    second = revision.json()
    assert second["version"] == 2
    assert second["figure_label"] == first["figure_label"]

    async def _ready_revision(session):
        from db import get_visual

        visual = await get_visual(session, uuid.UUID(second["id"]))
        visual.generation_status = "ready"

    seed(clean_pg_database_url, _ready_revision)
    approved = client.post(
        f"/api/v1/projects/{project['id']}/visuals/{second['id']}/approve",
        json={"section_key": "results", "block_index": 0},
    )
    assert approved.status_code == 200, approved.text

    async def _read_section(session):
        from db import get_section, latest_document

        document = await latest_document(session, uuid.UUID(project["id"]))
        section = await get_section(session, document_id=document.id, section_key="results")
        return section.body_ir_json, section.asset_refs_json

    body_ir, asset_refs = seed(clean_pg_database_url, _read_section)
    figures = [block for block in body_ir["blocks"] if block["type"] == "figure"]
    assert len(figures) == 1
    assert figures[0]["asset_ref"] == second["asset_ref"]
    assert figures[0]["label"] == first["figure_label"]
    assert asset_refs == [second["asset_ref"]]


def test_visual_suggest_enqueues_only_a_plan_and_ai_generation_is_opt_in(
    client: TestClient,
) -> None:
    project = _create_project(client)
    response = client.post(f"/api/v1/projects/{project['id']}/visuals/suggest")
    assert response.status_code == 202
    function, args, _kwargs = client.queue.calls[-1]  # type: ignore[attr-defined]
    assert function == "run_visual_suggest_pipeline"
    assert args[0] == project["id"]

    proposed = client.post(
        f"/api/v1/projects/{project['id']}/visuals",
        json={
            "caption": "Concept",
            "alt_text": "Conceptual illustration",
            "spec": {
                "kind": "ai_image",
                "prompt": "An abstract scientific collaboration concept",
            },
        },
    )
    assert proposed.status_code == 201, proposed.text
    visual = proposed.json()
    # 创建建议本身不调用 provider；默认关闭 AI 时，显式 generate 也不会入队。
    queued_before = len(client.queue.calls)  # type: ignore[attr-defined]
    generated = client.post(f"/api/v1/projects/{project['id']}/visuals/{visual['id']}/generate")
    assert generated.status_code == 409
    assert len(client.queue.calls) == queued_before  # type: ignore[attr-defined]


def test_visual_summary_counts_without_loading_the_whole_list(
    client: TestClient, clean_pg_database_url, tmp_path, monkeypatch
) -> None:
    """导航状态点、概览与导出提醒都只要这几个数字。

    此前它们各自拉一遍完整视觉列表（含 spec 与 renditions）才能显示一个计数。
    """
    monkeypatch.setenv("STORAGE_FS_ROOT", str(tmp_path))
    import paperforge_api.config as api_config

    api_config._settings = None
    project = _create_project(client, paper_type="original")
    upload = client.post(
        f"/api/v1/projects/{project['id']}/assets",
        files={"file": ("results.csv", b"method,score\nA,0.8\n", "text/csv")},
    ).json()
    ready = _create_chart_visual(client, project["id"], upload["asset_ref"])
    failed = _create_chart_visual(client, project["id"], upload["asset_ref"])

    async def _set_states(session):
        from db import get_visual

        (await get_visual(session, uuid.UUID(ready["id"]))).generation_status = "ready"
        broken = await get_visual(session, uuid.UUID(failed["id"]))
        broken.generation_status = "failed"
        broken.error_code = "moderation"  # 历史词表里的旧值

    seed(clean_pg_database_url, _set_states)

    summary = client.get(f"/api/v1/projects/{project['id']}/visuals/summary")
    assert summary.status_code == 200, summary.text
    body = summary.json()
    assert body["ready"] == 1
    assert body["failed"] == 1
    assert body["approved"] == 0
    # 没有文稿就谈不上「基于旧版正文」。
    assert body["stale"] == 0


def test_visual_response_translates_legacy_error_codes_and_adds_advice(
    client: TestClient, clean_pg_database_url, tmp_path, monkeypatch
) -> None:
    """历史行存的是旧词表甚至裸类名；界面只该看到一套统一 code。"""
    monkeypatch.setenv("STORAGE_FS_ROOT", str(tmp_path))
    import paperforge_api.config as api_config

    api_config._settings = None
    project = _create_project(client, paper_type="original")
    upload = client.post(
        f"/api/v1/projects/{project['id']}/assets",
        files={"file": ("results.csv", b"method,score\nA,0.8\n", "text/csv")},
    ).json()
    visual = _create_chart_visual(client, project["id"], upload["asset_ref"])

    async def _fail_with_bare_class_name(session):
        from db import get_visual

        row = await get_visual(session, uuid.UUID(visual["id"]))
        row.generation_status = "failed"
        # 改造前落库的就是这种值。
        row.error_code = "ValueError"
        row.error_message = "boom"

    seed(clean_pg_database_url, _fail_with_bare_class_name)

    rows = client.get(f"/api/v1/projects/{project['id']}/visuals").json()
    target = next(row for row in rows if row["id"] == visual["id"])
    assert target["error"]["code"] == "internal_error"
    assert target["error"]["message"]
    assert "ValueError" not in target["error"]["message"]
    # 平铺字段在过渡期保持原样，前端切换完再废弃。
    assert target["error_code"] == "ValueError"


def test_approve_rejects_a_stale_section_version(
    client: TestClient, clean_pg_database_url, tmp_path, monkeypatch
) -> None:
    """乐观并发：章节已被别处改动时不能把插图写进过期的 IR。"""
    monkeypatch.setenv("STORAGE_FS_ROOT", str(tmp_path))
    import paperforge_api.config as api_config

    api_config._settings = None
    project = _create_project(client, paper_type="original")
    upload = client.post(
        f"/api/v1/projects/{project['id']}/assets",
        files={"file": ("results.csv", b"method,score\nA,0.8\n", "text/csv")},
    ).json()
    visual = _create_chart_visual(client, project["id"], upload["asset_ref"])

    async def _seed(session):
        from db import create_document, get_visual, upsert_section

        document = await create_document(
            session, project_id=uuid.UUID(project["id"]), outline_id=None
        )
        await upsert_section(
            session,
            document_id=document.id,
            section_key="results",
            title="Results",
            order_no=1,
            body_ir={"key": "results", "level": 1, "title": "Results", "blocks": []},
            cite_keys=[],
        )
        (await get_visual(session, uuid.UUID(visual["id"]))).generation_status = "ready"

    seed(clean_pg_database_url, _seed)

    stale = client.post(
        f"/api/v1/projects/{project['id']}/visuals/{visual['id']}/approve",
        json={
            "section_key": "results",
            "block_index": 0,
            # 客户端手上是一份很旧的版本。
            "expected_section_updated_at": "2020-01-01T00:00:00Z",
        },
    )
    assert stale.status_code == 409, stale.text
    assert stale.json()["detail"]["code"] == "section_changed"

    # 不带版本字段时行为不变——旧客户端不能因为后端升级就全部失败。
    ok_response = client.post(
        f"/api/v1/projects/{project['id']}/visuals/{visual['id']}/approve",
        json={"section_key": "results", "block_index": 0},
    )
    assert ok_response.status_code == 200, ok_response.text


def test_section_save_rejects_a_stale_base_version(
    client: TestClient, clean_pg_database_url
) -> None:
    """保存一份基于旧版本的草稿会把别处刚插入的图删掉——必须拦下来。"""
    project = _create_project(client)

    async def _seed(session):
        from db import create_document, upsert_section

        document = await create_document(
            session, project_id=uuid.UUID(project["id"]), outline_id=None
        )
        await upsert_section(
            session,
            document_id=document.id,
            section_key="introduction",
            title="Introduction",
            order_no=1,
            body_ir={"key": "introduction", "level": 1, "title": "Introduction", "blocks": []},
            cite_keys=[],
        )

    seed(clean_pg_database_url, _seed)
    body = {
        "key": "introduction",
        "level": 1,
        "title": "Introduction",
        "blocks": [{"type": "paragraph", "runs": [{"t": "text", "v": "edited"}]}],
    }

    conflict = client.put(
        f"/api/v1/projects/{project['id']}/sections/introduction",
        json={"body_ir": body, "expected_updated_at": "2020-01-01T00:00:00Z"},
    )
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["detail"]["code"] == "section_changed"

    accepted = client.put(
        f"/api/v1/projects/{project['id']}/sections/introduction",
        json={"body_ir": body},
    )
    assert accepted.status_code == 200, accepted.text


def test_settings_exposes_provider_capabilities_without_any_credentials(
    client: TestClient, monkeypatch
) -> None:
    """界面按能力声明渲染表单；这份响应里不能出现任何凭据。"""
    monkeypatch.setenv("AI_IMAGES_ENABLED", "true")
    monkeypatch.setenv("IMAGE_PROVIDER", "cloudflare")
    monkeypatch.setenv("IMAGE_API_KEY", "cf-secret-token")
    monkeypatch.setenv("IMAGE_ACCOUNT_ID", "0123456789abcdef0123456789abcdef")
    import paperforge_api.config as api_config

    api_config._settings = None
    try:
        response = client.get("/api/v1/settings")
        assert response.status_code == 200, response.text
        body = response.json()
        capabilities = body["image_capabilities"]
        assert capabilities["provider"] == "cloudflare"
        # Cloudflare FLUX 只接受 prompt 与 steps：不得声明任何尺寸，
        # 否则界面又会给出不会生效的比例下拉框。
        assert capabilities["supported_sizes"] == []
        assert capabilities["quality_modes"] == ["low", "medium", "high"]

        serialized = response.text
        assert "cf-secret-token" not in serialized
        assert "0123456789abcdef0123456789abcdef" not in serialized
    finally:
        api_config._settings = None


def test_visual_draft_fills_the_whole_spec_from_one_sentence(client: TestClient) -> None:
    """新建 AI 插图不该要求用户手写图注、替代文本和构图三段文本。

    没有配置文本模型时也必须给一份**能直接提交**的草稿，而不是把用户弹回空表单。
    """
    project = _create_project(client)
    response = client.post(
        f"/api/v1/projects/{project['id']}/visuals/draft",
        json={"kind": "ai_image", "intent": "根系受力后的断裂过程"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["caption"]
    assert body["alt_text"]
    assert body["spec"]["kind"] == "ai_image"
    # 用户的意图原样进了描述，而不是被丢掉。
    assert "根系受力后的断裂过程" in body["spec"]["prompt"]
    assert body["generator"] == "deterministic"


def test_visual_draft_never_touches_the_image_provider_or_the_database(
    client: TestClient,
) -> None:
    """草稿只是草稿：不落库、不排任务，更不会调用图像服务。"""
    project = _create_project(client)
    queued_before = len(client.queue.calls)  # type: ignore[attr-defined]

    client.post(
        f"/api/v1/projects/{project['id']}/visuals/draft",
        json={"kind": "diagram", "intent": "检测流程"},
    )

    assert len(client.queue.calls) == queued_before  # type: ignore[attr-defined]
    assert client.get(f"/api/v1/projects/{project['id']}/visuals").json() == []


def test_visual_draft_survives_intents_that_hit_the_ai_image_guardrail(
    client: TestClient,
) -> None:
    """意图里带量化表述会被 AIImageSpec 拒绝——不能把这个错误甩回界面。"""
    project = _create_project(client)
    response = client.post(
        f"/api/v1/projects/{project['id']}/visuals/draft",
        json={"kind": "ai_image", "intent": "准确率 95% 的对比柱状图"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["spec"]["kind"] == "ai_image"


def test_visual_draft_matches_real_section_content_instead_of_first_section(
    client: TestClient, clean_pg_database_url
) -> None:
    project = _create_project(client)

    async def _seed_sections(session):
        document = await create_document(
            session, project_id=uuid.UUID(project["id"]), outline_id=None
        )
        for order, key, title, body_text in [
            (0, "background", "研究背景", "这里讨论文献范围和研究问题。"),
            (1, "mechanism", "作用机制", "根系断裂后，防御方法会改变应力传递路径。"),
        ]:
            await upsert_section(
                session,
                document_id=document.id,
                section_key=key,
                title=title,
                order_no=order,
                body_ir={
                    "key": key,
                    "level": 1,
                    "title": title,
                    "blocks": [{"type": "paragraph", "runs": [{"t": "text", "v": body_text}]}],
                },
                cite_keys=[],
            )

    seed(clean_pg_database_url, _seed_sections)
    response = client.post(
        f"/api/v1/projects/{project['id']}/visuals/draft",
        json={"kind": "auto", "intent": "展示根系断裂与防御方法的关系"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["target_section_key"] == "mechanism"
    assert "根系断裂" in body["context_summary"]
    assert body["suggested_block_index"] == 1
    assert body["kind"] == "diagram"


def test_visual_auto_selects_traceable_chart_but_never_invents_one_without_data(
    client: TestClient, tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("STORAGE_FS_ROOT", str(tmp_path))
    import paperforge_api.config as api_config

    api_config._settings = None
    with_data = _create_project(client, paper_type="original")
    upload = client.post(
        f"/api/v1/projects/{with_data['id']}/assets",
        files={"file": ("results.csv", b"method,score\nA,0.8\nB,0.9\n", "text/csv")},
    )
    assert upload.status_code == 201, upload.text
    chart = client.post(
        f"/api/v1/projects/{with_data['id']}/visuals/draft",
        json={"kind": "auto", "intent": "比较不同方法的性能"},
    )
    assert chart.status_code == 200, chart.text
    assert chart.json()["kind"] == "chart"
    assert chart.json()["spec"]["source_asset_ref"] == upload.json()["asset_ref"]

    without_data = _create_project(client, title="No data")
    fallback = client.post(
        f"/api/v1/projects/{without_data['id']}/visuals/draft",
        json={"kind": "chart", "intent": "比较不同方法的性能"},
    )
    assert fallback.status_code == 200, fallback.text
    assert fallback.json()["kind"] == "diagram"
    assert "避免编造数据" in fallback.json()["warnings"][0]


def test_visual_natural_language_revision_creates_a_new_version(client: TestClient) -> None:
    project = _create_project(client)
    original = client.post(
        f"/api/v1/projects/{project['id']}/visuals",
        json={
            "title": "研究流程",
            "caption": "研究流程",
            "alt_text": "三阶段研究流程",
            "spec": {
                "kind": "diagram",
                "direction": "TB",
                "nodes": [
                    {"id": "n1", "label": "输入"},
                    {"id": "n2", "label": "处理"},
                    {"id": "n3", "label": "输出"},
                ],
                "edges": [
                    {"source": "n1", "target": "n2"},
                    {"source": "n2", "target": "n3"},
                ],
            },
        },
    )
    assert original.status_code == 201, original.text
    revised = client.post(
        f"/api/v1/projects/{project['id']}/visuals/{original.json()['id']}/regenerate",
        json={"revision_instruction": "改成横向"},
    )
    assert revised.status_code == 201, revised.text
    body = revised.json()
    assert body["id"] != original.json()["id"]
    assert body["version"] == original.json()["version"] + 1
    assert body["supersedes_id"] == original.json()["id"]
    assert body["spec"]["direction"] == "LR"
