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
from db import create_user, create_user_session, upsert_entry, upsert_work
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
