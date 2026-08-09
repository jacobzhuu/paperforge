"""QDECOMP 的任务绑定不得破坏已有绑定（Phase 3.5，真实 Postgres）。

改动前 `persist_question_decomposition` 无条件调用 `replace_project_task_profile`，
而后者先删后插。两个后果：

* 用户显式绑定过任务，下一次全流程会把它悄悄换掉；
* 更常见的是本轮一个任务都没推断出来（生产上 52 条子问题只有 1 条命中本体线索），
  于是删完不插，把上一轮推断出来的绑定也一起抹掉。

这组用例把「不破坏」钉死。
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from db import (
    create_project,
    create_user,
    list_project_task_bindings,
    replace_project_task_profile,
    update_project_scope,
    upsert_task_definitions,
)
from db.repositories.projects import get_project
from paperforge_worker.config import WorkerSettings
from paperforge_worker.context import JobContext
from paperforge_worker.pipelines.qdecomp import (
    TASK_PROFILE_USER_BOUND_KEY,
    persist_question_decomposition,
)
from scholar_gateway import InMemoryHttpCache

ONTOLOGY = [
    {
        "slug": "recsys.poisoning_attack",
        "domain": "recsys_attack",
        "labels": {"en": "Poisoning attack"},
        "inclusion_cues": ["poisoning attack", "recommender"],
    },
    {
        "slug": "bgc.identification",
        "domain": "bgc",
        "labels": {"en": "BGC identification"},
        "inclusion_cues": ["biosynthetic gene cluster"],
    },
    {
        "slug": "generic.scholarly",
        "domain": "generic",
        "labels": {"en": "General scholarly work"},
    },
]

# 命中 recsys 线索的 scope：QDECOMP 会推断出 recsys.poisoning_attack。
RECSYS_SCOPE = {
    "topic": "poisoning attacks on recommenders",
    "research_question": "How effective are poisoning attacks on recommender systems?",
    "sub_questions": [
        {"text": "How does a poisoning attack degrade recommender accuracy?"},
    ],
}

# 不命中任何本体线索：QDECOMP 推断不出任务（生产上的常态）。
CHEMISTRY_SCOPE = {
    "topic": "reaction yield prediction",
    "research_question": "Which descriptors predict reaction yield best?",
    "sub_questions": [
        {"text": "Which molecular descriptors correlate with measured yield?"},
    ],
}


async def _setup(session_factory) -> uuid.UUID:
    async with session_factory() as session:
        await upsert_task_definitions(session, ONTOLOGY)
        owner = await create_user(
            session,
            email=f"{uuid.uuid4()}@example.test",
            password_hash="!test-only",
            verified=True,
        )
        project = await create_project(
            session,
            title="Binding preservation",
            paper_type="review",
            language="en",
            topic="t",
            owner_id=owner.id,
        )
        await session.commit()
        return project.id


def _context(project_id: uuid.UUID, session_factory) -> JobContext:
    return JobContext(
        project_id=project_id,
        job_id=None,
        settings=WorkerSettings(llm_default_provider="noop"),
        session_factory=session_factory,
        http_client=httpx.Client(),
        scholar_cache=InMemoryHttpCache(),
    )


async def _bindings(session_factory, project_id: uuid.UUID) -> list[str]:
    async with session_factory() as session:
        return [row.task_id for row in await list_project_task_bindings(session, project_id)]


async def _mark_user_bound(session_factory, project_id: uuid.UUID) -> None:
    async with session_factory() as session:
        project = await get_project(session, project_id)
        assert project is not None
        scope = dict(project.scope_json or {})
        scope[TASK_PROFILE_USER_BOUND_KEY] = True
        await update_project_scope(session, project, scope)
        await session.commit()


@pytest.mark.asyncio
async def test_inference_binds_when_the_project_is_unbound(session_factory) -> None:
    project_id = await _setup(session_factory)

    await persist_question_decomposition(
        _context(project_id, session_factory),
        scope=RECSYS_SCOPE,
        topic="poisoning attacks",
        language="en",
    )

    assert await _bindings(session_factory, project_id) == ["recsys.poisoning_attack"]


@pytest.mark.asyncio
async def test_inferring_nothing_no_longer_wipes_an_existing_binding(session_factory) -> None:
    """回归：这是生产上最常见的路径，它此前会把绑定删干净。"""
    project_id = await _setup(session_factory)
    async with session_factory() as session:
        await replace_project_task_profile(
            session,
            project_id=project_id,
            task_ids=["bgc.identification"],
        )
        await session.commit()

    await persist_question_decomposition(
        _context(project_id, session_factory),
        scope=CHEMISTRY_SCOPE,
        topic="reaction yield",
        language="en",
    )

    assert await _bindings(session_factory, project_id) == ["bgc.identification"]


@pytest.mark.asyncio
async def test_a_user_binding_survives_inference_that_disagrees(session_factory) -> None:
    project_id = await _setup(session_factory)
    async with session_factory() as session:
        await replace_project_task_profile(
            session,
            project_id=project_id,
            task_ids=["generic.scholarly"],
        )
        await session.commit()
    await _mark_user_bound(session_factory, project_id)

    # scope 明确命中 recsys 线索，推断结果与用户的选择相左。
    await persist_question_decomposition(
        _context(project_id, session_factory),
        scope=RECSYS_SCOPE,
        topic="poisoning attacks",
        language="en",
    )

    assert await _bindings(session_factory, project_id) == ["generic.scholarly"]


@pytest.mark.asyncio
async def test_an_unmarked_inferred_binding_can_still_be_updated(session_factory) -> None:
    """没有用户标记时，推断出的绑定仍然可以被后续推断修正。"""
    project_id = await _setup(session_factory)
    async with session_factory() as session:
        await replace_project_task_profile(
            session,
            project_id=project_id,
            task_ids=["bgc.identification"],
        )
        await session.commit()

    await persist_question_decomposition(
        _context(project_id, session_factory),
        scope=RECSYS_SCOPE,
        topic="poisoning attacks",
        language="en",
    )

    assert await _bindings(session_factory, project_id) == ["recsys.poisoning_attack"]


@pytest.mark.asyncio
async def test_repeated_runs_are_idempotent(session_factory) -> None:
    project_id = await _setup(session_factory)
    for _ in range(3):
        await persist_question_decomposition(
            _context(project_id, session_factory),
            scope=RECSYS_SCOPE,
            topic="poisoning attacks",
            language="en",
        )
    assert await _bindings(session_factory, project_id) == ["recsys.poisoning_attack"]


@pytest.mark.asyncio
async def test_an_unbound_project_that_infers_nothing_stays_unbound(session_factory) -> None:
    """化学项目：推断不出任务，也不该凭空绑定到什么。"""
    project_id = await _setup(session_factory)

    await persist_question_decomposition(
        _context(project_id, session_factory),
        scope=CHEMISTRY_SCOPE,
        topic="reaction yield",
        language="en",
    )

    assert await _bindings(session_factory, project_id) == []
