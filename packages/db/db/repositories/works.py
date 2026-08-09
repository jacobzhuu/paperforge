"""scholarly_work 归一入库（R1 的持久化侧）。

候选 → 实体的匹配顺序与 dedupe 同源：强标识符精确匹配 → 规范化标题哈希。
只有真实检索响应/反查成功的候选才会到达这里（R1 见 §4.4.3），
本模块不做任何「凭标题猜一条文献」的推断。
"""

from __future__ import annotations

import uuid
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models.library import (
    ScholarlyWork,
    WorkAuthor,
    WorkIdentifier,
    WorkUrl,
    WorkVersionRelation,
)

# 强标识符：命中即认为同一文献（与 scholar_gateway.dedupe 的 EXACT_IDENTIFIER_TYPES 对齐）。
STRONG_IDENTIFIER_FIELDS = (
    "doi",
    "pmid",
    "pmcid",
    "arxiv_id",
    "openalex_id",
    "semantic_scholar_id",
)


class WorkCandidateLike(Protocol):
    """scholar_gateway.ScholarlyWorkCandidate 的结构性契约（db 不依赖 gateway 包）。"""

    title: str
    normalized_title_hash: str
    abstract: str | None
    publication_year: int | None
    publication_date: str | None
    work_type: str | None
    venue_name: str | None
    publisher: str | None
    language: str | None
    doi: str | None
    pmid: str | None
    pmcid: str | None
    arxiv_id: str | None
    openalex_id: str | None
    semantic_scholar_id: str | None
    corpus_id: str | None
    oa_status: str | None
    license: str | None
    is_retracted: bool
    citation_count: int | None
    influential_citation_count: int | None
    identifiers: tuple[Any, ...]
    links: tuple[Any, ...]
    authors: tuple[Any, ...]


async def find_existing_work(
    session: AsyncSession,
    candidate: WorkCandidateLike,
) -> ScholarlyWork | None:
    for field in STRONG_IDENTIFIER_FIELDS:
        value = getattr(candidate, field, None)
        if not value:
            continue
        existing = await session.scalar(
            select(ScholarlyWork).where(getattr(ScholarlyWork, field) == value)
        )
        if existing is not None:
            return await resolve_canonical_work(session, existing)
    for identifier in getattr(candidate, "identifiers", ()) or ():
        existing_id = await session.scalar(
            select(WorkIdentifier).where(
                WorkIdentifier.id_type == identifier.id_type,
                WorkIdentifier.id_value == identifier.id_value,
            )
        )
        if existing_id is not None:
            existing = await session.get(ScholarlyWork, existing_id.work_id)
            if existing is not None:
                return await resolve_canonical_work(session, existing)
    title_hash = getattr(candidate, "normalized_title_hash", None)
    if title_hash:
        work = await session.scalar(
            select(ScholarlyWork).where(ScholarlyWork.normalized_title_hash == title_hash)
        )
        return await resolve_canonical_work(session, work) if work is not None else None
    return None


async def resolve_canonical_work(
    session: AsyncSession,
    work: ScholarlyWork,
) -> ScholarlyWork:
    """Follow explicit version links to the one project-facing master work."""
    seen = {work.id}
    current = work
    while current.canonical_work_id is not None and current.canonical_work_id not in seen:
        seen.add(current.canonical_work_id)
        parent = await session.get(ScholarlyWork, current.canonical_work_id)
        if parent is None:
            break
        current = parent
    return current


async def link_work_versions(
    session: AsyncSession,
    *,
    version_work_id: uuid.UUID,
    canonical_work_id: uuid.UUID,
    relation_type: str,
    confidence: float = 1.0,
    rationale: dict[str, Any] | None = None,
) -> ScholarlyWork:
    """Explicitly merge preprint/conference/journal manifestations safely.

    The version row remains available for provenance and its URLs/identifiers,
    while project library entries resolve to the single canonical entity.
    """
    if version_work_id == canonical_work_id:
        raise ValueError("a work cannot be a version of itself")
    version = await session.get(ScholarlyWork, version_work_id)
    canonical = await session.get(ScholarlyWork, canonical_work_id)
    if version is None or canonical is None:
        raise ValueError("version and canonical works must exist")
    root = await resolve_canonical_work(session, canonical)
    version.canonical_work_id = root.id
    relation = await session.scalar(
        select(WorkVersionRelation).where(
            WorkVersionRelation.source_work_id == version.id,
            WorkVersionRelation.target_work_id == root.id,
            WorkVersionRelation.relation_type == relation_type,
        )
    )
    if relation is None:
        relation = WorkVersionRelation(
            source_work_id=version.id,
            target_work_id=root.id,
            relation_type=relation_type,
            confidence=confidence,
            rationale_json=rationale,
        )
        session.add(relation)
    else:
        relation.confidence = confidence
        relation.rationale_json = rationale
    await session.flush()
    return root


