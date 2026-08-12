"""给历史证据补维度（真实 Postgres）。

生产上的证据是在 ``EXPERIMENT_EXTRACTION_MODE=off`` 时抽的：359 条 measurement 里
task 与 split 全为空，``comparability_key`` 于是退化成按证据单元加盐的唯一值——
306 个不同的键对应 359 行，没有任何两条结果落进同一个可比簇。SYNTH 的冲突判定和
Phase 4 的规则 2 因此从未被真实数据检验过。

回填必须做到两件事，这组用例把它们钉死：**已存在的证据单元一个字都不改**（只补
measurement），以及**两篇论文报告同一套设定时真的合并成一个可比簇**。
"""

from __future__ import annotations

import uuid

import pytest
from db import list_evidence_measurements, upsert_evidence_unit
from db.models.library import EvidenceUnit
from paperforge_worker.pipelines.evidence import (
    apply_work_dimensions,
    extract_work_dimensions,
)
from sqlalchemy import select
from tests.test_experiment_extraction_persistence import (  # noqa: F401 - 复用真实种子
    SPAN,
    _context,
    _patch_extractor,
    _seed_project,
    _seed_work,
    _stub_extraction,
)


async def _seed_unit(session_factory, project_id, work_id, *, text: str = SPAN):
    """一条**已经落库**的证据单元——回填的输入就是这些历史行。"""
    async with session_factory() as session:
        unit, _ = await upsert_evidence_unit(
            session,
            work_id=work_id,
            project_id=project_id,
            kind="experimental_fact",
            grade="A_located_structured",
            text=text,
            text_hash=uuid.uuid4().hex,
            page=7,
            section_path="Results",
            object_ref="table:3",
        )
        await session.commit()
        return unit.id


@pytest.mark.asyncio
async def test_backfill_writes_dimensions_onto_existing_units(
    session_factory, monkeypatch
) -> None:
    project_id = await _seed_project(session_factory)
    work_id = await _seed_work(session_factory, project_id, title="A", doi="10.1/a")
    unit_id = await _seed_unit(session_factory, project_id, work_id)
    _patch_extractor(monkeypatch, _stub_extraction())
    context = _context(project_id, session_factory, "off")

    extracted = await extract_work_dimensions(context, work_id=work_id)
    written = await apply_work_dimensions(context, extracted)

    assert written == 1
    async with session_factory() as session:
        measurements = await list_evidence_measurements(session, [unit_id])
    row = measurements[unit_id][0]
    assert (row.task, row.dataset, row.split) == ("sequence classification", "CORE-BENCH", "test")
    assert row.extraction_source == "llm"
    assert row.locator_verified is True


@pytest.mark.asyncio
async def test_two_works_with_the_same_setup_finally_share_a_key(
    session_factory, monkeypatch
) -> None:
    """回填的全部意义：同一套设定必须合并成一个可比簇，否则冲突永远无法成立。"""
    project_id = await _seed_project(session_factory)
    units = []
    for index, (title, doi) in enumerate((("A", "10.1/a"), ("B", "10.1/b"))):
        work_id = await _seed_work(session_factory, project_id, title=title, doi=doi)
        units.append(await _seed_unit(session_factory, project_id, work_id))
        _patch_extractor(monkeypatch, _stub_extraction())
        context = _context(project_id, session_factory, "off")
        extracted = await extract_work_dimensions(context, work_id=work_id)
        assert await apply_work_dimensions(context, extracted) == 1
        assert index >= 0

    async with session_factory() as session:
        measurements = await list_evidence_measurements(session, units)
    keys = {measurements[unit_id][0].comparability_key for unit_id in units}
    assert len(keys) == 1


@pytest.mark.asyncio
async def test_different_datasets_still_stay_incomparable(session_factory, monkeypatch) -> None:
    project_id = await _seed_project(session_factory)
    units = []
    for title, doi, dataset in (("A", "10.1/a", "CORE-BENCH"), ("B", "10.1/b", "OTHER-BENCH")):
        work_id = await _seed_work(session_factory, project_id, title=title, doi=doi)
        units.append(await _seed_unit(session_factory, project_id, work_id))
        _patch_extractor(monkeypatch, _stub_extraction(dataset=dataset))
        context = _context(project_id, session_factory, "off")
        extracted = await extract_work_dimensions(context, work_id=work_id)
        await apply_work_dimensions(context, extracted)

    async with session_factory() as session:
        measurements = await list_evidence_measurements(session, units)
    keys = {measurements[unit_id][0].comparability_key for unit_id in units}
    assert len(keys) == 2


