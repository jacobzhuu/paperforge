"""M1 API 契约测试（设计 §4.7）。

覆盖项目 CRUD、SCOPE、文献库圈选与白名单，并验证 R1 在 API 边界的拦截：
不能凭一个 work id 把未核验/不存在的文献塞进项目。
"""

from __future__ import annotations

import hashlib
import io
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from db import (
    create_document,
    create_quality_report,
    create_user,
    create_user_session,
    get_job,
    replace_citation_usage,
    update_job,
    upsert_entry,
    upsert_section,
    upsert_work,
)
from db.models.library import (
    DocumentFile,
    DocumentParse,
    LiteraturePdfUpload,
    ScholarlyWork,
)
from db.models.paper import GenerationJob
from db.repositories.evidence import upsert_evidence_unit
from db.repositories.visuals import document_snapshot_hash
from db.session import make_engine, make_session_factory
from fastapi.testclient import TestClient
from paperforge_api.deps import get_queue
from paperforge_api.main import create_app
from paperforge_api.schemas import LiteratureCardResponse
from sqlalchemy import select

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
        #: arq 任务 id 与管线参数分开记：断言关心的是「这条管线带了什么参数」，
        #: 而 ``_job_id`` 是派发管道自己的东西。
        self.job_ids: list[str | None] = []

    async def enqueue_job(self, function: str, *args: Any, **kwargs: Any) -> Any:
        arq_job_id = kwargs.pop("_job_id", None)
        self.calls.append((function, args, kwargs))
        self.job_ids.append(arq_job_id)
        # 真实的 arq 返回 Job；返回 None 表示「这个 id 已经在队列里，没有入队」，
        # 而 start_job 现在把那当成派发失败。替身必须照实区分这两件事。
        return SimpleNamespace(job_id=arq_job_id)


class _FailingQueue:
    async def enqueue_job(self, function: str, *args: Any, **kwargs: Any) -> None:
        raise ConnectionError("queue unavailable during dispatch")


class _DedupedQueue:
    """arq 在任务 id 撞车时返回 ``None``——那表示**没有**入队。"""

    async def enqueue_job(self, function: str, *args: Any, **kwargs: Any) -> None:
        return None


def _install_deepseek_image_stub(monkeypatch, *, captured: list[dict[str, Any]] | None = None):
    """让 AI 草稿测试验证全文链路，但绝不访问真实 DeepSeek。"""

    class StubRunner:
        enabled = True

        def __init__(self, *args, **kwargs) -> None:
            pass

        async def agenerate_json(self, role: str, **kwargs: Any):
            if captured is not None:
                captured.append({"role": role, **kwargs})
            return SimpleNamespace(
                ok=True,
                model="deepseek-test",
                value={
                    "title": "论文级综述图",
                    "caption": "结合论文全文呈现核心机制、方法分类与开放问题。",
                    "alt_text": "横向综述图展示核心机制、方法分类、防御与开放问题。",
                    "subject": "论文全文的核心研究主题与证据综合",
                    "composition": "横向阅读，中心主题连接方法分类、机制、防御和未来方向",
                    "elements": ["核心问题", "方法分类", "攻击机制", "防御策略", "开放问题"],
                    "text_policy": "auto",
                    "prompt": (
                        "Create a publication-ready horizontal graphical abstract grounded in "
                        "the complete paper. Organize the central research theme with clearly "
                        "connected method, mechanism, defense, and future-direction groups. Use "
                        "a restrained scientific palette, crisp journal-quality rendering, and "
                        "only the requested correctly spelled labels."
                    ),
                },
            )

    import llm_runtime

    monkeypatch.setattr(llm_runtime, "LLMRunner", StubRunner)


def _pdf_bytes() -> bytes:
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.add_metadata(
        {
            "/Title": "Retrieval Augmented Generation",
            "/Author": "Ada Lovelace",
            "/Subject": "doi:10.1000/rag",
        }
    )
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


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


def drain_jobs(database_url: str, project_id: str) -> int:
    """把该项目所有排队中的任务标成 succeeded，替代不存在的 worker。

    并发任务槽（``paperforge_api.jobs.ensure_project_job_slot``）按设计拒绝同项目的
    第二条产物任务——生产里这是对的，上一条真的还在跑。但测试用的是只记录调用的
    假队列，任务永远停在 ``queued``，于是「PDF 匹配失败后重试」「首稿完成后再起
    一轮」这类本来合法的第二步全被 409 挡掉，断言还没走到就先炸了。

    这个 helper 只做 worker 迟早会做的事：把上一条任务收尾。它显式调用而不是做成
    autouse fixture——并发槽本身的行为由 test_job_serialization.py 直接断言，
    而这里每一次调用都标出了「现实中这一步之前上一条任务已经跑完了」，
    自动排空会把这个前提藏起来。

    返回收尾的任务条数，便于调用方确认自己排空的正是预期的那一条。
    """

    async def _finish(session):
        rows = (
            await session.scalars(
                select(GenerationJob).where(
                    GenerationJob.project_id == uuid.UUID(project_id),
                    GenerationJob.status.in_({"queued", "running"}),
                )
            )
        ).all()
        for job in rows:
            job.status = "succeeded"
        return len(rows)

    return seed(database_url, _finish)


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


def test_full_generation_defaults_to_delivering_a_draft(client: TestClient) -> None:
    """不带 options 的 /generate 必须默认 draft。

    共用的 GenerationOptionsRequest 默认 scholarly，对单跑质量端点是对的，
    对全管线不是：scholarly 会在写完后追加收敛循环，而且没过质量门连导出都不做，
    于是「跑通全流程」默认可能不产出任何稿件。
    """
    project = _create_project(client)
    started = client.post(f"/api/v1/projects/{project['id']}/generate")
    assert started.status_code == 202
    function, _args, kwargs = client.queue.calls[-1]  # type: ignore[attr-defined]
    assert function == "run_full_pipeline"
    assert kwargs == {"quality_profile": "draft", "review_style": "narrative"}
    # 队列里的任务 id 就是 generation_job 的 id。没有这条对应关系，就没法回答
    # 「这条 queued 的行还在队列里吗」——而那正是分辨「排队中」和「已经没人在跑」
    # 的唯一可信信号。
    assert client.queue.job_ids[-1] == started.json()["id"]  # type: ignore[attr-defined]


