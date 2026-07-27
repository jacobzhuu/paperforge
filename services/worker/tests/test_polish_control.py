"""连贯性润色的可见性与可跳过（真实 Postgres）。

润色发生在所有章节都已「已生成」并落库之后，还要再跑十几分钟。它此前既不报进度、
也没有独立阶段名，用户对着一屏完成的正文和一个不动的「分节写作」只能理解为卡死；
而且除了杀掉整个任务之外没有别的出路。这里锁住两件事：润色是独立阶段并逐节报进度，
以及用户可以中途叫停、已润色的章节保留。
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from db import create_job, list_job_events, request_polish_skip, update_job
from db.models.paper import GenerationJob
from paperforge_worker.config import WorkerSettings
from paperforge_worker.context import JobContext
from paperforge_worker.pipelines.document import write_document
from scholar_gateway import InMemoryHttpCache
from test_document_persistence import _draft_writer, _seed
from test_document_persistence import _latest_document as _document

POLISHED_MARK = "（已润色）"


async def _seed_with_job(session_factory) -> tuple[uuid.UUID, uuid.UUID, str]:
    project_id, cite_key = await _seed(session_factory)
    async with session_factory() as session:
        job = await create_job(session, project_id=project_id, kind="write")
        await update_job(session, job, status="running", stage="write")
        await session.commit()
        return project_id, job.id, cite_key


def _context(project_id: uuid.UUID, job_id: uuid.UUID, session_factory) -> JobContext:
    return JobContext(
        project_id=project_id,
        job_id=job_id,
        settings=WorkerSettings(llm_default_provider="noop"),
        session_factory=session_factory,
        http_client=httpx.Client(),
        scholar_cache=InMemoryHttpCache(),
    )


def _polisher(*, skip_after: int = 0, session_factory=None, job_id: uuid.UUID | None = None):
    """替身润色：给正文打个标记；可在第 N 节之后模拟用户点下「跳过润色」。"""
    calls: list[str] = []

    async def _pass(*, draft, context, whitelist, runner=None):
        calls.append(draft.section_key)
        draft.paragraphs = [
            {**p, "text": f"{p.get('text', '')}{POLISHED_MARK}"} for p in draft.paragraphs
        ]
        if skip_after and len(calls) == skip_after and session_factory and job_id:
            async with session_factory() as session:
                job = await session.get(GenerationJob, job_id)
                await request_polish_skip(session, job)
                await session.commit()
        return draft

    return _pass, calls


async def _events(session_factory, job_id: uuid.UUID) -> list:
    async with session_factory() as session:
        return await list_job_events(session, job_id)


async def test_polish_is_its_own_stage_with_per_section_progress(
    session_factory,
    monkeypatch,
) -> None:
    """润色必须自报家门：独立阶段名 + 每节一条进度事件。"""
    project_id, job_id, cite_key = await _seed_with_job(session_factory)
    monkeypatch.setattr(
        "paperforge_worker.pipelines.document.write_section", _draft_writer(cite_key)
    )
    polisher, _calls = _polisher()
    monkeypatch.setattr("paperforge_worker.pipelines.document.coherence_pass", polisher)

    outcome = await write_document(
        _context(project_id, job_id, session_factory), language="en", title="T"
    )

    events = await _events(session_factory, job_id)
    by_type = [event.event_type for event in events]
    assert by_type.count("polish.section") == 4
    assert "polish.started" in by_type and "polish.completed" in by_type
    # 阶段名不能还是 write——那正是「看起来卡死」的根源。
    polish_events = [e for e in events if e.event_type.startswith("polish.")]
    assert all(e.payload_json.get("total") == 4 for e in polish_events if "total" in e.payload_json)

    # 写作事件带序号，用户能看出「还剩几节」。
    write_events = [e for e in events if e.event_type == "write.section"]
    assert [e.payload_json["index"] for e in write_events] == [1, 2, 3, 4]
    assert all(e.payload_json["total"] == 4 for e in write_events)

    assert outcome.polished_count == 4
    assert outcome.polish_skipped is False
    assert outcome.polish_pending_count == 0

    # 任务上的阶段名也得换过去：进度条读的是这个字段，不是事件类型。
    async with session_factory() as session:
        job = await session.get(GenerationJob, job_id)
    assert job.stage == "polish"
    assert job.progress == pytest.approx(0.90)


async def test_user_can_skip_the_rest_of_the_polish(session_factory, monkeypatch) -> None:
    """中途跳过：已润色的保留，剩下的按初稿交付，任务照常跑完。"""
    project_id, job_id, cite_key = await _seed_with_job(session_factory)
    monkeypatch.setattr(
        "paperforge_worker.pipelines.document.write_section", _draft_writer(cite_key)
    )
    polisher, calls = _polisher(skip_after=1, session_factory=session_factory, job_id=job_id)
    monkeypatch.setattr("paperforge_worker.pipelines.document.coherence_pass", polisher)

    outcome = await write_document(
        _context(project_id, job_id, session_factory), language="en", title="T"
    )

    # 点下跳过时正在跑的那一节跑完；之后不再调用润色。
    assert len(calls) == 1
    assert outcome.polish_skipped is True
    assert outcome.polished_count == 1
    assert outcome.polish_pending_count == 3

    events = await _events(session_factory, job_id)
    skipped = next(e for e in events if e.event_type == "polish.skipped")
    assert skipped.payload_json == {"done": 1, "total": 4, "remaining": 3}

    # 稿子是完整的：跳过的章节留着初稿，不是空章节。
    document = await _document(session_factory, project_id)
    assert document is not None
    async with session_factory() as session:
        from db import list_sections

        rows = await list_sections(session, document.id)
    assert len(rows) == 4
    polished = [row for row in rows if POLISHED_MARK in str(row.body_ir_json)]
    assert len(polished) == 1


async def test_skip_flag_set_before_polish_starts_skips_everything(
    session_factory,
    monkeypatch,
) -> None:
    """在润色开始前就按下的跳过同样生效——不该白跑一节。"""
    project_id, job_id, cite_key = await _seed_with_job(session_factory)
    monkeypatch.setattr(
        "paperforge_worker.pipelines.document.write_section", _draft_writer(cite_key)
    )
    polisher, calls = _polisher()
    monkeypatch.setattr("paperforge_worker.pipelines.document.coherence_pass", polisher)
    async with session_factory() as session:
        job = await session.get(GenerationJob, job_id)
        await request_polish_skip(session, job)
        await session.commit()

    outcome = await write_document(
        _context(project_id, job_id, session_factory), language="en", title="T"
    )

    assert calls == []
    assert outcome.polish_skipped is True
    assert outcome.polished_count == 0
    assert outcome.section_count == 4
