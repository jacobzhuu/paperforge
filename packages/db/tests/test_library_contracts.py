"""R1 / R3 数据层契约测试（设计 §4.4.3）。

反例优先：任何一个白名单条件不满足，cite key 都不得出现在写作白名单里。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest
from db import (
    assign_bibtex_key,
    create_project,
    create_user,
    get_writing_whitelist,
    list_entries,
    reference_metadata_payload,
    upsert_entry,
    upsert_work,
)
from db.models.library import ScholarlyWork
from paper_ir import ReferenceMetadata, make_bibtex_key, render_bibtex

pytestmark = pytest.mark.asyncio


@dataclass(frozen=True)
class _Identifier:
    id_type: str
    id_value: str
    is_primary: bool = False


@dataclass(frozen=True)
class _Author:
    author_name: str
    author_order: int
    raw_affiliation: str | None = None


@dataclass(frozen=True)
class _Link:
    url: str
    url_type: str | None = None
    source_name: str | None = None
    is_oa: bool = False


@dataclass(frozen=True)
class _Candidate:
    """最小候选替身：结构与 scholar_gateway.ScholarlyWorkCandidate 对齐。"""

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
    license: str | None = "cc-by"
    is_retracted: bool = False
    citation_count: int | None = 42
    influential_citation_count: int | None = 7
    identifiers: tuple[Any, ...] = (_Identifier("doi", "10.1000/rag", True),)
    links: tuple[Any, ...] = (_Link("https://example.org/rag.pdf", "pdf", "openalex", True),)
    authors: tuple[Any, ...] = (_Author("Ada Lovelace", 1),)
    raw_provider_metadata: dict[str, Any] = field(default_factory=dict)


async def _project(session, **kwargs):
    owner = await create_user(
        session,
        email=f"{uuid.uuid4()}@example.test",
        password_hash="!test-only",
        verified=True,
    )
    return await create_project(
        session,
        title=kwargs.pop("title", "RAG survey"),
        paper_type=kwargs.pop("paper_type", "review"),
        owner_id=owner.id,
        **kwargs,
    )


async def _verified_selected_entry(session, project, candidate=None):
    work, _ = await upsert_work(session, candidate or _Candidate())
    entry, _ = await upsert_entry(
        session,
        project_id=project.id,
        work_id=work.id,
        added_via="search",
        status="selected",
        verified=True,
    )
    payload = await reference_metadata_payload(session, work)
    await assign_bibtex_key(session, entry, make_bibtex_key, reference=ReferenceMetadata(**payload))
    return entry, work


async def test_whitelist_includes_selected_verified_keyed_entry(session):
    project = await _project(session)
    entry, _work = await _verified_selected_entry(session, project)

    whitelist = await get_writing_whitelist(session, project.id)
    assert entry.bibtex_key in whitelist
    assert whitelist[entry.bibtex_key] == entry.work_id


async def test_whitelist_excludes_candidate_status(session):
    project = await _project(session)
    entry, _work = await _verified_selected_entry(session, project)
    entry.status = "candidate"
    await session.flush()

    assert await get_writing_whitelist(session, project.id) == {}


async def test_whitelist_excludes_unverified_entry(session):
    project = await _project(session)
    entry, _work = await _verified_selected_entry(session, project)
    entry.verified_at = None
    await session.flush()

    assert await get_writing_whitelist(session, project.id) == {}


async def test_whitelist_excludes_entry_without_bibtex_key(session):
    project = await _project(session)
    entry, _work = await _verified_selected_entry(session, project)
    entry.bibtex_key = None
    await session.flush()

    assert await get_writing_whitelist(session, project.id) == {}


async def test_whitelist_excludes_retracted_work(session):
    project = await _project(session)
    _entry, work = await _verified_selected_entry(session, project)
    work.is_retracted = True
    await session.flush()

    # 设计 §8：撤稿文献默认不入写作白名单。
    assert await get_writing_whitelist(session, project.id) == {}


async def test_upsert_entry_rejects_unknown_added_via(session):
    project = await _project(session)
    work, _ = await upsert_work(session, _Candidate())
    with pytest.raises(ValueError, match="unsupported added_via"):
        await upsert_entry(
            session,
            project_id=project.id,
            work_id=work.id,
            added_via="llm_guessed",
        )


async def test_verified_at_is_not_cleared_by_later_unverified_upsert(session):
    project = await _project(session)
    work, _ = await upsert_work(session, _Candidate())
    await upsert_entry(
        session,
        project_id=project.id,
        work_id=work.id,
        added_via="search",
        verified=True,
    )
    entry, created = await upsert_entry(
        session,
        project_id=project.id,
        work_id=work.id,
        added_via="search",
        verified=False,
    )
    assert created is False
    assert entry.verified_at is not None


async def test_bibtex_key_is_stable_and_unique_within_project(session):
    project = await _project(session)
    first_entry, _ = await _verified_selected_entry(session, project)
    original_key = first_entry.bibtex_key

    # 同一作者/年份/关键词的第二篇：key 必须加后缀而不是撞车。
    second = _Candidate(
        title="Retrieval Augmented Generation Revisited",
        normalized_title_hash="hash-rag-2",
        doi="10.1000/rag2",
        identifiers=(_Identifier("doi", "10.1000/rag2", True),),
    )
    second_entry, _ = await _verified_selected_entry(session, project, second)

    assert second_entry.bibtex_key != original_key
    # 重复调用只消费已持久化的 key（R3：不重算）。
    first_work = await session.get(ScholarlyWork, first_entry.work_id)
    payload = await reference_metadata_payload(session, first_work)
    again = await assign_bibtex_key(
        session, first_entry, make_bibtex_key, reference=ReferenceMetadata(**payload)
    )
    assert again == original_key


async def test_render_bibtex_consumes_persisted_keys(session):
    project = await _project(session)
    entry, work = await _verified_selected_entry(session, project)
    payload = await reference_metadata_payload(session, work, bibtex_key=entry.bibtex_key)

    rendered = render_bibtex([ReferenceMetadata(**payload)])
    assert f"@article{{{entry.bibtex_key}" in rendered
    assert "Ada Lovelace" in rendered
    assert "10.1000/rag" in rendered


async def test_render_bibtex_refuses_reference_without_persisted_key(session):
    project = await _project(session)
    _entry, work = await _verified_selected_entry(session, project)
    payload = await reference_metadata_payload(session, work)  # 不带 key

    with pytest.raises(ValueError, match="no persisted bibtex_key"):
        render_bibtex([ReferenceMetadata(**payload)])


async def test_upsert_work_merges_without_overwriting_authoritative_fields(session):
    sparse = _Candidate(venue_name=None, publisher=None, abstract=None, citation_count=5)
    work, created = await upsert_work(session, _Candidate())
    assert created is True
    merged, created_again = await upsert_work(session, sparse)

    assert created_again is False
    assert merged.id == work.id
    assert merged.venue_name == "JMLR"  # 已有权威值不被稀疏值抹掉
    assert merged.citation_count == 42  # 引用数取各源最大值


async def test_upsert_work_marks_retraction_from_any_source(session):
    await upsert_work(session, _Candidate())
    retracted = _Candidate(is_retracted=True)
    work, _ = await upsert_work(session, retracted)
    assert work.is_retracted is True


async def test_list_entries_filters_by_status(session):
    project = await _project(session)
    await _verified_selected_entry(session, project)
    other = _Candidate(
        title="Another work",
        normalized_title_hash="hash-other",
        doi="10.1000/other",
        identifiers=(_Identifier("doi", "10.1000/other", True),),
    )
    work, _ = await upsert_work(session, other)
    await upsert_entry(
        session,
        project_id=project.id,
        work_id=work.id,
        added_via="search",
        status="candidate",
        verified=True,
    )

    selected = await list_entries(session, project.id, status="selected")
    candidates = await list_entries(session, project.id, status="candidate")
    assert len(selected) == 1
    assert len(candidates) == 1


async def test_entry_timestamps_are_timezone_aware(session):
    project = await _project(session)
    entry, _work = await _verified_selected_entry(session, project)
    assert entry.verified_at is not None
    assert entry.verified_at.tzinfo is not None
    assert entry.verified_at <= datetime.now(UTC)