def test_a_job_that_never_reaches_the_queue_is_reported_not_silently_left_queued(
    client: TestClient,
) -> None:
    """入队失败此前是静默的：行建好了、队列里没有，于是它永远停在 queued。

    那条行还会通过 ``ensure_project_job_slot`` 把整个项目锁死——用户此后点什么都是
    409，界面上也没有任何解释。
    """
    project = _create_project(client)
    client.app.dependency_overrides[get_queue] = lambda: _DedupedQueue()  # type: ignore[attr-defined]
    try:
        response = client.post(f"/api/v1/projects/{project['id']}/generate")
    finally:
        client.app.dependency_overrides[get_queue] = lambda: client.queue  # type: ignore[attr-defined]
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "job_enqueue_failed"


def test_delivered_full_job_can_start_or_skip_quality_repair(
    client: TestClient,
    clean_pg_database_url: str,
) -> None:
    """质量修复和润色同形：交付之后由用户决定，且只消费一次。"""
    project = _create_project(client)
    started = client.post(
        f"/api/v1/projects/{project['id']}/generate",
        json={"quality_profile": "draft", "review_style": "systematic"},
    ).json()

    async def _deliver_with_findings(session):
        job = await get_job(session, uuid.UUID(started["id"]))
        await create_document(session, project_id=uuid.UUID(project["id"]), outline_id=None)
        await update_job(
            session,
            job,
            status="succeeded",
            checkpoint={"quality_repair_decision": "pending", "quality_finding_count": 3},
        )

    seed(clean_pg_database_url, _deliver_with_findings)
    response = client.post(f"/api/v1/projects/{project['id']}/jobs/{started['id']}/quality-repair")
    assert response.status_code == 202, response.text
    repair = response.json()
    assert repair["kind"] == "write"
    function, _args, kwargs = client.queue.calls[-1]  # type: ignore[attr-defined]
    assert function == "run_quality_repair_pipeline"
    # 档位固定 scholarly：全流程用 draft 跑完，发现项还挂在 warnings 上，
    # 只有按严谨档重评它们才会变成阻断项、进而驱动收敛器重写章节。
    assert kwargs == {
        "source_job_id": started["id"],
        "quality_profile": "scholarly",
        "review_style": "systematic",
    }
    source = client.get(f"/api/v1/projects/{project['id']}/jobs/{started['id']}").json()
    assert source["checkpoint"]["quality_repair_decision"] == "started"
    assert source["checkpoint"]["quality_repair_job_id"] == repair["id"]
    assert (
        client.post(
            f"/api/v1/projects/{project['id']}/jobs/{started['id']}/quality-repair"
        ).status_code
        == 409
    )

    # 第二个项目而不是同一个项目再跑一次 /generate：并发任务槽只允许一条在飞的
    # 产物任务，而测试里没有 worker 去把上一条排空，同项目的第二次 enqueue 会 409。
    other = _create_project(client)
    skipped_source = client.post(f"/api/v1/projects/{other['id']}/generate").json()

    async def _deliver_for_skip(session):
        job = await get_job(session, uuid.UUID(skipped_source["id"]))
        await update_job(
            session,
            job,
            status="succeeded",
            checkpoint={"quality_repair_decision": "pending", "quality_finding_count": 1},
        )

    seed(clean_pg_database_url, _deliver_for_skip)
    skipped = client.post(
        f"/api/v1/projects/{other['id']}/jobs/{skipped_source['id']}/quality-repair/skip"
    )
    assert skipped.status_code == 200
    assert skipped.json()["checkpoint"]["quality_repair_decision"] == "skipped"


def test_quality_repair_enqueues_bounded_repair_pipeline(client: TestClient) -> None:
    project = _create_project(client)
    response = client.post(
        f"/api/v1/projects/{project['id']}/quality/repair",
        json={"quality_profile": "scholarly", "review_style": "narrative"},
    )
    assert response.status_code == 202, response.text
    function, _args, kwargs = client.queue.calls[-1]  # type: ignore[attr-defined]
    assert function == "run_quality_repair_pipeline"
    assert kwargs == {"quality_profile": "scholarly", "review_style": "narrative"}


