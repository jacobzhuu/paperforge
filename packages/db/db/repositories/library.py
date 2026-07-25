"""文献库仓储：R1 入库核验的持久化侧 + 写作白名单唯一事实源（设计 §4.4.3）。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models.library import LibraryEntry, LiteratureCard, ScholarlyWork
from db.repositories.works import get_work_authors

ENTRY_STATUSES = frozenset({"candidate", "selected", "excluded"})
# R1：只有这些来源可以产生 library_entry（设计 §4.4.3）。
ADDED_VIA = frozenset(
    {"search", "snowball", "doi_import", "bibtex_import", "llm_suggested_verified"}
)


def writing_whitelist_stmt(project_id: uuid.UUID) -> Select:
    """R1's single query source for cite-key → work-id eligibility.

    Only explicitly selected, verified, keyed, non-retracted works may enter
    writing prompts or pass R2.
    """
    return (
        select(LibraryEntry.bibtex_key, LibraryEntry.work_id)
        .join(ScholarlyWork, ScholarlyWork.id == LibraryEntry.work_id)
        .where(
            LibraryEntry.project_id == project_id,
            LibraryEntry.status == "selected",
            LibraryEntry.verified_at.is_not(None),
            LibraryEntry.bibtex_key.is_not(None),
            ScholarlyWork.is_retracted.is_(False),
        )
        .order_by(LibraryEntry.created_at, LibraryEntry.id)
    )


async def get_writing_whitelist(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> dict[str, uuid.UUID]:
    """Return the sole R1 whitelist consumed by writing and citation checks."""
    rows = (await session.execute(writing_whitelist_stmt(project_id))).all()
    return {cite_key: work_id for cite_key, work_id in rows if cite_key}


async def get_entry(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    work_id: uuid.UUID,
) -> LibraryEntry | None:
    return await session.scalar(
        select(LibraryEntry).where(
            LibraryEntry.project_id == project_id,
            LibraryEntry.work_id == work_id,
        )
    )


async def upsert_entry(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    work_id: uuid.UUID,
    added_via: str,
    status: str = "candidate",
    relevance_score: float | None = None,
    rank_reason: dict[str, Any] | None = None,
    verified: bool = False,
) -> tuple[LibraryEntry, bool]:
    """写入或更新一条 library_entry，返回 (entry, created)。

    ``verified=True`` 只应由通过 R1 核验的路径传入（真实检索响应、
    DOI/BibTeX 反查成功、LLM 建议且反查成功）。核验时间戳一旦写入不再回退。
    """
    if added_via not in ADDED_VIA:
        raise ValueError(f"unsupported added_via: {added_via}")
    if status not in ENTRY_STATUSES:
        raise ValueError(f"unsupported entry status: {status}")

    entry = await get_entry(session, project_id=project_id, work_id=work_id)
    created = False
    if entry is None:
        entry = LibraryEntry(
            project_id=project_id,
            work_id=work_id,
            status=status,
            relevance_score=relevance_score,
            rank_reason_json=rank_reason,
            added_via=added_via,
        )
        session.add(entry)
        created = True
    else:
        if relevance_score is not None:
            entry.relevance_score = relevance_score
        if rank_reason is not None:
            entry.rank_reason_json = rank_reason
        # 用户已勾选/排除的条目不被后续检索批次改回 candidate。
        if entry.status == "candidate" and status != "candidate":
            entry.status = status
    if verified and entry.verified_at is None:
        entry.verified_at = datetime.now(UTC)
    await session.flush()
    return entry, created


async def set_entry_status(
    session: AsyncSession,
    entry: LibraryEntry,
    status: str,
    *,
    user_pinned: bool | None = None,
) -> LibraryEntry:
    if status not in ENTRY_STATUSES:
        raise ValueError(f"unsupported entry status: {status}")
    entry.status = status
    if user_pinned is not None:
        entry.user_pinned = user_pinned
    await session.flush()
    return entry


async def assign_bibtex_key(
    session: AsyncSession,
    entry: LibraryEntry,
    make_key: Any,
    *,
    reference: Any,
) -> str:
    """R3：核验入库时一次性生成并持久化 bibtex_key；已有 key 直接复用。

    ``make_key(reference, taken=...)`` 由 ``paper_ir.bibtex.make_bibtex_key`` 提供
    （db 包不依赖 paper_ir，因此以可调用注入）。冲突在项目内解决。
    """
    if entry.bibtex_key:
        return entry.bibtex_key
    taken = {
        key
        for key in (
            await session.scalars(
                select(LibraryEntry.bibtex_key).where(
                    LibraryEntry.project_id == entry.project_id,
                    LibraryEntry.bibtex_key.is_not(None),
                )
            )
        ).all()
        if key
    }
    entry.bibtex_key = make_key(reference, taken=taken)
    await session.flush()
    return entry.bibtex_key


async def list_entries(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    status: str | None = None,
    limit: int = 500,
) -> list[tuple[LibraryEntry, ScholarlyWork]]:
    stmt = (
        select(LibraryEntry, ScholarlyWork)
        .join(ScholarlyWork, ScholarlyWork.id == LibraryEntry.work_id)
        .where(LibraryEntry.project_id == project_id)
        .order_by(
            LibraryEntry.user_pinned.desc(),
            LibraryEntry.relevance_score.desc().nullslast(),
            LibraryEntry.created_at,
        )
        .limit(limit)
    )
    if status:
        if status not in ENTRY_STATUSES:
            raise ValueError(f"unsupported entry status: {status}")
        stmt = stmt.where(LibraryEntry.status == status)
    return [(entry, work) for entry, work in (await session.execute(stmt)).all()]


async def delete_entry(session: AsyncSession, entry: LibraryEntry) -> None:
    await session.delete(entry)
    await session.flush()


async def get_cards(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> dict[uuid.UUID, LiteratureCard]:
    rows = (
        await session.scalars(
            select(LiteratureCard).where(LiteratureCard.project_id == project_id)
        )
    ).all()
    return {row.work_id: row for row in rows}


async def upsert_card(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    work_id: uuid.UUID,
    summary: str | None,
    contributions: list[str] | None = None,
    methods: list[str] | None = None,
    results: list[str] | None = None,
    limitations: list[str] | None = None,
    quotable_points: list[str] | None = None,
    fulltext_used: bool = False,
    extraction_model: str | None = None,
    source_hash: str | None = None,
) -> LiteratureCard:
    card = await session.scalar(
        select(LiteratureCard).where(
            LiteratureCard.project_id == project_id,
            LiteratureCard.work_id == work_id,
        )
    )
    if card is None:
        card = LiteratureCard(project_id=project_id, work_id=work_id)
        session.add(card)
    card.summary = summary
    card.contributions_json = contributions or []
    card.methods_json = methods or []
    card.results_json = results or []
    card.limitations_json = limitations or []
    card.quotable_points_json = quotable_points or []
    card.fulltext_used = fulltext_used
    card.extraction_model = extraction_model
    card.source_hash = source_hash
    await session.flush()
    return card


async def find_cached_card(
    session: AsyncSession,
    *,
    work_id: uuid.UUID,
    source_hash: str,
) -> LiteratureCard | None:
    """按 work + source_hash 跨项目复用卡片（设计 §4.3 / §4.9 成本控制）。"""
    return await session.scalar(
        select(LiteratureCard)
        .where(
            LiteratureCard.work_id == work_id,
            LiteratureCard.source_hash == source_hash,
        )
        .limit(1)
    )


async def reference_metadata_payload(
    session: AsyncSession,
    work: ScholarlyWork,
    *,
    bibtex_key: str | None = None,
) -> dict[str, Any]:
    """构造 paper_ir.ReferenceMetadata 的构造参数（db 不依赖 paper_ir）。"""
    return {
        "work_key": str(work.id),
        "bibtex_key": bibtex_key,
        "title": work.canonical_title,
        "normalized_title": None,
        "publication_year": work.publication_year,
        "venue_name": work.venue_name,
        "publisher": work.publisher,
        "doi": work.doi,
        "pmid": work.pmid,
        "pmcid": work.pmcid,
        "arxiv_id": work.arxiv_id,
        "openalex_id": work.openalex_id,
        "semantic_scholar_id": work.semantic_scholar_id,
        "corpus_id": work.corpus_id,
        "work_type": work.work_type,
        "language": work.language,
        "citation_metadata": {"authors": await get_work_authors(session, work.id)},
    }
