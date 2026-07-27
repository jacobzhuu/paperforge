"""write 阶段的落库时机（真实 Postgres）。

真实教训：一次一键生成里 write 跑了 18 分钟写完 7 节，而所有章节只在阶段末尾
一次性 upsert——中途撞上 arq job_timeout 或 worker 重启，这 18 分钟连同已经写好的
正文一起蒸发。这里锁住「写完一节存一节」，以及中途落库同样受白名单约束。
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import httpx
import pytest
from db import (
    create_outline,
    create_project,
    create_user,
    list_sections,
    upsert_entry,
    upsert_work,
)
from db.models.paper import PaperDocument
from paperforge_worker.config import WorkerSettings
from paperforge_worker.context import JobContext
from paperforge_worker.pipelines.document import write_document
from paperforge_worker.pipelines.writing import SectionDraft
from scholar_gateway import InMemoryHttpCache
from sqlalchemy import select

OUTLINE_TREE = {
    "topic": "poisoning attacks",
    "language": "en",
    "sections": [
        {"key": "abstract", "kind": "frame", "level": 1, "title": "Abstract"},
        {"key": "s1", "kind": "body", "level": 1, "title": "Attack Objectives"},
        {"key": "s2", "kind": "body", "level": 1, "title": "Threat Models"},
        {"key": "s3", "kind": "body", "level": 1, "title": "Attack Strategies"},
    ],
}


class _Author:
    def __init__(self, name: str, order: int) -> None:
        self.author_name = name
        self.author_order = order
        self.raw_affiliation = None


class _Candidate:
    title = "Data Poisoning in Sequential Recommenders"
    normalized_title_hash = "hash-poison"
    abstract = "We poison sequential recommenders."
    publication_year = 2025
    publication_date = "2025-01-02"
    work_type = "journal-article"
    venue_name = "JMLR"
    publisher = "ACME"
    language = "en"
    doi = "10.1000/poison"
    pmid = None
    pmcid = None
    arxiv_id = None
    openalex_id = None
    semantic_scholar_id = None
    corpus_id = None
    oa_status = "gold"
    license = None
    is_retracted = False
    citation_count = 7
    influential_citation_count = None
    identifiers: tuple[Any, ...] = ()
    links: tuple[Any, ...] = ()
    authors: tuple[Any, ...] = (_Author("Ada Lovelace", 1),)
    raw_provider_metadata: dict[str, Any] = {}


async def _seed(session_factory) -> tuple[uuid.UUID, str]:
    """建项目 + 大纲 + 一条已核验入库文献（白名单里唯一的 cite key）。"""
    async with session_factory() as session:
        owner = await create_user(
            session,
            email=f"{uuid.uuid4()}@example.test",
            password_hash="!test-only",
            verified=True,
        )
        project = await create_project(
            session,
            title="Poisoning review",
            paper_type="review",
            language="en",
            topic="poisoning",
            owner_id=owner.id,
        )
        work, _ = await upsert_work(session, _Candidate())
        entry, _ = await upsert_entry(
            session,
            project_id=project.id,
            work_id=work.id,
            added_via="search",
            status="selected",
            verified=True,
        )
        entry.bibtex_key = "lovelace2025poison"
        await create_outline(session, project_id=project.id, tree=OUTLINE_TREE)
        await session.commit()
        return project.id, entry.bibtex_key


def _context(project_id: uuid.UUID, session_factory) -> JobContext:
    return JobContext(
        project_id=project_id,
        job_id=None,  # 无 job 时 emit 只打日志，测试不必造 generation_job
        settings=WorkerSettings(),
        session_factory=session_factory,
        http_client=httpx.Client(),
        scholar_cache=InMemoryHttpCache(),
    )


def _draft_writer(cite_key: str, *, cancel_at: str | None = None):
    """替身写手：按需在某一节抛 CancelledError，模拟 arq 超时掐断。"""

    async def _write(*, section, cards, whitelist, context, runner, assets=None):
        key = str(section.get("key") or "")
        if key == cancel_at:
            raise asyncio.CancelledError
        draft = SectionDraft(section_key=key, title=str(section.get("title") or key))
        draft.paragraphs = [{"text": f"{key} body.", "cite_keys": [cite_key]}]
        draft.generator = "llm:test-model"
        draft.model = "test-model"
        return draft

    return _write


async def _latest_document(session_factory, project_id: uuid.UUID) -> PaperDocument | None:
    async with session_factory() as session:
        return await session.scalar(
            select(PaperDocument)
            .where(PaperDocument.project_id == project_id)
            .order_by(PaperDocument.version.desc())
        )


async def test_sections_survive_an_interrupted_write(session_factory, monkeypatch) -> None:
    """写到一半被掐断时，此前写完的章节必须已经在库里。"""
    project_id, cite_key = await _seed(session_factory)
    monkeypatch.setattr(
        "paperforge_worker.pipelines.document.write_section",
        _draft_writer(cite_key, cancel_at="s3"),
    )

    with pytest.raises(asyncio.CancelledError):
        await write_document(_context(project_id, session_factory), language="en", title="T")

    document = await _latest_document(session_factory, project_id)
    assert document is not None
    async with session_factory() as session:
        rows = await list_sections(session, document.id)
    # s1、s2 写完即落库；s3 被掐断，abstract 还没轮到（框架章节最后写）。
    assert [row.section_key for row in rows] == ["s1", "s2"]
    assert all(row.body_ir_json for row in rows)
    assert all(row.cite_keys_json == [cite_key] for row in rows)


async def test_interrupted_write_never_persists_off_whitelist_cite_keys(
    session_factory,
    monkeypatch,
) -> None:
    """中途落库不放松 R2：白名单外的引用键在存盘前就被 strip。"""
    project_id, cite_key = await _seed(session_factory)
    monkeypatch.setattr(
        "paperforge_worker.pipelines.document.write_section",
        _draft_writer("hallucinated2099ghost", cancel_at="s2"),
    )

    with pytest.raises(asyncio.CancelledError):
        await write_document(_context(project_id, session_factory), language="en", title="T")

    document = await _latest_document(session_factory, project_id)
    assert document is not None
    async with session_factory() as session:
        rows = await list_sections(session, document.id)
    assert [row.section_key for row in rows] == ["s1"]
    stored = rows[0].body_ir_json or {}
    cited = [
        key
        for block in stored.get("blocks", [])
        for run in block.get("runs", [])
        if run.get("t") == "cite"
        for key in run.get("keys", [])
    ]
    assert cited == []  # 幻觉出来的 key 进不了正文
    assert cite_key not in str(stored)  # 也不会被"修"成白名单里的某个 key
    # 但必须留痕：编辑器要看得见「这里原本有一条被拒的引用」。
    warned = [
        key
        for warning in stored.get("citation_warnings", [])
        for key in warning.get("rejected_keys", [])
    ]
    assert warned == ["hallucinated2099ghost"]


async def test_completed_write_persists_every_section_in_outline_order(
    session_factory,
    monkeypatch,
) -> None:
    """正常跑完时的最终状态不受「中途多存了几次」影响。"""
    project_id, cite_key = await _seed(session_factory)
    monkeypatch.setattr(
        "paperforge_worker.pipelines.document.write_section",
        _draft_writer(cite_key),
    )

    outcome = await write_document(
        _context(project_id, session_factory),
        language="en",
        title="T",
        coherence=False,  # 润色要真调 LLM，这里只验落库
    )

    assert outcome.section_count == 4
    document = await _latest_document(session_factory, project_id)
    assert document is not None and str(document.id) == outcome.document_id
    async with session_factory() as session:
        rows = await list_sections(session, document.id)
    assert [row.section_key for row in rows] == ["abstract", "s1", "s2", "s3"]
    assert [row.order_no for row in rows] == [0, 1, 2, 3]
