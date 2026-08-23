"""论文结构仓储：outline / paper_document / paper_section / citation_usage（设计 §4.3）。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models.paper import (
    CitationUsage,
    Outline,
    PaperDocument,
    PaperSection,
    SentenceDowngrade,
)

# ``needs_rewrite`` 是「这一节没有正文」——写作降级留下的缺口，不是一份粗糙的初稿。
# 质量门据此产出阻断项，编辑器据此打徽标；两边都必须能和 ``generated`` 区分开。
SECTION_STATUSES = frozenset({"generated", "edited", "approved", "needs_rewrite"})
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
        select(func.coalesce(func.max(Outline.version), 0)).where(Outline.project_id == project_id)
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
    # 正文或 FigureBlock 一旦变化，绑定旧快照的质量报告立即失效。
    from db.repositories.quality import invalidate_quality_reports_for_document

    await invalidate_quality_reports_for_document(session, document_id)
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
    await session.execute(delete(CitationUsage).where(CitationUsage.section_id == section_id))
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
    from db.repositories.quality import invalidate_quality_reports_for_document

    document_id = await session.scalar(
        select(PaperSection.document_id).where(PaperSection.id == section_id)
    )
    if document_id:
        await invalidate_quality_reports_for_document(session, document_id)
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


async def replace_sentence_downgrades(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    job_id: uuid.UUID | None,
    document_id: uuid.UUID,
    section_id: uuid.UUID,
    section_key: str,
    events: list[dict[str, Any]],
) -> int:
    """重写某章节「被规则拿掉的句子」记录。

    与 ``replace_citation_usage`` 同一生命周期：整节替换。修复轮会重写章节，
    上一轮的删除记录必须跟着旧正文一起走，否则计数会随修复轮次不断累加。

    :param events: ``sentence_downgrade_events`` 产出的事件列表。
    :returns: 写入的行数。
    """
    await session.execute(
        delete(SentenceDowngrade).where(SentenceDowngrade.section_id == section_id)
    )
    for event in events:
        session.add(
            SentenceDowngrade(
                project_id=project_id,
                job_id=job_id,
                document_id=document_id,
                section_id=section_id,
                section_key=section_key,
                paragraph_index=int(event["paragraph_index"]),
                sentence_index=int(event["sentence_index"]),
                rule=str(event["rule"])[:48],
                outcome=str(event.get("outcome") or "removed")[:16],
                text=str(event.get("text") or ""),
                cite_keys_json=list(event.get("cite_keys") or []),
                evidence_ids_json=list(event.get("evidence_ids") or []),
                locator_status=str(event.get("locator_status") or "unknown")[:24],
                locators_json=list(event.get("locators") or []),
            )
        )
    await session.flush()
    return len(events)


async def sentence_downgrade_summary(
    session: AsyncSession, *, document_id: uuid.UUID
) -> dict[str, Any]:
    """整篇 + 每节的删除计数与原因分布（质检报告的数据源）。

    :returns: ``{"total", "removed", "rewritten", "by_rule", "by_locator_status",
        "by_section": [{"section_key", "total", "by_rule"}]}``。
    """
    rows = (
        await session.execute(
            select(
                SentenceDowngrade.section_key,
                SentenceDowngrade.rule,
                SentenceDowngrade.outcome,
                SentenceDowngrade.locator_status,
                func.count().label("n"),
            )
            .where(SentenceDowngrade.document_id == document_id)
            .group_by(
                SentenceDowngrade.section_key,
                SentenceDowngrade.rule,
                SentenceDowngrade.outcome,
                SentenceDowngrade.locator_status,
            )
        )
    ).all()

    by_rule: dict[str, int] = {}
    by_locator: dict[str, int] = {}
    by_section: dict[str, dict[str, Any]] = {}
    total = removed = rewritten = 0
    for section_key, rule, outcome, locator_status, count in rows:
        total += count
        if outcome == "rewritten":
            rewritten += count
        else:
            removed += count
        by_rule[rule] = by_rule.get(rule, 0) + count
        by_locator[locator_status] = by_locator.get(locator_status, 0) + count
        entry = by_section.setdefault(
            section_key, {"section_key": section_key, "total": 0, "by_rule": {}}
        )
        entry["total"] += count
        entry["by_rule"][rule] = entry["by_rule"].get(rule, 0) + count
    return {
        "total": total,
        "removed": removed,
        "rewritten": rewritten,
        "by_rule": dict(sorted(by_rule.items(), key=lambda kv: -kv[1])),
        "by_locator_status": dict(sorted(by_locator.items(), key=lambda kv: -kv[1])),
        "by_section": sorted(by_section.values(), key=lambda item: -item["total"]),
    }


async def list_sentence_downgrades(
    session: AsyncSession, *, document_id: uuid.UUID, limit: int = 200
) -> list[SentenceDowngrade]:
    """逐条明细，供审计页与排查使用。"""
    return list(
        (
            await session.execute(
                select(SentenceDowngrade)
                .where(SentenceDowngrade.document_id == document_id)
                .order_by(SentenceDowngrade.section_key, SentenceDowngrade.paragraph_index,
                          SentenceDowngrade.sentence_index)
                .limit(limit)
            )
        ).scalars()
    )
