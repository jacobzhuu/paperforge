"""论文结构仓储：outline / paper_document / paper_section / citation_usage（设计 §4.3）。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models.paper import CitationUsage, Outline, PaperDocument, PaperSection

SECTION_STATUSES = frozenset({"generated", "edited", "approved"})
OUTLINE_STATUSES = frozenset({"draft", "confirmed"})


async def create_outline(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    tree: dict[str, Any],
    status: str = "draft",
) -> Outline:
    """每次生成都建新版本——大纲是可回溯的，不是就地覆盖。"""
    if status not in OUTLINE_STATUSES:
        raise ValueError(f"unsupported outline status: {status}")
    current = await session.scalar(
        select(func.coalesce(func.max(Outline.version), 0)).where(
            Outline.project_id == project_id
        )
    )
    outline = Outline(
        project_id=project_id,
        version=int(current or 0) + 1,
        tree_json=tree,
        status=status,
    )
    session.add(outline)
    await session.flush()
    return outline


async def latest_outline(session: AsyncSession, project_id: uuid.UUID) -> Outline | None:
    return await session.scalar(
        select(Outline)
        .where(Outline.project_id == project_id)
        .order_by(Outline.version.desc())
        .limit(1)
    )


async def get_outline(session: AsyncSession, outline_id: uuid.UUID) -> Outline | None:
    return await session.get(Outline, outline_id)


async def update_outline_tree(
    session: AsyncSession,
    outline: Outline,
    tree: dict[str, Any],
    *,
    status: str | None = None,
) -> Outline:
    outline.tree_json = tree
    if status is not None:
        if status not in OUTLINE_STATUSES:
            raise ValueError(f"unsupported outline status: {status}")
        outline.status = status
    await session.flush()
    return outline


async def create_document(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    outline_id: uuid.UUID | None,
    status: str = "draft",
) -> PaperDocument:
    current = await session.scalar(
        select(func.coalesce(func.max(PaperDocument.version), 0)).where(
            PaperDocument.project_id == project_id
        )
    )
    document = PaperDocument(
        project_id=project_id,
        version=int(current or 0) + 1,
        outline_id=outline_id,
        status=status,
    )
    session.add(document)
    await session.flush()
    return document


async def latest_document(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> PaperDocument | None:
    return await session.scalar(
        select(PaperDocument)
        .where(PaperDocument.project_id == project_id)
        .order_by(PaperDocument.version.desc())
        .limit(1)
    )


async def upsert_section(
    session: AsyncSession,
    *,
    document_id: uuid.UUID,
    section_key: str,
    title: str,
    order_no: int,
    body_ir: dict[str, Any] | None,
    cite_keys: list[str] | None,
    parent_key: str | None = None,
    asset_refs: list[str] | None = None,
    status: str = "generated",
    model: str | None = None,
) -> PaperSection:
    if status not in SECTION_STATUSES:
        raise ValueError(f"unsupported section status: {status}")
    section = await session.scalar(
        select(PaperSection).where(
            PaperSection.document_id == document_id,
            PaperSection.section_key == section_key,
        )
    )
    if section is None:
        section = PaperSection(document_id=document_id, section_key=section_key)
        session.add(section)
    section.title = title
    section.parent_key = parent_key
    section.order_no = order_no
    section.body_ir_json = body_ir
    section.cite_keys_json = cite_keys or []
    section.asset_refs_json = asset_refs or []
    section.status = status
    section.model = model
    section.updated_at = datetime.now(UTC)
    await session.flush()
    return section


async def list_sections(
    session: AsyncSession,
    document_id: uuid.UUID,
) -> list[PaperSection]:
    return list(
        (
            await session.scalars(
                select(PaperSection)
                .where(PaperSection.document_id == document_id)
                .order_by(PaperSection.order_no, PaperSection.section_key)
            )
        ).all()
    )


async def get_section(
    session: AsyncSession,
    *,
    document_id: uuid.UUID,
    section_key: str,
) -> PaperSection | None:
    return await session.scalar(
        select(PaperSection).where(
            PaperSection.document_id == document_id,
            PaperSection.section_key == section_key,
        )
    )


async def replace_citation_usage(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    section_id: uuid.UUID,
    usages: list[dict[str, Any]],
) -> int:
    """重写某章节的引用使用记录（引用审计页的数据源）。"""
    await session.execute(
        delete(CitationUsage).where(CitationUsage.section_id == section_id)
    )
    for usage in usages:
        session.add(
            CitationUsage(
                project_id=project_id,
                section_id=section_id,
                work_id=usage["work_id"],
                cite_key=usage["cite_key"],
                context_snippet=usage.get("context_snippet"),
            )
        )
    await session.flush()
    return len(usages)


async def list_citation_usage(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> list[CitationUsage]:
    return list(
        (
            await session.scalars(
                select(CitationUsage)
                .where(CitationUsage.project_id == project_id)
                .order_by(CitationUsage.created_at)
            )
        ).all()
    )