@pytest.mark.asyncio
async def test_the_evidence_units_themselves_are_untouched(session_factory, monkeypatch) -> None:
    """回填只补 measurement。改动证据文本或分级会让引用与分级门禁一起失真。"""
    project_id = await _seed_project(session_factory)
    work_id = await _seed_work(session_factory, project_id, title="A", doi="10.1/a")
    unit_id = await _seed_unit(session_factory, project_id, work_id)
    async with session_factory() as session:
        before = (await session.get(EvidenceUnit, unit_id)).__dict__.copy()
    _patch_extractor(monkeypatch, _stub_extraction())
    context = _context(project_id, session_factory, "off")

    await apply_work_dimensions(
        context, await extract_work_dimensions(context, work_id=work_id)
    )

    async with session_factory() as session:
        after = await session.get(EvidenceUnit, unit_id)
    for field in ("text", "grade", "kind", "page", "section_path", "object_ref", "text_hash"):
        assert getattr(after, field) == before[field]


@pytest.mark.asyncio
async def test_an_unverified_cell_is_never_written(session_factory, monkeypatch) -> None:
    """未定位核验的格子进不了跨研究比较——写进来只会污染 comparability_key。"""
    from dataclasses import replace

    project_id = await _seed_project(session_factory)
    work_id = await _seed_work(session_factory, project_id, title="A", doi="10.1/a")
    unit_id = await _seed_unit(session_factory, project_id, work_id)
    extraction = _stub_extraction()
    unverified = replace(extraction, cells=(replace(extraction.cells[0], locator_verified=False),))
    _patch_extractor(monkeypatch, unverified)
    context = _context(project_id, session_factory, "off")

    written = await apply_work_dimensions(
        context, await extract_work_dimensions(context, work_id=work_id)
    )

    assert written == 0
    async with session_factory() as session:
        assert await list_evidence_measurements(session, [unit_id]) == {}


@pytest.mark.asyncio
async def test_a_work_without_fulltext_is_skipped_not_failed(
    session_factory, monkeypatch
) -> None:
    project_id = await _seed_project(session_factory)
    _patch_extractor(monkeypatch, _stub_extraction())
    context = _context(project_id, session_factory, "off")

    extracted = await extract_work_dimensions(context, work_id=uuid.uuid4())

    assert extracted.skipped == "no_fulltext"
    assert extracted.cells == 0


@pytest.mark.asyncio
async def test_a_work_with_fulltext_but_no_evidence_is_skipped(
    session_factory, monkeypatch
) -> None:
    """没有证据单元就没有绑定目标；补出来的维度无处可挂。"""
    project_id = await _seed_project(session_factory)
    work_id = await _seed_work(session_factory, project_id, title="A", doi="10.1/a")
    _patch_extractor(monkeypatch, _stub_extraction())
    context = _context(project_id, session_factory, "off")

    extracted = await extract_work_dimensions(context, work_id=work_id)

    assert extracted.skipped == "no_evidence_units"


@pytest.mark.asyncio
async def test_a_rerun_replaces_the_previous_dimensions(session_factory, monkeypatch) -> None:
    """重跑要覆盖，不能叠加。

    模型两次给出的单元格不必逐字节相同，upsert 的匹配键对不上就会留下旧行——生产
    上因此出现过同一篇论文带两个 task 值，把本该合并的可比键又拆开。
    """
    project_id = await _seed_project(session_factory)
    work_id = await _seed_work(session_factory, project_id, title="A", doi="10.1/a")
    unit_id = await _seed_unit(session_factory, project_id, work_id)
    context = _context(project_id, session_factory, "off")

    _patch_extractor(monkeypatch, _stub_extraction(dataset="OLD-BENCH"))
    await apply_work_dimensions(
        context, await extract_work_dimensions(context, work_id=work_id)
    )
    _patch_extractor(monkeypatch, _stub_extraction(dataset="NEW-BENCH"))
    await apply_work_dimensions(
        context, await extract_work_dimensions(context, work_id=work_id)
    )

    async with session_factory() as session:
        rows = (await list_evidence_measurements(session, [unit_id]))[unit_id]
    assert [row.dataset for row in rows] == ["NEW-BENCH"]


