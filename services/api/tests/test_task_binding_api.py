"""显式任务绑定的 API 契约（Phase 3.5）。

`ProjectTaskProfile` 与 `replace_project_task_profile` 早就存在，但只有 QDECOMP 会写，
而它只在本体线索命中时才推断得出任务——生产上 29 个项目里只有 1 个被绑定。其余的
静默继承**全部**领域的指标与数据集白名单。这组用例锁住新的显式绑定通道，以及
「任务集是从哪来的」这个此前完全不可见的信息。
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from db import create_user, upsert_task_definitions
from db.session import make_engine, make_session_factory
from fastapi.testclient import TestClient
from paperforge_api.auth_service import hash_password  # noqa: F401  (parity with sibling tests)
from paperforge_api.deps import get_queue
from paperforge_api.main import create_app

ONTOLOGY = [
    {
        "slug": "recsys.poisoning_attack",
        "domain": "recsys_attack",
        "labels": {"en": "Poisoning attack", "zh": "投毒攻击"},
        "metrics": ["NDCG@K", "HR@K"],
        "datasets": ["MovieLens-1M"],
        "vocabulary": {"model_families": ["SASRec"]},
    },
    {
        "slug": "bgc.identification",
        "domain": "bgc",
        "labels": {"en": "BGC identification", "zh": "基因簇识别"},
        "metrics": ["AUROC"],
        "datasets": ["MIBiG"],
        "vocabulary": {"pretrained_backbones": ["ESM2"]},
    },
    {
        "slug": "generic.scholarly",
        "domain": "generic",
        "labels": {"en": "General scholarly work", "zh": "通用学术研究"},
        "metrics": [],
        "datasets": [],
        "dimensions": ["dataset", "metric_name", "split"],
        "vocabulary": {},
    },
]


class _FakeQueue:
    async def enqueue_job(self, *args: Any, **kwargs: Any) -> Any:
        return None


def _seed(database_url: str, coro_factory):
    import asyncio

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

    return asyncio.run(_run())


@pytest.fixture
def client(clean_pg_database_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", clean_pg_database_url)
    monkeypatch.setenv("LLM_DEFAULT_PROVIDER", "noop")
    monkeypatch.setenv("AUTH_RATE_LIMIT_ENABLED", "false")
    import paperforge_api.config as api_config
    import paperforge_api.deps as api_deps

    api_config._settings = None
    api_deps._engine = None
    api_deps._session_factory = None

    app = create_app()
    app.dependency_overrides[get_queue] = lambda: _FakeQueue()
    with TestClient(app, headers={"Origin": "http://localhost:3000"}) as test_client:
        raw_session = "task-binding-test-session"

        async def _bootstrap(session):
            from db import create_user_session

            user = await create_user(
                session,
                email="tasks@example.test",
                password_hash="!test-only",
                display_name="Task owner",
                verified=True,
            )
            await create_user_session(
                session,
                user_id=user.id,
                token_hash=hashlib.sha256(raw_session.encode()).hexdigest(),
                expires_at=datetime.now(UTC) + timedelta(days=30),
            )
            # clean_pg_database_url 会 TRUNCATE 掉迁移种下的 task_definition。
            await upsert_task_definitions(session, ONTOLOGY)
            return user.id

        _seed(clean_pg_database_url, _bootstrap)
        test_client.cookies.set("paperforge_session", raw_session)
        yield test_client
    app.dependency_overrides.clear()
    api_config._settings = None
    api_deps._engine = None
    api_deps._session_factory = None


def _project(client: TestClient) -> str:
    response = client.post(
        "/api/v1/projects",
        json={
            "title": "Reaction yield prediction",
            "paper_type": "review",
            "writing_mode": "auto",
            "language": "en",
            "topic": "chemistry",
            "citation_style": "author_year",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_task_catalogue_is_listed_with_binding_hints(client: TestClient) -> None:
    response = client.get("/api/v1/tasks")
    assert response.status_code == 200
    body = response.json()
    assert {item["slug"] for item in body} == {item["slug"] for item in ONTOLOGY}
    recsys = next(item for item in body if item["slug"] == "recsys.poisoning_attack")
    assert recsys["label"] == "投毒攻击"
    assert recsys["metric_count"] == 2
    assert recsys["has_vocabulary"] is True
    generic = next(item for item in body if item["slug"] == "generic.scholarly")
    assert generic["has_vocabulary"] is False


def test_catalogue_can_be_filtered_by_domain(client: TestClient) -> None:
    body = client.get("/api/v1/tasks", params={"domain": "bgc"}).json()
    assert [item["slug"] for item in body] == ["bgc.identification"]


def test_an_unbound_project_reports_the_fallback_and_says_why(client: TestClient) -> None:
    project_id = _project(client)
    body = client.get(f"/api/v1/projects/{project_id}/tasks").json()

    assert body["bound"] is False
    assert body["source"] == "fallback"
    assert body["task_ids"] == []
    assert body["fallback_mode"] == "all_tasks"
    # 这正是此前不可见的事实：化学项目继承了每一个领域的白名单。
    assert {item["slug"] for item in body["effective_tasks"]} == {item["slug"] for item in ONTOLOGY}
    assert body["fallback_note"] and "未绑定" in body["fallback_note"]


def test_explicit_binding_narrows_the_effective_task_set(client: TestClient) -> None:
    project_id = _project(client)
    response = client.put(
        f"/api/v1/projects/{project_id}/tasks",
        json={"task_ids": ["generic.scholarly"]},
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["bound"] is True
    assert body["source"] == "explicit"
    assert body["task_ids"] == ["generic.scholarly"]
    assert [item["slug"] for item in body["effective_tasks"]] == ["generic.scholarly"]
    assert body["fallback_note"] is None

    # 重新读取必须一致——绑定是持久的，不是响应里的一次性投影。
    reread = client.get(f"/api/v1/projects/{project_id}/tasks").json()
    assert reread["task_ids"] == ["generic.scholarly"]
    assert reread["source"] == "explicit"


def test_binding_preserves_requested_order_and_deduplicates(client: TestClient) -> None:
    project_id = _project(client)
    body = client.put(
        f"/api/v1/projects/{project_id}/tasks",
        json={"task_ids": ["bgc.identification", "recsys.poisoning_attack", "bgc.identification"]},
    ).json()
    assert body["task_ids"] == ["bgc.identification", "recsys.poisoning_attack"]


def test_empty_list_unbinds_and_returns_to_the_fallback(client: TestClient) -> None:
    project_id = _project(client)
    client.put(f"/api/v1/projects/{project_id}/tasks", json={"task_ids": ["bgc.identification"]})

    body = client.put(f"/api/v1/projects/{project_id}/tasks", json={"task_ids": []}).json()

    assert body["bound"] is False
    assert body["source"] == "fallback"
    assert len(body["effective_tasks"]) == len(ONTOLOGY)


def test_unknown_task_ids_are_rejected_without_partial_writes(client: TestClient) -> None:
    project_id = _project(client)
    client.put(f"/api/v1/projects/{project_id}/tasks", json={"task_ids": ["bgc.identification"]})

    response = client.put(
        f"/api/v1/projects/{project_id}/tasks",
        json={"task_ids": ["bgc.identification", "does.not.exist"]},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "unknown_task_ids"
    assert response.json()["detail"]["task_ids"] == ["does.not.exist"]
    # 先前的绑定必须原样保留：一次被拒的请求不能改变任何状态。
    assert client.get(f"/api/v1/projects/{project_id}/tasks").json()["task_ids"] == [
        "bgc.identification"
    ]


def test_binding_is_scoped_to_the_owning_project(client: TestClient) -> None:
    first = _project(client)
    second = _project(client)
    client.put(f"/api/v1/projects/{first}/tasks", json={"task_ids": ["bgc.identification"]})

    assert client.get(f"/api/v1/projects/{second}/tasks").json()["bound"] is False


def test_unknown_project_returns_404(client: TestClient) -> None:
    missing = "00000000-0000-4000-8000-000000000999"
    assert client.get(f"/api/v1/projects/{missing}/tasks").status_code == 404
    assert (
        client.put(f"/api/v1/projects/{missing}/tasks", json={"task_ids": []}).status_code == 404
    )