async def upsert_work(
    session: AsyncSession,
    candidate: WorkCandidateLike,
) -> tuple[ScholarlyWork, bool]:
    """写入或补全一条 scholarly_work，返回 (work, created)。

    已存在时只**补空**，不覆盖已有值——不同 provider 的元数据完整度不同，
    先到的权威值（例如 Crossref 的 venue）不该被后到的稀疏值抹掉。
    撤稿标记是例外：任一来源报告撤稿即置真（设计 §8）。
    """
    existing = await find_existing_work(session, candidate)
    if existing is None:
        work = ScholarlyWork(
            canonical_title=candidate.title,
            normalized_title_hash=candidate.normalized_title_hash,
            abstract=candidate.abstract,
            publication_year=candidate.publication_year,
            publication_date=candidate.publication_date,
            work_type=candidate.work_type,
            venue_name=candidate.venue_name,
            publisher=candidate.publisher,
            language=candidate.language,
            doi=candidate.doi,
            pmid=candidate.pmid,
            pmcid=candidate.pmcid,
            arxiv_id=candidate.arxiv_id,
            openalex_id=candidate.openalex_id,
            semantic_scholar_id=candidate.semantic_scholar_id,
            corpus_id=candidate.corpus_id,
            is_retracted=bool(candidate.is_retracted),
            oa_status=candidate.oa_status,
            license=candidate.license,
            citation_count=candidate.citation_count,
            influential_citation_count=candidate.influential_citation_count,
        )
        session.add(work)
        await session.flush()
        created = True
    else:
        work = existing
        _fill_missing(work, candidate)
        if candidate.is_retracted:
            work.is_retracted = True
        await session.flush()
        created = False

    await _sync_identifiers(session, work, candidate)
    await _sync_urls(session, work, candidate)
    await _sync_authors(session, work, candidate)
    await session.flush()
    return work, created


def _fill_missing(work: ScholarlyWork, candidate: WorkCandidateLike) -> None:
    for field in (
        "abstract",
        "publication_year",
        "publication_date",
        "work_type",
        "venue_name",
        "publisher",
        "language",
        "doi",
        "pmid",
        "pmcid",
        "arxiv_id",
        "openalex_id",
        "semantic_scholar_id",
        "corpus_id",
        "oa_status",
        "license",
    ):
        if getattr(work, field, None) in (None, "") and getattr(candidate, field, None):
            setattr(work, field, getattr(candidate, field))
    for field in ("citation_count", "influential_citation_count"):
        incoming = getattr(candidate, field, None)
        current = getattr(work, field, None)
        # 引用数取各源最大值：不同源的覆盖范围不同，取大者更接近真实影响力。
        if incoming is not None and (current is None or incoming > current):
            setattr(work, field, incoming)


async def _sync_identifiers(
    session: AsyncSession,
    work: ScholarlyWork,
    candidate: WorkCandidateLike,
) -> None:
    existing = {
        (row.id_type, row.id_value)
        for row in (
            await session.scalars(select(WorkIdentifier).where(WorkIdentifier.work_id == work.id))
        ).all()
    }
    for identifier in getattr(candidate, "identifiers", ()) or ():
        key = (identifier.id_type, identifier.id_value)
        if key in existing:
            continue
        # Identifiers are globally canonical even before the migration can add a
        # global unique constraint to old installations.  Never duplicate one
        # onto a second manifestation.
        owner = await session.scalar(
            select(WorkIdentifier).where(
                WorkIdentifier.id_type == identifier.id_type,
                WorkIdentifier.id_value == identifier.id_value,
            )
        )
        if owner is not None and owner.work_id != work.id:
            continue
        existing.add(key)
        session.add(
            WorkIdentifier(
                work_id=work.id,
                id_type=identifier.id_type,
                id_value=identifier.id_value,
            )
        )


async def _sync_urls(
    session: AsyncSession,
    work: ScholarlyWork,
    candidate: WorkCandidateLike,
) -> None:
    existing = {
        row.url
        for row in (await session.scalars(select(WorkUrl).where(WorkUrl.work_id == work.id))).all()
    }
    for link in getattr(candidate, "links", ()) or ():
        if link.url in existing:
            continue
        existing.add(link.url)
        session.add(
            WorkUrl(
                work_id=work.id,
                url=link.url,
                url_type=getattr(link, "url_type", None),
                is_oa=bool(getattr(link, "is_oa", False)),
            )
        )


async def _sync_authors(
    session: AsyncSession,
    work: ScholarlyWork,
    candidate: WorkCandidateLike,
) -> None:
    existing = (
        await session.scalars(select(WorkAuthor).where(WorkAuthor.work_id == work.id))
    ).all()
    if existing:
        # 作者列表按整体替换语义处理：只有在库内为空时才写入，避免多源顺序打架。
        return
    for author in getattr(candidate, "authors", ()) or ():
        session.add(
            WorkAuthor(
                work_id=work.id,
                author_name=author.author_name,
                author_order=author.author_order,
                raw_affiliation=getattr(author, "raw_affiliation", None),
            )
        )


async def get_work_authors(
    session: AsyncSession,
    work_id: uuid.UUID,
) -> list[dict[str, Any]]:
    rows = (
        await session.scalars(
            select(WorkAuthor)
            .where(WorkAuthor.work_id == work_id)
            .order_by(WorkAuthor.author_order)
        )
    ).all()
    return [{"author_name": row.author_name, "author_order": row.author_order} for row in rows]


async def get_work_urls(
    session: AsyncSession,
    work_id: uuid.UUID,
) -> list[dict[str, Any]]:
    rows = (await session.scalars(select(WorkUrl).where(WorkUrl.work_id == work_id))).all()
    return [
        {"url": row.url, "url_type": row.url_type, "is_oa": row.is_oa, "source_name": None}
        for row in rows
    ]