def test_original_full_generation_requires_parseable_method_and_result_materials(
    client: TestClient,
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("STORAGE_FS_ROOT", str(tmp_path))
    import paperforge_api.config as api_config

    api_config._settings = None
    project = _create_project(client, paper_type="original")
    endpoint = f"/api/v1/projects/{project['id']}/generate"

    missing = client.post(endpoint, json={})
    assert missing.status_code == 409
    detail = missing.json()["detail"]
    assert detail["code"] == "original_materials_required"
    assert {issue["code"] for issue in detail["issues"]} == {
        "result_material_missing",
        "method_material_missing",
    }

    table = client.post(
        f"/api/v1/projects/{project['id']}/assets",
        files={"file": ("results.csv", b"model,accuracy\nA,92.5%\n", "text/csv")},
    )
    assert table.status_code == 201, table.text
    still_missing = client.post(endpoint, json={})
    assert still_missing.status_code == 409
    assert [issue["code"] for issue in still_missing.json()["detail"]["issues"]] == [
        "method_material_missing"
    ]

    method = client.post(
        f"/api/v1/projects/{project['id']}/assets",
        files={
            "file": (
                "method.md",
                b"Samples were normalized before model training and evaluation.",
                "text/markdown",
            )
        },
    )
    assert method.status_code == 201, method.text
    started = client.post(endpoint, json={})
    assert started.status_code == 202, started.text
    assert started.json()["kind"] == "full"
    blocked_delete = client.delete(f"/api/v1/projects/{project['id']}/assets/{method.json()['id']}")
    assert blocked_delete.status_code == 409


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
    assert detail["code"] == "quality_gate_failed"
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


def test_finished_full_job_can_start_or_skip_optional_polish(
    client: TestClient,
    clean_pg_database_url: str,
) -> None:
    """首稿完成后由用户二选一；润色作为独立任务，不重跑全文管线。"""
    project = _create_project(client)
    started = client.post(
        f"/api/v1/projects/{project['id']}/generate",
        json={"quality_profile": "submission", "review_style": "systematic"},
    ).json()

    async def _finish_with_first_draft(session):
        job = await get_job(session, uuid.UUID(started["id"]))
        document = await create_document(
            session,
            project_id=uuid.UUID(project["id"]),
            outline_id=None,
        )
        await update_job(
            session,
            job,
            status="succeeded",
            checkpoint={
                "polish_decision": "pending",
                "write_document_id": str(document.id),
            },
        )
        return str(document.id)

    document_id = seed(clean_pg_database_url, _finish_with_first_draft)
    response = client.post(f"/api/v1/projects/{project['id']}/jobs/{started['id']}/polish")
    assert response.status_code == 202, response.text
    polished = response.json()
    assert polished["kind"] == "write"
    assert polished["checkpoint"]["write_document_id"] == document_id
    function, _args, kwargs = client.queue.calls[-1]  # type: ignore[attr-defined]
    assert function == "run_polish_pipeline"
    assert kwargs == {
        "source_job_id": started["id"],
        "quality_profile": "submission",
        "review_style": "systematic",
    }
    source = client.get(f"/api/v1/projects/{project['id']}/jobs/{started['id']}").json()
    assert source["checkpoint"]["polish_decision"] == "started"
    assert source["checkpoint"]["polish_job_id"] == polished["id"]
    assert (
        client.post(f"/api/v1/projects/{project['id']}/jobs/{started['id']}/polish").status_code
        == 409
    )

    # 起第二轮之前，用户显式启动的那条润色任务在现实里已经跑完了。
    assert drain_jobs(clean_pg_database_url, project["id"]) == 1
    skipped_source = client.post(f"/api/v1/projects/{project['id']}/generate").json()

    async def _finish_for_skip(session):
        job = await get_job(session, uuid.UUID(skipped_source["id"]))
        await update_job(
            session,
            job,
            status="succeeded",
            checkpoint={"polish_decision": "pending"},
        )

    seed(clean_pg_database_url, _finish_for_skip)
    skipped = client.post(
        f"/api/v1/projects/{project['id']}/jobs/{skipped_source['id']}/polish/skip"
    )
    assert skipped.status_code == 200
    assert skipped.json()["checkpoint"]["polish_decision"] == "skipped"


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


def test_pdf_upload_is_private_and_enqueues_matching(
    client: TestClient,
    clean_pg_database_url: str,
) -> None:
    project = _create_project(client)
    uploaded = client.post(
        f"/api/v1/projects/{project['id']}/library/pdf-uploads",
        files={"file": ("paper.pdf", _pdf_bytes(), "application/pdf")},
    )
    assert uploaded.status_code == 202, uploaded.text
    body = uploaded.json()
    assert body["upload"]["status"] == "matching"
    assert body["upload"]["filename"] == "paper.pdf"
    function, _args, kwargs = client.queue.calls[-1]  # type: ignore[attr-defined]
    assert function == "run_pdf_match_pipeline"
    assert kwargs["upload_id"] == body["upload"]["id"]
    assert client.queue.job_ids[-1] == body["job"]["id"]  # type: ignore[attr-defined]

    async def _load(session):
        row = await session.get(LiteraturePdfUpload, uuid.UUID(body["upload"]["id"]))
        return row.project_id, row.object_key

    owner_project_id, object_key = seed(clean_pg_database_url, _load)
    assert owner_project_id == uuid.UUID(project["id"])
    assert object_key.startswith(
        f"users/{client.owner_id}/projects/{project['id']}/literature/"  # type: ignore[attr-defined]
    )

    other = _create_project(client, title="Other project")
    assert client.get(f"/api/v1/projects/{other['id']}/library/pdf-uploads").json() == []
    assert (
        client.get(
            f"/api/v1/projects/{other['id']}/library/pdf-uploads/{body['upload']['id']}/download"
        ).status_code
        == 404
    )

    async def _fail_match(session):
        row = await session.get(LiteraturePdfUpload, uuid.UUID(body["upload"]["id"]))
        row.status = "match_failed"
        row.error_json = {"reason": "verification_failed"}

    seed(clean_pg_database_url, _fail_match)
    # 匹配任务失败之后才谈得上重试；这里替 worker 把它收尾。
    assert drain_jobs(clean_pg_database_url, project["id"]) == 1
    retried = client.post(
        f"/api/v1/projects/{project['id']}/library/pdf-uploads/{body['upload']['id']}/retry"
    )
    assert retried.status_code == 202, retried.text
    retried_body = retried.json()
    assert retried_body["upload"]["status"] == "matching"
    function, _args, kwargs = client.queue.calls[-1]  # type: ignore[attr-defined]
    assert function == "run_pdf_match_pipeline"
    assert kwargs["upload_id"] == body["upload"]["id"]
    assert client.queue.job_ids[-1] == retried_body["job"]["id"]  # type: ignore[attr-defined]


def test_pdf_upload_enqueue_failure_is_committed_as_retryable(
    client: TestClient,
    clean_pg_database_url: str,
) -> None:
    project = _create_project(client, title="Recoverable PDF dispatch")
    client.app.dependency_overrides[get_queue] = lambda: _FailingQueue()

    response = client.post(
        f"/api/v1/projects/{project['id']}/library/pdf-uploads",
        files={"file": ("paper.pdf", _pdf_bytes(), "application/pdf")},
    )
    assert response.status_code == 503, response.text

    uploads = client.get(f"/api/v1/projects/{project['id']}/library/pdf-uploads").json()
    assert len(uploads) == 1
    assert uploads[0]["status"] == "match_failed"
    assert uploads[0]["error"]["reason"] == "task_enqueue_failed"
    downloaded = client.get(
        f"/api/v1/projects/{project['id']}/library/pdf-uploads/{uploads[0]['id']}/download"
    )
    assert downloaded.status_code == 200

    async def _job_state(session):
        row = await session.scalar(
            select(GenerationJob).where(GenerationJob.project_id == uuid.UUID(project["id"]))
        )
        return row.status, row.error_json

    job_status, error = seed(clean_pg_database_url, _job_state)
    assert job_status == "failed"
    assert error["reason"] == "task_enqueue_failed"


def test_cancelling_queued_pdf_job_makes_upload_retryable(client: TestClient) -> None:
    project = _create_project(client, title="Cancelled PDF match")
    started = client.post(
        f"/api/v1/projects/{project['id']}/library/pdf-uploads",
        files={"file": ("paper.pdf", _pdf_bytes(), "application/pdf")},
    ).json()

    cancelled = client.post(f"/api/v1/projects/{project['id']}/jobs/{started['job']['id']}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"

    uploads = client.get(f"/api/v1/projects/{project['id']}/library/pdf-uploads").json()
    assert uploads[0]["status"] == "match_failed"
    assert uploads[0]["error"] == {
        "reason": "job_cancelled",
        "job_id": started["job"]["id"],
    }
    retried = client.post(
        f"/api/v1/projects/{project['id']}/library/pdf-uploads/{started['upload']['id']}/retry"
    )
    assert retried.status_code == 202, retried.text
    assert retried.json()["upload"]["status"] == "matching"


def test_running_pdf_job_cannot_be_paused(
    client: TestClient,
    clean_pg_database_url: str,
) -> None:
    project = _create_project(client, title="Atomic PDF match")
    started = client.post(
        f"/api/v1/projects/{project['id']}/library/pdf-uploads",
        files={"file": ("paper.pdf", _pdf_bytes(), "application/pdf")},
    ).json()

    async def _run(session):
        row = await get_job(session, uuid.UUID(started["job"]["id"]))
        await update_job(session, row, status="running")

    seed(clean_pg_database_url, _run)
    paused = client.post(f"/api/v1/projects/{project['id']}/jobs/{started['job']['id']}/pause")
    assert paused.status_code == 409
    assert "retry the upload" in paused.json()["detail"]


def test_confirmed_pdf_binds_private_document_and_enters_library(
    client: TestClient,
    clean_pg_database_url: str,
) -> None:
    project = _create_project(client)
    uploaded = client.post(
        f"/api/v1/projects/{project['id']}/library/pdf-uploads",
        files={"file": ("paper.pdf", _pdf_bytes(), "application/pdf")},
    ).json()["upload"]

    async def _match(session):
        work, _ = await upsert_work(session, _Candidate())
        # Confirming a PDF is an explicit re-admission action, even if this
        # scholarly work had previously been excluded from the project.
        await upsert_entry(
            session,
            project_id=uuid.UUID(project["id"]),
            work_id=work.id,
            added_via="pdf_upload",
            status="excluded",
            verified=True,
        )
        row = await session.get(LiteraturePdfUpload, uuid.UUID(uploaded["id"]))
        row.matched_work_id = work.id
        row.match_method = "doi_existing"
        row.match_confidence = 1.0
        row.extracted_metadata_json = {
            "doi": "10.1000/rag",
            "title": "Retrieval Augmented Generation",
            "authors": ["Ada Lovelace"],
            "publication_year": 2023,
        }
        # A re-upload of identical bytes must repair/rebind the existing
        # project-private document instead of creating a duplicate or parsing
        # a stale/missing object.
        session.add(
            DocumentFile(
                project_id=uuid.UUID(project["id"]),
                access_scope="private",
                work_id=work.id,
                kind="uploaded_pdf",
                object_key="users/stale/missing.pdf",
                mime="application/pdf",
                bytes=row.bytes,
                content_hash=row.content_hash,
            )
        )
        row.status = "needs_confirmation"
        return str(work.id), row.object_key

    work_id, uploaded_object_key = seed(clean_pg_database_url, _match)
    # 现实里这一步之前匹配任务已经跑完（upload.status 才会走到 needs_confirmation）。
    assert drain_jobs(clean_pg_database_url, project["id"]) == 1
    confirmed = client.post(
        f"/api/v1/projects/{project['id']}/library/pdf-uploads/{uploaded['id']}/confirm",
        json={"literature_role": "core"},
    )
    assert confirmed.status_code == 202, confirmed.text
    confirmed_body = confirmed.json()
    assert confirmed_body["upload"]["status"] == "parsing"
    function, _args, kwargs = client.queue.calls[-1]  # type: ignore[attr-defined]
    assert function == "run_uploaded_pdf_pipeline"
    assert kwargs["upload_id"] == uploaded["id"]
    assert client.queue.job_ids[-1] == confirmed_body["job"]["id"]  # type: ignore[attr-defined]

    library = client.get(f"/api/v1/projects/{project['id']}/library").json()
    assert len(library) == 1
    assert library[0]["work"]["id"] == work_id
    assert library[0]["status"] == "selected"
    assert library[0]["added_via"] == "pdf_upload"
    assert library[0]["literature_role"] == "core"
    assert library[0]["bibtex_key"]
    assert library[0]["utilization"]["fulltext_status"] == "parsing"
    assert library[0]["utilization"]["fulltext_source"] == "user_pdf"

    async def _document_scope(session):
        documents = list(
            (
                await session.scalars(
                    select(DocumentFile).where(DocumentFile.project_id == uuid.UUID(project["id"]))
                )
            ).all()
        )
        return (
            len(documents),
            documents[0].access_scope,
            documents[0].work_id,
            documents[0].object_key,
        )

    document_count, access_scope, bound_work_id, object_key = seed(
        clean_pg_database_url,
        _document_scope,
    )
    assert document_count == 1
    assert access_scope == "private"
    assert bound_work_id == uuid.UUID(work_id)
    assert object_key == uploaded_object_key
    downloaded = client.get(
        f"/api/v1/projects/{project['id']}/library/pdf-uploads/{uploaded['id']}/download"
    )
    assert downloaded.status_code == 200
    assert downloaded.content.startswith(b"%PDF-")
    assert downloaded.headers["cache-control"] == "private, no-store"
    assert downloaded.headers["x-content-type-options"] == "nosniff"

    async def _fail_parse(session):
        row = await session.get(LiteraturePdfUpload, uuid.UUID(uploaded["id"]))
        row.status = "parse_failed"
        row.error_json = {"reason": "temporary_parser_failure"}

    seed(clean_pg_database_url, _fail_parse)
    # 解析任务失败之后才谈得上重试；这里替 worker 把它收尾。
    assert drain_jobs(clean_pg_database_url, project["id"]) == 1
    retried = client.post(
        f"/api/v1/projects/{project['id']}/library/pdf-uploads/{uploaded['id']}/retry"
    )
    assert retried.status_code == 202, retried.text
    retried_body = retried.json()
    assert retried_body["upload"]["status"] == "parsing"
    function, _args, kwargs = client.queue.calls[-1]  # type: ignore[attr-defined]
    assert function == "run_uploaded_pdf_pipeline"
    assert kwargs["upload_id"] == uploaded["id"]
    assert client.queue.job_ids[-1] == retried_body["job"]["id"]  # type: ignore[attr-defined]


def test_private_pdf_utilization_does_not_leak_to_another_project(
    client: TestClient,
    clean_pg_database_url: str,
) -> None:
    first = _create_project(client, title="Private source owner")
    second = _create_project(client, title="Same work without private source")

    async def _seed(session):
        work, _ = await upsert_work(session, _Candidate())
        for project in (first, second):
            await upsert_entry(
                session,
                project_id=uuid.UUID(project["id"]),
                work_id=work.id,
                added_via="search",
                status="selected",
                verified=True,
            )
        document = DocumentFile(
            project_id=uuid.UUID(first["id"]),
            access_scope="private",
            work_id=work.id,
            kind="uploaded_pdf",
            object_key=(
                f"users/{client.owner_id}/projects/{first['id']}/literature/private.pdf"  # type: ignore[attr-defined]
            ),
            mime="application/pdf",
            bytes=12,
            content_hash="a" * 64,
        )
        session.add(document)
        await session.flush()
        session.add(
            DocumentParse(
                document_file_id=document.id,
                parser_version="ingest_fulltext_v2",
                status="parsed",
                extracted_text="Private full-text evidence.",
            )
        )
        await upsert_evidence_unit(
            session,
            work_id=work.id,
            project_id=uuid.UUID(first["id"]),
            kind="experimental_fact",
            grade="B_located_prose",
            text="Private full-text evidence.",
            text_hash="b" * 64,
            source_document_file_id=document.id,
        )
        return str(work.id)

    work_id = seed(clean_pg_database_url, _seed)
    first_entry = next(
        item
        for item in client.get(f"/api/v1/projects/{first['id']}/library").json()
        if item["work"]["id"] == work_id
    )
    second_entry = next(
        item
        for item in client.get(f"/api/v1/projects/{second['id']}/library").json()
        if item["work"]["id"] == work_id
    )

    assert first_entry["utilization"]["fulltext_status"] == "available"
    assert first_entry["utilization"]["fulltext_source"] == "user_pdf"
    assert first_entry["utilization"]["evidence_count"] == 1
    assert second_entry["utilization"]["fulltext_status"] == "abstract_only"
    assert second_entry["utilization"]["fulltext_source"] == "none"
    assert second_entry["utilization"]["evidence_count"] == 0


def test_library_utilization_counts_only_the_latest_document(
    client: TestClient,
    clean_pg_database_url: str,
) -> None:
    project = _create_project(client, title="Current manuscript utilization")

    async def _seed(session):
        work, _ = await upsert_work(session, _Candidate())
        entry, _ = await upsert_entry(
            session,
            project_id=uuid.UUID(project["id"]),
            work_id=work.id,
            added_via="search",
            status="selected",
            verified=True,
        )
        entry.bibtex_key = "lovelace2023rag"
        old_document = await create_document(
            session,
            project_id=uuid.UUID(project["id"]),
            outline_id=None,
        )
        old_section = await upsert_section(
            session,
            document_id=old_document.id,
            section_key="old",
            title="Old draft",
            order_no=1,
            body_ir={"blocks": []},
            cite_keys=[entry.bibtex_key],
        )
        await replace_citation_usage(
            session,
            project_id=uuid.UUID(project["id"]),
            section_id=old_section.id,
            usages=[
                {
                    "work_id": work.id,
                    "cite_key": entry.bibtex_key,
                    "context_snippet": "Only the old draft cited this work.",
                }
            ],
        )
        current_document = await create_document(
            session,
            project_id=uuid.UUID(project["id"]),
            outline_id=None,
        )
        await upsert_section(
            session,
            document_id=current_document.id,
            section_key="current",
            title="Current draft",
            order_no=1,
            body_ir={"blocks": []},
            cite_keys=[],
        )
        return str(work.id)

    work_id = seed(clean_pg_database_url, _seed)
    entry = next(
        item
        for item in client.get(f"/api/v1/projects/{project['id']}/library").json()
        if item["work"]["id"] == work_id
    )
    assert entry["utilization"]["usage_evaluated"] is True
    assert entry["utilization"]["citation_status"] == "not_cited"
    assert entry["utilization"]["citation_count"] == 0
    assert entry["utilization"]["unused_reason"] == "只有摘要，建议上传 PDF 全文"


def test_fulltext_status_keeps_source_and_progress_from_the_same_document_class(
    client: TestClient,
    clean_pg_database_url: str,
) -> None:
    project = _create_project(client, title="Mixed full-text sources")

    async def _seed(session):
        work = ScholarlyWork(
            canonical_title="Mixed private and shared source",
            is_retracted=False,
        )
        session.add(work)
        await session.flush()
        await upsert_entry(
            session,
            project_id=uuid.UUID(project["id"]),
            work_id=work.id,
            added_via="search",
            status="selected",
            verified=True,
        )
        shared = DocumentFile(
            project_id=None,
            access_scope="shared",
            work_id=work.id,
            kind="oa_pdf",
            object_key=f"shared/oa/works/{work.id}/failed.pdf",
            mime="application/pdf",
            bytes=10,
            content_hash="c" * 64,
        )
        private = DocumentFile(
            project_id=uuid.UUID(project["id"]),
            access_scope="private",
            work_id=work.id,
            kind="uploaded_pdf",
            object_key=(
                f"users/{client.owner_id}/projects/{project['id']}/literature/pending.pdf"  # type: ignore[attr-defined]
            ),
            mime="application/pdf",
            bytes=10,
            content_hash="d" * 64,
        )
        session.add_all([shared, private])
        await session.flush()
        session.add(
            DocumentParse(
                document_file_id=shared.id,
                parser_version="ingest_fulltext_v2",
                status="failed",
                error_json={"reason": "bad OA copy"},
            )
        )
        return str(work.id)

    work_id = seed(clean_pg_database_url, _seed)
    entry = next(
        item
        for item in client.get(f"/api/v1/projects/{project['id']}/library").json()
        if item["work"]["id"] == work_id
    )
    assert entry["utilization"]["fulltext_status"] == "parsing"
    assert entry["utilization"]["fulltext_source"] == "user_pdf"


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
    assert selected.json()[0]["user_pinned"] is True

    whitelist = client.get(f"/api/v1/projects/{project['id']}/library/whitelist").json()
    assert len(whitelist["cite_keys"]) == 1
    assert whitelist["cite_keys"][0].startswith("lovelace2023")


def test_evidence_endpoints_expose_selected_work_titles(
    client: TestClient,
    clean_pg_database_url: str,
) -> None:
    """证据视图的题名取自 ScholarlyWork.canonical_title。

    该模型没有 ``title`` 字段，早期实现读错字段名，导致项目一旦有 selected 文献，
    证据单元与证据矩阵两个端点就整体 500——前端只能显示模块级报错。
    """
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
    assert (
        client.post(
            f"/api/v1/projects/{project['id']}/library/entries",
            json={"work_ids": [work_id], "status": "selected"},
        ).status_code
        == 200
    )

    async def _seed_evidence(session):
        await upsert_evidence_unit(
            session,
            work_id=uuid.UUID(work_id),
            project_id=uuid.UUID(project["id"]),
            kind="experimental_fact",
            grade="B_located_prose",
            text="RAG improves factuality by 12 points.",
            text_hash="hash-evidence-1",
            page=4,
        )

    seed(clean_pg_database_url, _seed_evidence)

    units = client.get(f"/api/v1/projects/{project['id']}/evidence-units")
    assert units.status_code == 200, units.text
    assert [item["title"] for item in units.json()] == ["Retrieval Augmented Generation"]
    assert units.json()[0]["cite_key"].startswith("lovelace2023")

    matrix = client.get(f"/api/v1/projects/{project['id']}/evidence-matrix")
    assert matrix.status_code == 200, matrix.text
    body = matrix.json()
    assert [item["title"] for item in body["evidence"]] == ["Retrieval Augmented Generation"]
    assert body["diagnostics"]["evidence_unit_count"] == 1


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
        "priced_call_count": 0,
        # 一次调用都没有时金额是完整的 0；有未定价调用才降级成下界（P1-4）。
        "unpriced_call_count": 0,
        "cost_complete": True,
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

    # v1 的失败是历史审计记录；v2 成功后，导航汇总必须与工作台的版本折叠一致，
    # 不能继续显示一个永远消不掉的失败红点。
    revised = client.post(
        f"/api/v1/projects/{project['id']}/visuals/{failed['id']}/regenerate",
        json={},
    )
    assert revised.status_code == 201, revised.text

    async def _ready_revision(session):
        from db import get_visual

        row = await get_visual(session, uuid.UUID(revised.json()["id"]))
        row.generation_status = "ready"

    seed(clean_pg_database_url, _ready_revision)
    body = client.get(f"/api/v1/projects/{project['id']}/visuals/summary").json()
    assert body["ready"] == 2
    assert body["failed"] == 0


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


def test_visual_list_exposes_successful_generation_time_and_method(
    client: TestClient, clean_pg_database_url, tmp_path, monkeypatch
) -> None:
    """建议创建时间与图片生成时间必须分开，方式取实际 attempt 而非当前配置。"""
    monkeypatch.setenv("STORAGE_FS_ROOT", str(tmp_path))
    import paperforge_api.config as api_config

    api_config._settings = None
    project = _create_project(client, paper_type="original")
    upload = client.post(
        f"/api/v1/projects/{project['id']}/assets",
        files={"file": ("results.csv", b"method,score\nA,0.8\n", "text/csv")},
    ).json()
    visual = _create_chart_visual(client, project["id"], upload["asset_ref"])

    async def _record_success(session):
        from db import get_visual, record_visual_attempt

        row = await get_visual(session, uuid.UUID(visual["id"]))
        row.generation_status = "ready"
        row.provider = "visuald"
        await record_visual_attempt(
            session,
            visual_id=row.id,
            provider="visuald",
            model=None,
            output_width=1200,
            output_height=800,
        )

    seed(clean_pg_database_url, _record_success)
    rows = client.get(f"/api/v1/projects/{project['id']}/visuals").json()
    target = next(row for row in rows if row["id"] == visual["id"])
    assert target["generated_at"]
    assert target["created_at"]
    assert target["provider"] == "visuald"


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
        # Cloudflare FLUX 接受 prompt、steps 与 seed，但不接受尺寸：
        # 否则界面又会给出不会生效的比例下拉框。
        assert capabilities["supported_sizes"] == []
        assert capabilities["quality_modes"] == ["low", "medium", "high"]
        assert capabilities["supports_seed"] is True
        assert capabilities["supports_negative_prompt"] is False

        serialized = response.text
        assert "cf-secret-token" not in serialized
        assert "0123456789abcdef0123456789abcdef" not in serialized
    finally:
        api_config._settings = None


def test_visual_draft_fills_the_whole_spec_from_one_sentence(
    client: TestClient, monkeypatch
) -> None:
    """新建 AI 插图不该要求用户手写图注、替代文本和构图三段文本。

    用户的一句话必须先由 DeepSeek 结合全文扩展，不能直接变成 Yunwu 提示词。
    """
    _install_deepseek_image_stub(monkeypatch)
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
    assert body["spec"]["refined_prompt"] == body["spec"]["prompt"]
    assert "publication-ready" in body["spec"]["refined_prompt"]
    assert body["spec"]["semantics"]["elements"]
    assert body["spec"]["quality"] == "high"
    assert body["generator"] == "llm:deepseek-test"


def test_ai_image_draft_fails_closed_when_deepseek_is_unavailable(client: TestClient) -> None:
    project = _create_project(client)
    response = client.post(
        f"/api/v1/projects/{project['id']}/visuals/draft",
        json={"kind": "ai_image", "intent": "生成全文综述图"},
    )
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "deepseek_image_prompt_failed"


def test_yunwu_ai_draft_requires_deepseek_full_paper_analysis(
    client: TestClient, monkeypatch
) -> None:
    """“开始创作”先经 DeepSeek，Yunwu 只能拿到分析后的成品提示词。"""

    monkeypatch.setenv("AI_IMAGES_ENABLED", "true")
    monkeypatch.setenv("IMAGE_PROVIDER", "yunwu")
    monkeypatch.setenv("YUNWU_API_KEY", "yunwu-test-key")
    import paperforge_api.config as api_config

    calls: list[dict[str, Any]] = []
    _install_deepseek_image_stub(monkeypatch, captured=calls)
    api_config._settings = None
    try:
        project = _create_project(client, title="循证研究自动化")
        response = client.post(
            f"/api/v1/projects/{project['id']}/visuals/draft",
            json={"kind": "ai_image", "intent": "摘要图"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["kind"] == "ai_image"
        assert body["generator"] == "llm:deepseek-test"
        assert body["spec"]["refined_prompt"]
        assert body["spec"]["semantics"]["aspect_ratio"] == "3:2"
        assert calls[0]["role"] == "polisher"
        assert "SECTION-BALANCED PAPER CONTEXT" in calls[0]["user_prompt"]
        assert "循证研究自动化" in calls[0]["user_prompt"]
    finally:
        api_config._settings = None


def test_old_ai_draft_is_analyzed_against_full_paper_before_generation(
    client: TestClient, monkeypatch, clean_pg_database_url
) -> None:
    monkeypatch.setenv("AI_IMAGES_ENABLED", "true")
    monkeypatch.setenv("IMAGE_PROVIDER", "yunwu")
    monkeypatch.setenv("YUNWU_API_KEY", "yunwu-test-key")
    import paperforge_api.config as api_config

    api_config._settings = None
    calls: list[dict[str, Any]] = []
    _install_deepseek_image_stub(monkeypatch, captured=calls)
    try:
        project = _create_project(client, title="序列推荐攻击综述")

        async def _seed_full_paper(session):
            document = await create_document(
                session, project_id=uuid.UUID(project["id"]), outline_id=None
            )
            for order, key, title, text in [
                (0, "introduction", "引言", "序列推荐攻击的研究背景。"),
                (1, "conclusion", "结论", "全文结论末尾标记 FULL-PAPER-SENTINEL。"),
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
                        "blocks": [{"type": "paragraph", "runs": [{"t": "text", "v": text}]}],
                    },
                    cite_keys=[],
                )

        seed(clean_pg_database_url, _seed_full_paper)
        old = client.post(
            f"/api/v1/projects/{project['id']}/visuals",
            json={
                "title": "全文综述图",
                "caption": "全文综述图",
                "alt_text": "全文综述图",
                "spec": {
                    "kind": "ai_image",
                    "prompt": "A generic academic overview illustration",
                    "quality": "high",
                },
            },
        )
        assert old.status_code == 201, old.text

        blocked = client.post(
            f"/api/v1/projects/{project['id']}/visuals/{old.json()['id']}/generate"
        )
        assert blocked.status_code == 409
        assert blocked.json()["detail"]["code"] == "ai_prompt_requires_deepseek"

        prepared = client.post(
            f"/api/v1/projects/{project['id']}/visuals/{old.json()['id']}/prepare-generation"
        )
        assert prepared.status_code == 200, prepared.text
        body = prepared.json()
        assert body["spec"]["refined_prompt"] == body["resolved_prompt"]
        assert body["paper_snapshot_hash"]
        assert "FULL-PAPER-SENTINEL" in calls[0]["user_prompt"]

        generated = client.post(
            f"/api/v1/projects/{project['id']}/visuals/{old.json()['id']}/generate"
        )
        assert generated.status_code == 202, generated.text
    finally:
        api_config._settings = None


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


def test_visual_draft_survives_intents_that_cannot_go_into_a_prompt(
    client: TestClient, monkeypatch
) -> None:
    """意图里带 URL 会被 AIImageSpec 拒绝——不能把这个错误甩回界面。

    题材本身不再是拒绝理由：量化表述曾经会撞上一份关键字黑名单，现在只有
    注入类内容（URL、代码片段）进不了提示词。
    """
    _install_deepseek_image_stub(monkeypatch)
    project = _create_project(client)
    for intent in ("准确率 95% 的对比示意", "参照 https://example.com/figure.png 的构图"):
        response = client.post(
            f"/api/v1/projects/{project['id']}/visuals/draft",
            json={"kind": "ai_image", "intent": intent},
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


def test_visual_auto_respects_explicit_colorful_icon_style(client: TestClient, monkeypatch) -> None:
    """“技术流程”命中关系词，但彩色 icon 是明确的 AI 插图风格要求。

    这条分支只在 AI 插图开启时成立——`client` 夹具默认把它关掉（免得本机配置
    影响断言），所以这里必须自己打开并重置配置缓存。此前没开，用例断言的
    ai_image 永远拿不到，只能得到兜底的示意图。
    """
    monkeypatch.setenv("AI_IMAGES_ENABLED", "true")
    monkeypatch.setenv("YUNWU_API_KEY", "yunwu-test-key")
    import paperforge_api.config as api_config

    api_config._settings = None
    try:
        _install_deepseek_image_stub(monkeypatch)
        project = _create_project(client)
        response = client.post(
            f"/api/v1/projects/{project['id']}/visuals/draft",
            json={"kind": "auto", "intent": "生成彩色的带有 icon 图标示意的技术发展图"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["kind"] == "ai_image"
        assert body["spec"]["kind"] == "ai_image"
        assert "图标化视觉风格" in body["reason"]
    finally:
        api_config._settings = None


def test_visual_auto_prefers_configured_yunwu_over_graphviz_for_process_intent(
    client: TestClient, monkeypatch
) -> None:
    """自动模式默认用已配置的 Yunwu；Graphviz 只在显式选 diagram 时使用。"""
    monkeypatch.setenv("AI_IMAGES_ENABLED", "true")
    monkeypatch.setenv("IMAGE_PROVIDER", "yunwu")
    monkeypatch.setenv("YUNWU_API_KEY", "yunwu-test-key")
    import paperforge_api.config as api_config

    api_config._settings = None
    try:
        _install_deepseek_image_stub(monkeypatch)
        project = _create_project(client)
        automatic = client.post(
            f"/api/v1/projects/{project['id']}/visuals/draft",
            json={"kind": "auto", "intent": "展示检索、筛选和证据整合流程"},
        )
        assert automatic.status_code == 200, automatic.text
        assert automatic.json()["kind"] == "ai_image"
        assert "AI 生成" in automatic.json()["reason"]

        local = client.post(
            f"/api/v1/projects/{project['id']}/visuals/draft",
            json={"kind": "diagram", "intent": "展示检索、筛选和证据整合流程"},
        )
        assert local.status_code == 200, local.text
        assert local.json()["kind"] == "diagram"
    finally:
        api_config._settings = None


def test_visual_auto_falls_back_to_a_diagram_when_ai_images_are_off(
    client: TestClient,
) -> None:
    """AI 插图关闭时，「auto」不能选一种根本生成不了的类型。

    否则用户会拿到一张永远点不动的卡片——生成按钮禁用，却看不出是功能没开还是坏了。
    """
    project = _create_project(client)
    response = client.post(
        f"/api/v1/projects/{project['id']}/visuals/draft",
        json={"kind": "auto", "intent": "生成彩色的带有 icon 图标示意的技术发展图"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["kind"] == "diagram"


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


def test_ai_image_revision_is_replanned_by_deepseek_instead_of_appended_to_style(
    client: TestClient, monkeypatch, clean_pg_database_url
) -> None:
    calls: list[dict[str, Any]] = []
    _install_deepseek_image_stub(monkeypatch, captured=calls)
    project = _create_project(client, title="序列推荐攻击综述")

    async def _seed_section(session):
        document = await create_document(
            session, project_id=uuid.UUID(project["id"]), outline_id=None
        )
        await upsert_section(
            session,
            document_id=document.id,
            section_key="conclusion",
            title="结论",
            order_no=1,
            body_ir={
                "key": "conclusion",
                "level": 1,
                "title": "结论",
                "blocks": [
                    {
                        "type": "paragraph",
                        "runs": [{"t": "text", "v": "防御、评测基准与未来方向。"}],
                    }
                ],
            },
            cite_keys=[],
        )

    seed(clean_pg_database_url, _seed_section)
    original = client.post(
        f"/api/v1/projects/{project['id']}/visuals",
        json={
            "title": "全文综述图",
            "caption": "旧图注",
            "alt_text": "旧替代文本",
            "spec": {
                "kind": "ai_image",
                "prompt": "A generic academic overview illustration",
                "refined_prompt": "A generic academic overview illustration",
                "style": "clean academic conceptual illustration",
            },
        },
    )
    revised = client.post(
        f"/api/v1/projects/{project['id']}/visuals/{original.json()['id']}/regenerate",
        json={"revision_instruction": "太简陋了，分析全文并生成真正能放入论文中的综述图"},
    )

    assert revised.status_code == 201, revised.text
    body = revised.json()
    assert body["spec"]["style"] == "clean academic conceptual illustration"
    assert body["spec"]["refined_prompt"] == body["resolved_prompt"]
    assert "太简陋了" in calls[0]["user_prompt"]
    assert "防御、评测基准与未来方向" in calls[0]["user_prompt"]


# ---- 任务控制与项目删除 ----


def _start_full_job(client: TestClient, project: dict[str, Any]) -> dict[str, Any]:
    response = client.post(f"/api/v1/projects/{project['id']}/generate", json={})
    assert response.status_code == 202, response.text
    return response.json()


def test_started_job_records_how_to_replay_itself(client: TestClient) -> None:
    """建 job 时就要记下管线与参数——「继续」事后无从推断当初跑的是什么。"""
    project = _create_project(client)
    job = _start_full_job(client, project)
    assert job["checkpoint"]["resume"] == {
        "function": "run_full_pipeline",
        "kwargs": {"quality_profile": "draft", "review_style": "narrative"},
    }


def test_cancel_marks_a_queued_job_cancelled_immediately(client: TestClient) -> None:
    """还没被 worker 捡走的任务立刻落终态，用户不用盯着一个僵尸「排队中」。"""
    project = _create_project(client)
    job = _start_full_job(client, project)
    response = client.post(f"/api/v1/projects/{project['id']}/jobs/{job['id']}/cancel")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "cancelled"
    assert body["checkpoint"]["control"] == "cancel"


def test_cancel_rejects_an_already_finished_job(client: TestClient) -> None:
    project = _create_project(client)
    job = _start_full_job(client, project)
    client.post(f"/api/v1/projects/{project['id']}/jobs/{job['id']}/cancel")
    again = client.post(f"/api/v1/projects/{project['id']}/jobs/{job['id']}/cancel")
    assert again.status_code == 409


def test_pause_requires_a_running_job(client: TestClient) -> None:
    """排队中的任务只能取消：还没开始跑，没有断点可留。"""
    project = _create_project(client)
    job = _start_full_job(client, project)
    response = client.post(f"/api/v1/projects/{project['id']}/jobs/{job['id']}/pause")
    assert response.status_code == 409


def test_resume_creates_a_new_job_seeded_with_the_checkpoint(
    client: TestClient,
    clean_pg_database_url: str,
) -> None:
    """继续 = 新建 job + 播种断点；上一轮的停止开关不能被继承。"""
    project = _create_project(client)
    job = _start_full_job(client, project)

    async def _pause(session):
        row = await get_job(session, uuid.UUID(job["id"]))
        await update_job(
            session,
            row,
            status="paused",
            checkpoint={"control": "pause", "outline": {"section_count": 4}},
        )

    seed(clean_pg_database_url, _pause)

    response = client.post(f"/api/v1/projects/{project['id']}/jobs/{job['id']}/resume")
    assert response.status_code == 202, response.text
    resumed = response.json()
    assert resumed["id"] != job["id"]
    assert resumed["status"] == "queued"
    # 断点带过来了，停止开关没有——否则续跑的 job 一启动就把自己停了。
    assert resumed["checkpoint"]["outline"] == {"section_count": 4}
    assert "control" not in resumed["checkpoint"]
    assert resumed["checkpoint"]["resumed_from"] == job["id"]
    assert client.queue.calls[-1][0] == "run_full_pipeline"


def test_resume_rejects_a_job_that_is_not_paused(client: TestClient) -> None:
    project = _create_project(client)
    job = _start_full_job(client, project)
    response = client.post(f"/api/v1/projects/{project['id']}/jobs/{job['id']}/resume")
    assert response.status_code == 409


def test_deleting_a_project_cancels_its_running_jobs(
    client: TestClient,
    clean_pg_database_url: str,
) -> None:
    """删除意味着「这些都不要了」，不该再要求用户先手动取消一遍。"""
    project = _create_project(client)
    job = _start_full_job(client, project)

    async def _run(session):
        row = await get_job(session, uuid.UUID(job["id"]))
        await update_job(session, row, status="running")

    seed(clean_pg_database_url, _run)

    assert client.delete(f"/api/v1/projects/{project['id']}").status_code == 204

    async def _read(session):
        row = await get_job(session, uuid.UUID(job["id"]))
        return row.status, (row.checkpoint_json or {}).get("control")

    status_value, control = seed(clean_pg_database_url, _read)
    assert status_value == "cancelled"
    assert control == "cancel"


def test_deleted_project_disappears_from_every_route(client: TestClient) -> None:
    """软删除收口在授权依赖上：删掉之后整个项目的每个端点都该 404。"""
    project = _create_project(client)
    assert client.delete(f"/api/v1/projects/{project['id']}").status_code == 204

    assert client.get("/api/v1/projects").json() == []
    for path in ("", "/scope", "/library", "/jobs", "/cost"):
        response = client.get(f"/api/v1/projects/{project['id']}{path}")
        assert response.status_code == 404, f"{path} 仍然可访问：{response.status_code}"


def test_deleted_project_can_be_restored(client: TestClient) -> None:
    project = _create_project(client)
    client.delete(f"/api/v1/projects/{project['id']}")

    listed = client.get("/api/v1/projects?deleted=true").json()
    assert [item["id"] for item in listed] == [project["id"]]
    assert listed[0]["deleted_at"] is not None

    restored = client.post(f"/api/v1/projects/{project['id']}/restore")
    assert restored.status_code == 200, restored.text
    assert restored.json()["deleted_at"] is None
    assert [item["id"] for item in client.get("/api/v1/projects").json()] == [project["id"]]
    assert client.get("/api/v1/projects?deleted=true").json() == []
