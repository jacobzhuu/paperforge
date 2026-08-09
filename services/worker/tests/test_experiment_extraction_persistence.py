"""LLM 实验抽取的落库行为（真实 Postgres）。

三件事必须靠真库验证，替身证明不了：

* ``off`` 档下这条链路一行都不写——"零行为变化"是 Phase 2 上线的前提；
* ``shadow`` 档写 ``experiment_v3_llm``，但 ``EvidenceMeasurement`` 仍只来自正则通道，
  因此 comparability_key、SYNTH 与正文完全不受影响；
* ``on`` 档下两篇报告同一 (task, dataset, metric, split) 的论文，
  在库里拿到**相同**的 comparability_key —— 这正是 P0-3 想解锁的东西。
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from db import create_project, create_user, upsert_entry, upsert_work
from db.models.library import (
    DocumentChunk,
    DocumentFile,
    DocumentParse,
    EvidenceMeasurement,
    EvidenceUnit,
    ExperimentResult,
    StructuredExtraction,
)
from paperforge_worker.config import WorkerSettings
from paperforge_worker.context import JobContext
from paperforge_worker.pipelines import evidence as evidence_module
from paperforge_worker.pipelines.evidence import (
    LLM_STRUCTURED_EXTRACTION_SCHEMA,
    STRUCTURED_EXTRACTION_SCHEMA,
    extract_evidence_units,
)
from paperforge_worker.pipelines.experiment_extraction import (
    ExperimentExtraction,
    ExtractedResultCell,
)
from paperforge_worker.pipelines.fulltext import PARSER_VERSION
from scholar_gateway import InMemoryHttpCache
from sqlalchemy import select

FULLTEXT = (
    "[[PAGE=7|TABLE=3]] Table 3 reports that our model reaches an accuracy of 0.923 "
    "on the CORE-BENCH test split, which improves over prior work."
)

SPAN = (
    "Table 3 reports that our model reaches an accuracy of 0.923 "
    "on the CORE-BENCH test split, which improves over prior work."
)


class _Candidate:
    """provider 候选替身；upsert_work 只读这些字段。"""

    normalized_title_hash = None
    abstract = "We evaluate a sequence classifier on CORE-BENCH."
    publication_year = 2025
    publication_date = "2025-03-01"
    work_type = "journal-article"
    venue_name = "JMLR"
    publisher = "ACME"
    language = "en"
    doi = None
    pmid = None
    pmcid = None
    arxiv_id = None
    openalex_id = None
    semantic_scholar_id = None
    corpus_id = None
    oa_status = "gold"
    license = None
    citation_count = 3
    influential_citation_count = None
    is_retracted = False
    authors = ()
    identifiers = ()
    links = ()
    provider_name = "openalex"
    provider_record_id = None

    def __init__(self, title: str, doi: str) -> None:
        self.title = title
        self.doi = doi


def _settings(mode: str) -> WorkerSettings:
    return WorkerSettings(
        llm_default_provider="noop",
        experiment_extraction_mode=mode,
        experiment_extraction_max_works=10,
    )


def _context(project_id: uuid.UUID, session_factory, mode: str) -> JobContext:
    return JobContext(
        project_id=project_id,
        job_id=None,
        settings=_settings(mode),
        session_factory=session_factory,
        http_client=httpx.Client(),
        scholar_cache=InMemoryHttpCache(),
    )


async def _seed_work(session_factory, project_id: uuid.UUID, *, title: str, doi: str):
    """入库一篇 selected 文献，连同已解析的全文与一条可定位的 chunk。"""
    async with session_factory() as session:
        work, _ = await upsert_work(session, _Candidate(title, doi))
        await upsert_entry(
            session,
            project_id=project_id,
            work_id=work.id,
            added_via="search",
            status="selected",
            verified=True,
        )
        document = DocumentFile(
            project_id=None,
            work_id=work.id,
            access_scope="shared",
            kind="oa_pdf",
            object_key=f"shared/oa/works/{work.id}/f.bin",
            mime="application/pdf",
            bytes=1024,
            content_hash=uuid.uuid4().hex,
        )
        session.add(document)
        await session.flush()
        parse = DocumentParse(
            document_file_id=document.id,
            parser_version=PARSER_VERSION,
            status="parsed",
            extracted_text=FULLTEXT,
            metadata_json={"structured_objects": [{"object_ref": "table:3", "page_number": 7}]},
        )
        session.add(parse)
        await session.flush()
        session.add(
            DocumentChunk(
                document_parse_id=parse.id,
                chunk_no=1,
                text=FULLTEXT,
                content_role="results",
                page=7,
                section_path="Results",
                object_ref="table:3",
            )
        )
        await session.commit()
        return work.id


async def _seed_project(session_factory) -> uuid.UUID:
    async with session_factory() as session:
        owner = await create_user(
            session,
            email=f"{uuid.uuid4()}@example.test",
            password_hash="!test-only",
            verified=True,
        )
        project = await create_project(
            session,
            title="Comparability review",
            paper_type="review",
            language="en",
            topic="sequence classification",
            owner_id=owner.id,
        )
        await session.commit()
        return project.id


def _stub_extraction(*, dataset: str = "CORE-BENCH", split: str = "test") -> ExperimentExtraction:
    """一份已经通过准入规则的抽取结果。

    这里刻意跳过模型调用：准入规则本身在 test_experiment_extraction.py 里逐条覆盖，
    这个文件要验证的是"通过之后落库对不对"。
    """
    return ExperimentExtraction(
        task="sequence classification",
        task_variant=None,
        protocol={"optimizer": "AdamW"},
        cells=(
            ExtractedResultCell(
                metric_name="accuracy",
                value=0.923,
                dataset=dataset,
                split=split,
                source_location="table:3, p.7",
                locator_verified=True,
                verbatim_span=SPAN,
            ),
        ),
        model="test-extractor",
    )


def _patch_extractor(monkeypatch, extraction: ExperimentExtraction | None) -> list[int]:
    """替换模型调用，并数它被调了几次（预算与档位都靠这个断言）。"""
    calls: list[int] = []

    async def _fake(context, *, document, fulltext):  # noqa: ANN001 - 内部替身
        calls.append(1)
        return extraction

    monkeypatch.setattr(evidence_module, "_llm_structured_extraction", _fake)
    return calls


async def _seed_card(session_factory, project_id, work_id) -> None:
    """卡片带 fulltext_used，证据候选才会走全文分支而不是摘要分支。"""
    from db import upsert_card

    async with session_factory() as session:
        await upsert_card(
            session,
            project_id=project_id,
            work_id=work_id,
            summary="s",
            contributions=[],
            methods=[],
            results=[],
            limitations=[],
            quotable_points=[
                {"text": SPAN, "page": 7, "section": "Results", "object_ref": "table:3"}
            ],
            fulltext_used=True,
            extraction_model="test",
            source_hash="h",
        )
        await session.commit()


@pytest.mark.asyncio
async def test_off_mode_writes_no_llm_extraction_row(session_factory, monkeypatch) -> None:
    project_id = await _seed_project(session_factory)
    work_id = await _seed_work(session_factory, project_id, title="A", doi="10.1/a")
    await _seed_card(session_factory, project_id, work_id)
    calls = _patch_extractor(monkeypatch, _stub_extraction())

    outcome = await extract_evidence_units(
        _context(project_id, session_factory, "off"),
    )

    assert calls == [], "off 档不得调用模型"
    assert outcome.llm_extraction_works == 0
    async with session_factory() as session:
        schemas = list(
            (await session.scalars(select(StructuredExtraction.schema_version))).all()
        )
    assert schemas == [STRUCTURED_EXTRACTION_SCHEMA], "off 档只应留下正则那一份"


@pytest.mark.asyncio
async def test_shadow_mode_persists_v3_but_leaves_measurements_untouched(
    session_factory, monkeypatch
) -> None:
    project_id = await _seed_project(session_factory)
    work_id = await _seed_work(session_factory, project_id, title="A", doi="10.1/a")
    await _seed_card(session_factory, project_id, work_id)
    _patch_extractor(monkeypatch, _stub_extraction())

    outcome = await extract_evidence_units(
        _context(project_id, session_factory, "shadow"),
    )

    assert outcome.llm_extraction_works == 1
    assert outcome.llm_cells_accepted == 1
    assert outcome.llm_cells_locator_verified == 1
    assert outcome.to_payload()["llm_extraction"]["locator_verification_rate"] == 1.0

    async with session_factory() as session:
        schemas = sorted(
            (await session.scalars(select(StructuredExtraction.schema_version))).all()
        )
        sources = sorted(
            {
                value
                for value in (
                    await session.scalars(select(EvidenceMeasurement.extraction_source))
                ).all()
            }
        )
    assert schemas == [STRUCTURED_EXTRACTION_SCHEMA, LLM_STRUCTURED_EXTRACTION_SCHEMA]
    # 影子档的红线：测量值仍然只来自正则，可比性与正文因此完全不变。
    assert sources == ["regex"]


@pytest.mark.asyncio
async def test_on_mode_promotes_llm_dimensions_into_measurements(
    session_factory, monkeypatch
) -> None:
    project_id = await _seed_project(session_factory)
    work_id = await _seed_work(session_factory, project_id, title="A", doi="10.1/a")
    await _seed_card(session_factory, project_id, work_id)
    _patch_extractor(monkeypatch, _stub_extraction())

    await extract_evidence_units(
        _context(project_id, session_factory, "on"),
    )

    async with session_factory() as session:
        rows = list(
            (
                await session.scalars(
                    select(EvidenceMeasurement).where(
                        EvidenceMeasurement.extraction_source == "llm"
                    )
                )
            ).all()
        )
    assert len(rows) == 1
    assert rows[0].dataset == "CORE-BENCH"
    assert rows[0].split == "test"
    assert rows[0].task == "sequence classification"
    assert rows[0].locator_verified is True


@pytest.mark.asyncio
async def test_two_works_reporting_the_same_setup_share_a_comparability_key(
    session_factory, monkeypatch
) -> None:
    """P0-3 的验收点：填齐维度之后，跨论文的同一实验设置真的可比。"""
    project_id = await _seed_project(session_factory)
    work_a = await _seed_work(session_factory, project_id, title="A", doi="10.1/a")
    work_b = await _seed_work(session_factory, project_id, title="B", doi="10.1/b")
    await _seed_card(session_factory, project_id, work_a)
    await _seed_card(session_factory, project_id, work_b)
    _patch_extractor(monkeypatch, _stub_extraction())

    await extract_evidence_units(
        _context(project_id, session_factory, "on"),
    )

    async with session_factory() as session:
        keys = list(
            (
                await session.scalars(
                    select(EvidenceMeasurement.comparability_key).where(
                        EvidenceMeasurement.extraction_source == "llm"
                    )
                )
            ).all()
        )
    assert len(keys) == 2
    assert len(set(keys)) == 1, "同一 (task, dataset, metric, split) 必须落在同一个 key 上"


@pytest.mark.asyncio
async def test_different_datasets_stay_incomparable(session_factory, monkeypatch) -> None:
    """反向保证：数据集不同就不能被合并成一个可比簇。"""
    project_id = await _seed_project(session_factory)
    work_a = await _seed_work(session_factory, project_id, title="A", doi="10.1/a")
    work_b = await _seed_work(session_factory, project_id, title="B", doi="10.1/b")
    await _seed_card(session_factory, project_id, work_a)
    await _seed_card(session_factory, project_id, work_b)

    extractions = {
        str(work_a): _stub_extraction(dataset="CORE-BENCH"),
        str(work_b): _stub_extraction(dataset="OTHER-BENCH"),
    }

    async def _fake(context, *, document, fulltext):  # noqa: ANN001
        return extractions[str(document.work_id)]

    monkeypatch.setattr(evidence_module, "_llm_structured_extraction", _fake)

    await extract_evidence_units(
        _context(project_id, session_factory, "on"),
    )

    async with session_factory() as session:
        keys = list(
            (
                await session.scalars(
                    select(EvidenceMeasurement.comparability_key).where(
                        EvidenceMeasurement.extraction_source == "llm"
                    )
                )
            ).all()
        )
    assert len(set(keys)) == 2


@pytest.mark.asyncio
async def test_unverified_locator_never_reaches_measurements(
    session_factory, monkeypatch
) -> None:
    """定位没核验过的单元格入库供诊断，但不得进入可比性计算。"""
    project_id = await _seed_project(session_factory)
    work_id = await _seed_work(session_factory, project_id, title="A", doi="10.1/a")
    await _seed_card(session_factory, project_id, work_id)
    unverified = ExperimentExtraction(
        task="sequence classification",
        cells=(
            ExtractedResultCell(
                metric_name="accuracy",
                value=0.923,
                dataset="CORE-BENCH",
                split="test",
                source_location="table:42, p.99",
                locator_verified=False,
                verbatim_span=SPAN,
            ),
        ),
        model="test-extractor",
    )
    _patch_extractor(monkeypatch, unverified)

    await extract_evidence_units(
        _context(project_id, session_factory, "on"),
    )

    async with session_factory() as session:
        llm_measurements = list(
            (
                await session.scalars(
                    select(EvidenceMeasurement).where(
                        EvidenceMeasurement.extraction_source == "llm"
                    )
                )
            ).all()
        )
        results = list(
            (
                await session.scalars(
                    select(ExperimentResult).where(ExperimentResult.extraction_source == "llm")
                )
            ).all()
        )
    assert llm_measurements == [], "未核验定位不得进入 EvidenceMeasurement"
    assert len(results) == 1, "但仍要留在结果表里供影子期诊断"
    assert results[0].locator_verified is False


@pytest.mark.asyncio
async def test_budget_exhaustion_falls_back_to_regex_only(session_factory, monkeypatch) -> None:
    project_id = await _seed_project(session_factory)
    work_a = await _seed_work(session_factory, project_id, title="A", doi="10.1/a")
    work_b = await _seed_work(session_factory, project_id, title="B", doi="10.1/b")
    await _seed_card(session_factory, project_id, work_a)
    await _seed_card(session_factory, project_id, work_b)
    calls = _patch_extractor(monkeypatch, _stub_extraction())

    context = _context(project_id, session_factory, "shadow")
    context.settings.experiment_extraction_max_works = 1

    outcome = await extract_evidence_units(context)

    assert len(calls) == 1, "预算用完之后不得再调用模型"
    assert outcome.llm_extraction_works == 1
    assert outcome.llm_budget_skipped == 1


@pytest.mark.asyncio
async def test_evidence_units_are_unchanged_by_the_extractor(
    session_factory, monkeypatch
) -> None:
    """证据分级与定位仍由确定性通道独占；模型碰不到它们。"""
    project_id = await _seed_project(session_factory)
    work_id = await _seed_work(session_factory, project_id, title="A", doi="10.1/a")
    await _seed_card(session_factory, project_id, work_id)

    async def _units(mode: str) -> list[tuple[Any, ...]]:
        _patch_extractor(monkeypatch, _stub_extraction())
        await extract_evidence_units(
            _context(project_id, session_factory, mode),
        )
        async with session_factory() as session:
            rows = list((await session.scalars(select(EvidenceUnit))).all())
        return sorted(
            (row.text_hash, row.grade, row.page, row.object_ref, row.extraction_model)
            for row in rows
        )

    assert await _units("off") == await _units("on")