@pytest.mark.asyncio
async def test_a_rerun_leaves_regex_measurements_alone(session_factory, monkeypatch) -> None:
    """只清自己写过的行；正则通道的结果不属于回填的管辖范围。"""
    from db import upsert_evidence_measurement

    project_id = await _seed_project(session_factory)
    work_id = await _seed_work(session_factory, project_id, title="A", doi="10.1/a")
    unit_id = await _seed_unit(session_factory, project_id, work_id)
    async with session_factory() as session:
        await upsert_evidence_measurement(
            session, evidence_unit_id=unit_id, metric_name="F1", value=0.5,
            extraction_source="regex",
        )
        await session.commit()
    _patch_extractor(monkeypatch, _stub_extraction())
    context = _context(project_id, session_factory, "off")

    await apply_work_dimensions(
        context, await extract_work_dimensions(context, work_id=work_id)
    )

    async with session_factory() as session:
        rows = (await list_evidence_measurements(session, [unit_id]))[unit_id]
    assert {row.extraction_source for row in rows} == {"regex", "llm"}


@pytest.mark.asyncio
async def test_running_twice_does_not_duplicate_measurements(
    session_factory, monkeypatch
) -> None:
    project_id = await _seed_project(session_factory)
    work_id = await _seed_work(session_factory, project_id, title="A", doi="10.1/a")
    unit_id = await _seed_unit(session_factory, project_id, work_id)
    _patch_extractor(monkeypatch, _stub_extraction())
    context = _context(project_id, session_factory, "off")

    for _ in range(2):
        await apply_work_dimensions(
            context, await extract_work_dimensions(context, work_id=work_id)
        )

    async with session_factory() as session:
        rows = (await session.scalars(select(EvidenceUnit).where(EvidenceUnit.id == unit_id))).all()
        measurements = await list_evidence_measurements(session, [unit_id])
    assert len(rows) == 1
    assert len(measurements[unit_id]) == 1


@pytest.mark.asyncio
async def test_the_extractor_is_called_once_per_work(session_factory, monkeypatch) -> None:
    """抽取只跑一次、结果先缓在内存里，推广门槛才不会让成本翻倍。"""
    project_id = await _seed_project(session_factory)
    work_id = await _seed_work(session_factory, project_id, title="A", doi="10.1/a")
    await _seed_unit(session_factory, project_id, work_id)
    calls = _patch_extractor(monkeypatch, _stub_extraction())
    context = _context(project_id, session_factory, "off")

    extracted = await extract_work_dimensions(context, work_id=work_id)
    await apply_work_dimensions(context, extracted)
    await apply_work_dimensions(context, extracted)

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_extraction_is_independent_of_the_production_mode_flag(
    session_factory, monkeypatch
) -> None:
    """回填是显式的运维动作，不该受 EXPERIMENT_EXTRACTION_MODE 摆布。

    生产上那个开关仍是 off；如果回填也听它的，就永远补不上维度。
    """
    project_id = await _seed_project(session_factory)
    work_id = await _seed_work(session_factory, project_id, title="A", doi="10.1/a")
    unit_id = await _seed_unit(session_factory, project_id, work_id)
    _patch_extractor(monkeypatch, _stub_extraction())

    context = _context(project_id, session_factory, "off")
    assert context.settings.experiment_extraction_enabled is False
    await apply_work_dimensions(
        context, await extract_work_dimensions(context, work_id=work_id)
    )

    async with session_factory() as session:
        assert await list_evidence_measurements(session, [unit_id])


# --- 任务归一：措辞不能把同一套设定拆散 ---------------------------------------


def test_a_free_text_task_is_canonicalised_to_the_bound_ontology():
    """生产实测：同一批结果里「sequential recommendation」有三种写法，于是
    ``HR@10|Beauty|test`` 被拆成三个键，跨论文可比簇一个都形成不了。"""
    from db.repositories.tasks import TaskSpec
    from paperforge_worker.pipelines.evidence import _canonical_task

    spec = TaskSpec(
        slug="recsys.poisoning_attack",
        domain="recsys_attack",
        inclusion_cues=("sequential recommendation", "poisoning"),
    )

    variants = [
        "sequential recommendation",
        "Sequential recommendation with adversarial training",
        "Poisoning attack against sequential recommenders",
    ]

    assert {_canonical_task(value, [spec]) for value in variants} == {"recsys.poisoning_attack"}


def test_an_unmatched_task_still_loses_only_its_formatting():
    """命不中本体就退到大小写/空白归一——不再因排版分裂，也不硬塞进别的任务。"""
    from paperforge_worker.pipelines.evidence import _canonical_task

    assert _canonical_task("  Protein  Folding ", []) == "protein folding"
    assert _canonical_task("protein folding", []) == "protein folding"
    assert _canonical_task("   ", []) is None
    assert _canonical_task(None, []) is None
