"""任务绑定建议器（真实 Postgres）。

覆盖率闭合靠的是这个建议器，而不是手工改 29 行。它与 QDECOMP 的自动推断有两点
关键差异：判据包含题名与主题（29 个项目里 23 个从未跑过 QDECOMP，一条研究问题都
没有），以及匹配到领域就绑定该领域的**全部**任务（综述会横跨一个领域的多个任务，
指标白名单必须取并集）。

最重要的一条是排除线索：`infer_task_id` 只看纳入线索，于是"根际微生物次级代谢产物
与植物抗病机制"会命中 bgc 的"次级代谢"。那是一篇生物学机制论文，绑到 bgc
（指标 AUROC/MCC、数据集 MIBiG）就是自信地绑错。
"""

from __future__ import annotations

import uuid

import pytest
from db import (
    create_project,
    create_user,
    list_project_task_bindings,
    upsert_task_definitions,
)
from paperforge_api.admin import _task_suggest

ONTOLOGY = [
    {
        "slug": "recsys.poisoning_attack",
        "domain": "recsys_attack",
        "labels": {"en": "Poisoning attack"},
        "inclusion_cues": ["序列推荐", "投毒攻击"],
    },
    {
        "slug": "recsys.robust_training",
        "domain": "recsys_attack",
        "labels": {"en": "Robust training"},
        "inclusion_cues": ["鲁棒推荐"],
    },
    {
        "slug": "bgc.identification",
        "domain": "bgc",
        "labels": {"en": "BGC identification"},
        "inclusion_cues": ["生物合成", "次级代谢", "天然产物"],
        "exclusion_cues": ["植物抗病", "根际"],
    },
    {
        "slug": "generic.scholarly",
        "domain": "generic",
        "labels": {"en": "General scholarly work"},
    },
]


async def _seed(session) -> None:
    await upsert_task_definitions(session, ONTOLOGY)
    await session.flush()


async def _project(session, *, title: str, topic: str | None = None) -> uuid.UUID:
    owner = await create_user(
        session,
        email=f"{uuid.uuid4()}@example.test",
        password_hash="!test-only",
        verified=True,
    )
    project = await create_project(
        session,
        title=title,
        paper_type="review",
        language="zh",
        topic=topic or title,
        owner_id=owner.id,
    )
    await session.flush()
    return project.id


async def _bindings(session, project_id: uuid.UUID) -> list[str]:
    return [row.task_id for row in await list_project_task_bindings(session, project_id)]


@pytest.mark.asyncio
async def test_a_domain_match_binds_the_whole_domain(session, capsys) -> None:
    """综述横跨一个领域的多个任务；只绑最佳单任务会漏掉指标。"""
    await _seed(session)
    project_id = await _project(session, title="序列推荐攻击综述")

    await _task_suggest(session, apply=True, include_bound=False)

    assert await _bindings(session, project_id) == [
        "recsys.poisoning_attack",
        "recsys.robust_training",
    ]


@pytest.mark.asyncio
async def test_no_match_proposes_the_generic_task(session, capsys) -> None:
    """通用任务是一个明确判断（"没有适用的专用本体"），不是"未绑定"。"""
    await _seed(session)
    project_id = await _project(session, title="光催化产过氧化氢")

    await _task_suggest(session, apply=True, include_bound=False)

    assert await _bindings(session, project_id) == ["generic.scholarly"]


@pytest.mark.asyncio
async def test_an_exclusion_cue_overrides_an_inclusion_match(session, capsys) -> None:
    """核心用例：命中"次级代谢"，但被"植物抗病"排除，落到 generic。"""
    await _seed(session)
    project_id = await _project(session, title="根际微生物次级代谢产物与植物抗病机制研究进展")

    await _task_suggest(session, apply=True, include_bound=False)

    assert await _bindings(session, project_id) == ["generic.scholarly"]
    assert "excluded from bgc" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_a_computational_paper_in_the_same_field_still_binds(session, capsys) -> None:
    """排除线索必须足够窄：真正的计算 BGC 论文仍要绑到 bgc。"""
    await _seed(session)
    project_id = await _project(session, title="蛋白质语言模型在天然产物生物合成研究中的应用")

    await _task_suggest(session, apply=True, include_bound=False)

    assert await _bindings(session, project_id) == ["bgc.identification"]


@pytest.mark.asyncio
async def test_dry_run_writes_nothing(session, capsys) -> None:
    await _seed(session)
    project_id = await _project(session, title="序列推荐投毒攻击")

    await _task_suggest(session, apply=False, include_bound=False)

    assert await _bindings(session, project_id) == []
    assert "dry run" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_applying_twice_is_idempotent(session, capsys) -> None:
    await _seed(session)
    project_id = await _project(session, title="序列推荐投毒攻击")

    await _task_suggest(session, apply=True, include_bound=False)
    first = await _bindings(session, project_id)
    await _task_suggest(session, apply=True, include_bound=True)

    assert await _bindings(session, project_id) == first
    assert "0 would change" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_already_bound_projects_are_skipped_by_default(session, capsys) -> None:
    """默认只补未绑定的；已有绑定（可能是用户定的）不去动它。"""
    await _seed(session)
    project_id = await _project(session, title="序列推荐投毒攻击")
    from db import replace_project_task_profile

    await replace_project_task_profile(
        session,
        project_id=project_id,
        task_ids=["generic.scholarly"],
    )
    await session.flush()

    await _task_suggest(session, apply=True, include_bound=False)

    assert await _bindings(session, project_id) == ["generic.scholarly"]


@pytest.mark.asyncio
async def test_title_alone_is_enough_when_there_are_no_questions(session, capsys) -> None:
    """23/29 的项目从未跑过 QDECOMP；只靠子问题的推断对它们完全无效。"""
    await _seed(session)
    project_id = await _project(session, title="写一篇近3年序列推荐攻击的文献综述", topic="")

    await _task_suggest(session, apply=True, include_bound=False)

    assert await _bindings(session, project_id) != []


@pytest.mark.asyncio
async def test_a_missing_generic_task_fails_loudly(session) -> None:
    """没有 generic.scholarly 就没有兜底；此时静默把项目留空是最糟的结果。"""
    await upsert_task_definitions(
        session,
        [{"slug": "x.one", "domain": "d", "labels": {"en": "X"}}],
    )
    await session.flush()
    await _project(session, title="anything")

    with pytest.raises(SystemExit):
        await _task_suggest(session, apply=False, include_bound=False)
