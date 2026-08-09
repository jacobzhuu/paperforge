"""文献库仓储：R1 入库核验的持久化侧 + 写作白名单唯一事实源（设计 §4.4.3）。"""

from __future__ import annotations

import html
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models.library import (
    LITERATURE_ROLES,
    EligibilityDecision,
    LibraryEntry,
    LiteratureCard,
    ScholarlyWork,
)
from db.repositories.works import get_work_authors, resolve_canonical_work

ENTRY_STATUSES = frozenset({"candidate", "selected", "excluded", "candidate_uncertain"})

# R1：只有这些来源可以产生 library_entry（设计 §4.4.3）。
ADDED_VIA = frozenset(
    {
        "search",
        "snowball",
        "doi_import",
        "bibtex_import",
        "pdf_upload",
        "llm_suggested_verified",
    }
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
    literature_role: str | None = None,
) -> tuple[LibraryEntry, bool]:
    """写入或更新一条 library_entry，返回 (entry, created)。

    ``verified=True`` 只应由通过 R1 核验的路径传入（真实检索响应、
    DOI/BibTeX 反查成功、LLM 建议且反查成功）。核验时间戳一旦写入不再回退。
    """
    if added_via not in ADDED_VIA:
        raise ValueError(f"unsupported added_via: {added_via}")
    if status not in ENTRY_STATUSES:
        raise ValueError(f"unsupported entry status: {status}")
    if literature_role is not None and literature_role not in LITERATURE_ROLES:
        raise ValueError(f"unsupported literature role: {literature_role}")
    work = await session.get(ScholarlyWork, work_id)
    if work is not None:
        work_id = (await resolve_canonical_work(session, work)).id

    entry = await get_entry(session, project_id=project_id, work_id=work_id)
    created = False
    if entry is None:
        entry = LibraryEntry(
            project_id=project_id,
            work_id=work_id,
            status=status,
            relevance_score=relevance_score,
            rank_reason_json=rank_reason,
            literature_role=literature_role or "general",
            user_pinned=literature_role == "core",
            added_via=added_via,
        )
        session.add(entry)
        created = True
    else:
        if relevance_score is not None:
            entry.relevance_score = relevance_score
        if rank_reason is not None:
            entry.rank_reason_json = rank_reason
        if literature_role is not None:
            entry.literature_role = literature_role
            entry.user_pinned = literature_role == "core"
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


async def set_entry_role(
    session: AsyncSession,
    entry: LibraryEntry,
    literature_role: str,
) -> LibraryEntry:
    if literature_role not in LITERATURE_ROLES:
        raise ValueError(f"unsupported literature role: {literature_role}")
    entry.literature_role = literature_role
    # Core papers must survive automatic eligibility screening.  Keep the
    # existing pin bit as a backward-compatible projection until SCREEN reads
    # literature_role directly.
    entry.user_pinned = literature_role == "core"
    await session.flush()
    return entry


async def upsert_eligibility_decision(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    work_id: uuid.UUID,
    decision: str,
    criterion_hits: dict[str, Any],
    anchor_facet_hit: bool,
    reason: str,
    decided_by: str = "deterministic",
    model: str | None = None,
) -> EligibilityDecision:
    if decision not in {"include", "exclude", "uncertain"}:
        raise ValueError(f"unsupported eligibility decision: {decision}")
    row = await session.scalar(
        select(EligibilityDecision).where(
            EligibilityDecision.project_id == project_id, EligibilityDecision.work_id == work_id
        )
    )
    if row is None:
        row = EligibilityDecision(
            project_id=project_id,
            work_id=work_id,
            decision=decision,
            decided_by=decided_by,
        )
        session.add(row)
    row.decision = decision
    row.criterion_hits_json = criterion_hits
    row.anchor_facet_hit = anchor_facet_hit
    row.reason = reason
    row.decided_by = decided_by
    row.model = model
    await session.flush()
    return row


async def list_eligibility_decisions(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    decision: str | None = None,
) -> list[tuple[EligibilityDecision, ScholarlyWork]]:
    """筛选判定 + 对应文献。

    这些行一直在写，却从来没有被读回过——用户只看得到文献最后的 status，
    看不到「为什么被排除/为什么存疑」，于是每一次筛选偏差都是不可诊断的。
    """
    stmt = (
        select(EligibilityDecision, ScholarlyWork)
        .join(ScholarlyWork, ScholarlyWork.id == EligibilityDecision.work_id)
        .where(EligibilityDecision.project_id == project_id)
        .order_by(EligibilityDecision.decision, ScholarlyWork.canonical_title)
    )
    if decision is not None:
        if decision not in {"include", "exclude", "uncertain"}:
            raise ValueError(f"unsupported eligibility decision: {decision}")
        stmt = stmt.where(EligibilityDecision.decision == decision)
    return [(row, work) for row, work in (await session.execute(stmt)).all()]


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
        await session.scalars(select(LiteratureCard).where(LiteratureCard.project_id == project_id))
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
    quotable_points: list[Any] | None = None,
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
    authors = await get_work_authors(session, work.id)
    return {
        "work_key": str(work.id),
        "bibtex_key": bibtex_key,
        "title": _clean_reference_text(work.canonical_title),
        "normalized_title": None,
        "publication_year": work.publication_year,
        "venue_name": _clean_reference_text(work.venue_name),
        "publisher": _clean_reference_text(work.publisher),
        "doi": work.doi,
        "pmid": work.pmid,
        "pmcid": work.pmcid,
        "arxiv_id": work.arxiv_id,
        "openalex_id": work.openalex_id,
        "semantic_scholar_id": work.semantic_scholar_id,
        "corpus_id": work.corpus_id,
        "work_type": work.work_type,
        "language": work.language,
        "citation_metadata": {
            "authors": [
                {
                    **author,
                    "author_name": _clean_author_name(str(author.get("author_name") or "")),
                }
                for author in authors
                if _clean_author_name(str(author.get("author_name") or ""))
            ]
        },
    }


def _clean_reference_text(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = html.unescape(value)
    cleaned = re.sub(r"</?(?:i|b|em|strong|sub|sup)\b[^>]*>", "", cleaned, flags=re.I)
    cleaned = re.sub(r"<[^>]+>", "", cleaned)
    return " ".join(cleaned.split()) or None


def _clean_author_name(value: str) -> str:
    cleaned = _clean_reference_text(value) or ""
    if "," in cleaned:
        return cleaned
    parts = cleaned.split()
    if len(parts) == 2 and re.fullmatch(r"(?:[A-Z]\.?){1,4}", parts[-1]):
        return f"{parts[0]}, {parts[1]}"
    return cleaned
