"""任务本体的库内契约（真实 Postgres）。

审计修正 C-6：`list_project_task_specs` 在项目没有 ProjectTaskProfile 时返回**全部**
任务定义，而没有任何路由会创建 profile 行——于是每个项目（化学、临床、材料）都继承了
recsys 与 BGC 的指标和数据集白名单。这个文件锁住两种回退各自的行为。
"""

from __future__ import annotations

import uuid

import pytest
from db import create_project, create_user
from db.models.paper import ProjectTaskProfile, TaskDefinition
from db.repositories.tasks import (
    GENERIC_TASK_SLUG,
    compile_dataset_pattern,
    datasets_for_tasks,
    list_project_task_specs,
    metrics_for_tasks,
    upsert_task_definitions,
    vocabulary_for_tasks,
)
from sqlalchemy import select


async def _seed_ontology(session) -> None:
    await upsert_task_definitions(
        session,
        [
            {
                "slug": "recsys.poisoning_attack",
                "domain": "recsys_attack",
                "labels": {"en": "Poisoning attack"},
                "metrics": ["NDCG@K", "HR@K"],
                "datasets": ["MovieLens-1M", "Amazon Beauty"],
                "inclusion_cues": ["recommender"],
                "vocabulary": {"model_families": ["SASRec"]},
            },
            {
                "slug": GENERIC_TASK_SLUG,
                "domain": "generic",
                "labels": {"en": "General scholarly work"},
                "metrics": [],
                "datasets": [],
                "dimensions": ["dataset", "metric_name", "split"],
                "vocabulary": {},
            },
        ],
    )
    await session.flush()


async def _project(session) -> uuid.UUID:
    owner = await create_user(
        session,
        email=f"{uuid.uuid4()}@example.test",
        password_hash="!test-only",
        verified=True,
    )
    project = await create_project(
        session,
        title="Reaction yield prediction review",
        paper_type="review",
        language="en",
        topic="chemistry",
        owner_id=owner.id,
    )
    return project.id


@pytest.mark.asyncio
async def test_all_tasks_fallback_leaks_every_domain(session) -> None:
    """当前默认行为，明确记录下来——这正是 generic_only 要修的东西。"""
    await _seed_ontology(session)
    project_id = await _project(session)

    specs = await list_project_task_specs(session, project_id, fallback="all_tasks")

    slugs = {spec.slug for spec in specs}
    assert "recsys.poisoning_attack" in slugs, "化学项目继承了推荐领域的任务"
    assert "MovieLens-1M" in datasets_for_tasks(specs)
    assert compile_dataset_pattern(datasets_for_tasks(specs)) is not None


@pytest.mark.asyncio
async def test_generic_only_fallback_stops_the_leak(session) -> None:
    await _seed_ontology(session)
    project_id = await _project(session)

    specs = await list_project_task_specs(session, project_id, fallback="generic_only")

    assert [spec.slug for spec in specs] == [GENERIC_TASK_SLUG]
    assert datasets_for_tasks(specs) == []
    # 没有数据集白名单 ⇒ 没有数据集正则，正文里的随机大写词不会被当成数据集。
    assert compile_dataset_pattern(datasets_for_tasks(specs)) is None
    assert metrics_for_tasks(specs) == []
    assert vocabulary_for_tasks(specs).empty is True


@pytest.mark.asyncio
async def test_an_explicit_profile_wins_over_either_fallback(session) -> None:
    await _seed_ontology(session)
    project_id = await _project(session)
    session.add(
        ProjectTaskProfile(
            project_id=project_id,
            task_id="recsys.poisoning_attack",
            is_core=True,
            order_index=0,
        )
    )
    await session.flush()

    for fallback in ("all_tasks", "generic_only"):
        specs = await list_project_task_specs(session, project_id, fallback=fallback)
        assert [spec.slug for spec in specs] == ["recsys.poisoning_attack"]


@pytest.mark.asyncio
async def test_generic_only_degrades_when_the_row_is_absent(session) -> None:
    """迁移尚未跑到的库里没有 generic.scholarly；那时退回旧行为好过返回空列表。"""
    await upsert_task_definitions(
        session,
        [
            {
                "slug": "recsys.poisoning_attack",
                "domain": "recsys_attack",
                "labels": {"en": "Poisoning attack"},
            }
        ],
    )
    await session.flush()
    project_id = await _project(session)

    specs = await list_project_task_specs(session, project_id, fallback="generic_only")

    assert [spec.slug for spec in specs] == ["recsys.poisoning_attack"]


@pytest.mark.asyncio
async def test_upsert_is_idempotent_and_never_deletes(session) -> None:
    await _seed_ontology(session)
    before = set(await session.scalars(select(TaskDefinition.slug)))

    # 只提交其中一条：另一条必须原样保留，本体是共享的。
    result = await upsert_task_definitions(
        session,
        [
            {
                "slug": "recsys.poisoning_attack",
                "domain": "recsys_attack",
                "labels": {"en": "Poisoning attack (edited)"},
                "metrics": ["NDCG@K"],
            }
        ],
    )
    await session.flush()

    after = set(await session.scalars(select(TaskDefinition.slug)))
    assert result == {"created": 0, "updated": 1}
    assert after == before


@pytest.mark.asyncio
async def test_vocabulary_round_trips_through_the_database(session) -> None:
    await upsert_task_definitions(
        session,
        [
            {
                "slug": "x.one",
                "domain": "d",
                "labels": {"en": "X"},
                "vocabulary": {
                    "model_families": ["CNN"],
                    "split_strategies": [{"cue": "temporal split", "name": "temporal"}],
                    "task_variants": ["binary"],
                },
            }
        ],
    )
    await session.flush()

    specs = await list_project_task_specs(session, await _project(session))
    spec = next(item for item in specs if item.slug == "x.one")
    assert spec.vocabulary.model_families == ("CNN",)
    assert spec.vocabulary.split_strategies == (("temporal split", "temporal"),)
    # 裸字符串条目的规范名就是它自己。
    assert spec.vocabulary.task_variants == (("binary", "binary"),)
