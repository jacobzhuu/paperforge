"""叙述性综合的编排行为（Phase 4，真实 Postgres）。

三件事必须钉死：

* **关掉时逐字节不变。** bundle 里连 ``synthesis`` 键都不该出现，一行都不写。
* **``answer_status`` 永不被它改写。** readiness 与写作前置门禁的输入与开关无关。
* **``bundle_hash`` 真的省钱。** SYNTH 在一次完整流水线里最多跑 4 次（初次 + 两轮
  补充 + PDF 上传刷新），bundle 没变就一次调用都不该再发生。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from db import (
    create_project,
    create_user,
    list_question_syntheses,
    list_research_questions,
    replace_research_questions,
    upsert_entry,
    upsert_evidence_measurement,
    upsert_evidence_unit,
    upsert_question_evidence_link,
    upsert_work,
)
from paperforge_worker.config import WorkerSettings
from paperforge_worker.context import JobContext
from paperforge_worker.pipelines import synthesis as synthesis_module
from paperforge_worker.pipelines.synthesis import (
    enrich_with_synthesis,
    load_synthesis_bundles,
    synthesize_questions,
)
from paperforge_worker.pipelines.synthesis_llm import QuestionSynthesisResult, SynthesisEntry
from scholar_gateway import InMemoryHttpCache


@dataclass(frozen=True)
class _Author:
    author_name: str
    author_order: int
    raw_affiliation: str | None = None


@dataclass(frozen=True)
class _Candidate:
    title: str = "A comparable study"
    normalized_title_hash: str = "hash-synth"
    abstract: str | None = "We compare methods."
    publication_year: int | None = 2023
    publication_date: str | None = "2023-05-02"
    work_type: str | None = "journal-article"
    venue_name: str | None = "JMLR"
    publisher: str | None = "ACME"
    language: str | None = "en"
    doi: str | None = "10.1000/synth"
    pmid: str | None = None
    pmcid: str | None = None
    arxiv_id: str | None = None
    openalex_id: str | None = None
    semantic_scholar_id: str | None = None
    corpus_id: str | None = None
    oa_status: str | None = "gold"
    license: str | None = "cc-by"
    is_retracted: bool = False
    citation_count: int | None = 3
    influential_citation_count: int | None = 0
    identifiers: tuple[Any, ...] = ()
    links: tuple[Any, ...] = ()
    authors: tuple[Any, ...] = (_Author("Ada Lovelace", 1),)
    raw_provider_metadata: dict[str, Any] = field(default_factory=dict)


def _context(project_id: uuid.UUID, session_factory, **settings: Any) -> JobContext:
    return JobContext(
        project_id=project_id,
        job_id=None,
        settings=WorkerSettings(llm_default_provider="noop", **settings),
        session_factory=session_factory,
        http_client=httpx.Client(),
        scholar_cache=InMemoryHttpCache(),
    )


async def _seed(session_factory) -> tuple[uuid.UUID, uuid.UUID]:
    """一个子问题 + 两篇论文的同可比键证据 —— 刚好够触发综合。"""
    async with session_factory() as session:
        owner = await create_user(
            session,
            email=f"{uuid.uuid4()}@example.test",
            password_hash="!test-only",
            verified=True,
        )
        project = await create_project(
            session,
            title="Synthesis enrichment",
            paper_type="review",
            language="en",
            topic="comparable results",
            owner_id=owner.id,
        )
        await replace_research_questions(
            session,
            project_id=project.id,
            core_text="Does the method transfer?",
            sub_questions=[{"text": "Does the reported gain hold across datasets?"}],
        )
        questions = await list_research_questions(session, project.id, kind="sub")
        question = questions[0]

        for index in range(2):
            work, _ = await upsert_work(
                session,
                _Candidate(
                    title=f"Study {index}",
                    normalized_title_hash=f"hash-synth-{index}",
                    doi=f"10.1000/synth{index}",
                ),
            )
            await upsert_entry(
                session,
                project_id=project.id,
                work_id=work.id,
                added_via="search",
                status="selected",
                verified=True,
            )
            unit, _ = await upsert_evidence_unit(
                session,
                work_id=work.id,
                project_id=project.id,
                kind="experimental_fact",
                grade="B_located_prose",
                text=f"Study {index} reports NDCG@10 of 0.4{index}1 on the shared benchmark.",
                text_hash=f"hash-unit-{index}",
                page=4,
            )
            await upsert_evidence_measurement(
                session,
                evidence_unit_id=unit.id,
                metric_name="ndcg@10",
                value=0.401 + index,
                dataset="beauty",
                task="sequential_recommendation",
                split="leave-one-out",
            )
            await upsert_question_evidence_link(
                session,
                research_question_id=question.id,
                evidence_unit_id=unit.id,
                stance="supports",
            )
        await session.commit()
        return project.id, question.id


def _result(claim: str = "The gain holds on the shared benchmark.") -> QuestionSynthesisResult:
    return QuestionSynthesisResult(
        generator="llm:test-model",
        claim=claim,
        agreement=(
            SynthesisEntry(kind="agreement", statement="Both studies agree.", evidence_ids=()),
        ),
    )


def _stub_synthesizer(monkeypatch, result=None, calls: list | None = None):
    async def _fake(*, bundle, runner, allowed_dimensions, language):
        if calls is not None:
            calls.append(bundle["question_id"])
        return result if result is not None else _result()

    monkeypatch.setattr(synthesis_module, "synthesize_bundle", _fake)


@pytest.mark.asyncio
async def test_disabled_leaves_the_bundle_byte_identical(session_factory, monkeypatch) -> None:
    """关掉开关时必须逐字节回到引入前的行为——这是它作为开关的意义。

    默认值已在 2026-08-15 翻成 True（此前它自落地起从未在生产跑过，
    `question_synthesis` 全库零行），所以这条用例现在要显式关掉才测得到关态。
    """
    project_id, _ = await _seed(session_factory)
    calls: list[str] = []
    _stub_synthesizer(monkeypatch, calls=calls)

    outcome = await synthesize_questions(
        _context(project_id, session_factory, synthesis_llm_enabled=False)
    )

    assert calls == []
    assert "synthesis" not in outcome.bundles[0]
    assert "synthesis" not in outcome.to_payload()
    async with session_factory() as session:
        stored = await list_question_syntheses(session, project_id=project_id, bundle_hashes=["x"])
    assert stored == []


@pytest.mark.asyncio
async def test_enabled_attaches_a_narrative_without_touching_answer_status(
    session_factory, monkeypatch
) -> None:
    project_id, question_id = await _seed(session_factory)
    _stub_synthesizer(monkeypatch)

    # 基线取「关掉综合」的那一版：这条用例要证明的是开启后 answer_status 不变。
    baseline = await synthesize_questions(
        _context(project_id, session_factory, synthesis_llm_enabled=False)
    )
    before = [(b["question_id"], b["answer_status"]) for b in baseline.bundles]

    outcome = await synthesize_questions(
        _context(project_id, session_factory, synthesis_llm_enabled=True)
    )

    assert [(b["question_id"], b["answer_status"]) for b in outcome.bundles] == before
    assert outcome.bundles[0]["synthesis"]["claim"] == "The gain holds on the shared benchmark."
    assert outcome.synthesis_generated == 1
    async with session_factory() as session:
        questions = await list_research_questions(session, project_id, kind="sub")
    assert [q.answer_status for q in questions] == [before[0][1]]


@pytest.mark.asyncio
async def test_an_unchanged_bundle_makes_no_second_call(session_factory, monkeypatch) -> None:
    project_id, _ = await _seed(session_factory)
    calls: list[str] = []
    _stub_synthesizer(monkeypatch, calls=calls)

    first = await synthesize_questions(
        _context(project_id, session_factory, synthesis_llm_enabled=True)
    )
    second = await synthesize_questions(
        _context(project_id, session_factory, synthesis_llm_enabled=True)
    )

    assert len(calls) == 1
    assert second.synthesis_generated == 0
    assert second.synthesis_reused == 1
    # 复用的那一份必须真的挂回去了，否则大纲/写作会拿到空综合。
    assert second.bundles[0]["synthesis"]["claim"] == first.bundles[0]["synthesis"]["claim"]


@pytest.mark.asyncio
async def test_new_evidence_invalidates_the_stored_synthesis(session_factory, monkeypatch) -> None:
    """bundle 变了就必须重新综合——否则补充轮次后的正文引用的是旧结论。"""
    project_id, question_id = await _seed(session_factory)
    calls: list[str] = []
    _stub_synthesizer(monkeypatch, calls=calls)

    await synthesize_questions(_context(project_id, session_factory, synthesis_llm_enabled=True))

    async with session_factory() as session:
        works = await upsert_work(
            session,
            _Candidate(title="Study 3", normalized_title_hash="hash-synth-3", doi="10.1000/s3"),
        )
        await upsert_entry(
            session,
            project_id=project_id,
            work_id=works[0].id,
            added_via="search",
            status="selected",
            verified=True,
        )
        unit, _ = await upsert_evidence_unit(
            session,
            work_id=works[0].id,
            project_id=project_id,
            kind="experimental_fact",
            grade="B_located_prose",
            text="A third study reports a different direction.",
            text_hash="hash-unit-3",
            page=6,
        )
        await upsert_question_evidence_link(
            session,
            research_question_id=question_id,
            evidence_unit_id=unit.id,
            stance="contradicts",
        )
        await session.commit()

    await synthesize_questions(_context(project_id, session_factory, synthesis_llm_enabled=True))

    assert len(calls) == 2


@pytest.mark.asyncio
async def test_a_fully_rejected_response_is_remembered_and_not_retried(
    session_factory, monkeypatch
) -> None:
    """一条都没通过校验时落 deterministic 行当负缓存，并留下降级记录。"""
    project_id, _ = await _seed(session_factory)
    calls: list[str] = []
    _stub_synthesizer(
        monkeypatch,
        result=QuestionSynthesisResult(
            generator="llm:test-model",
            rejected=({"kind": "conflict", "reason": "unknown_comparability_key"},),
        ),
        calls=calls,
    )

    context = _context(project_id, session_factory, synthesis_llm_enabled=True)
    first = await synthesize_questions(context)
    second = await synthesize_questions(
        _context(project_id, session_factory, synthesis_llm_enabled=True)
    )

    assert first.bundles[0]["synthesis"] is None
    assert second.bundles[0]["synthesis"] is None
    assert len(calls) == 1
    assert any(item["reason"] == "llm_rejected" for item in context.warnings)


@pytest.mark.asyncio
async def test_a_question_below_the_evidence_floor_is_never_sent(
    session_factory, monkeypatch
) -> None:
    project_id, _ = await _seed(session_factory)
    calls: list[str] = []
    _stub_synthesizer(monkeypatch, calls=calls)
    context = _context(project_id, session_factory, synthesis_llm_enabled=True)
    outcome = await load_synthesis_bundles(context)
    outcome.bundles[0]["answer_status"] = "insufficient_evidence"

    await enrich_with_synthesis(context, outcome)

    assert calls == []
    assert outcome.synthesis_skipped == 1


@pytest.mark.asyncio
async def test_the_per_job_cap_bounds_the_spend(session_factory, monkeypatch) -> None:
    project_id, _ = await _seed(session_factory)
    calls: list[str] = []
    _stub_synthesizer(monkeypatch, calls=calls)
    context = _context(
        project_id,
        session_factory,
        synthesis_llm_enabled=True,
        synthesis_llm_max_questions=0,
    )

    outcome = await synthesize_questions(context)

    assert calls == []
    assert outcome.synthesis_skipped == 1
