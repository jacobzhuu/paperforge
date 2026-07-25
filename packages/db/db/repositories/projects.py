"""项目仓储（设计 §4.3 paper_project / §4.7 项目 REST）。"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models.library import LibraryEntry
from db.models.paper import PaperDocument, PaperProject, PaperSection

PAPER_TYPES = frozenset({"review", "original"})
WRITING_MODES = frozenset({"auto", "assisted"})
LANGUAGES = frozenset({"zh", "en"})
CITATION_STYLES = frozenset({"author_year", "gbt7714", "ieee", "apa"})
# Draft-first：状态只描述进度，不含「拒绝产出」终态（设计 §1.4）。
PROJECT_STATUSES = (
    "draft",
    "scoping",
    "searching",
    "curating",
    "writing",
    "rendering",
    "review",
    "done",
)


async def create_project(
    session: AsyncSession,
    *,
    title: str,
    paper_type: str,
    writing_mode: str = "auto",
    language: str = "en",
    topic: str | None = None,
    venue_template: str | None = None,
    citation_style: str = "author_year",
    contribution_points: list[str] | None = None,
    owner_id: str | None = None,
) -> PaperProject:
    """创建项目。枚举值非法时抛 ValueError（API 转 422）。"""
    if paper_type not in PAPER_TYPES:
        raise ValueError(f"unsupported paper_type: {paper_type}")
    if writing_mode not in WRITING_MODES:
        raise ValueError(f"unsupported writing_mode: {writing_mode}")
    if language not in LANGUAGES:
        raise ValueError(f"unsupported language: {language}")
    if citation_style not in CITATION_STYLES:
        raise ValueError(f"unsupported citation_style: {citation_style}")
    if not title.strip():
        raise ValueError("project title must not be empty")

    scope_json: dict[str, Any] = {}
    if topic:
        scope_json["topic"] = topic
    if contribution_points:
        scope_json["contribution_points"] = [p for p in contribution_points if p.strip()]

    project = PaperProject(
        title=title.strip(),
        paper_type=paper_type,
        writing_mode=writing_mode,
        language=language,
        venue_template=venue_template,
        citation_style=citation_style,
        status="draft",
        scope_json=scope_json or None,
        owner_id=owner_id,
    )
    session.add(project)
    await session.flush()
    return project


async def get_project(session: AsyncSession, project_id: uuid.UUID) -> PaperProject | None:
    return await session.get(PaperProject, project_id)


async def list_projects(
    session: AsyncSession,
    *,
    owner_id: str | None = None,
    limit: int = 100,
) -> list[PaperProject]:
    stmt = select(PaperProject).order_by(PaperProject.created_at.desc()).limit(limit)
    if owner_id is not None:
        stmt = stmt.where(PaperProject.owner_id == owner_id)
    return list((await session.scalars(stmt)).all())


async def update_project_scope(
    session: AsyncSession,
    project: PaperProject,
    scope: dict[str, Any],
) -> PaperProject:
    """整体替换 scope（可编辑、可随时重生成——设计 §3.2 去掉协议锁定语义）。"""
    project.scope_json = scope
    await session.flush()
    return project


async def set_project_status(
    session: AsyncSession,
    project: PaperProject,
    status: str,
) -> PaperProject:
    if status not in PROJECT_STATUSES:
        raise ValueError(f"unsupported project status: {status}")
    project.status = status
    await session.flush()
    return project


async def project_counters(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> dict[str, int]:
    """项目列表展示用的轻量计数（入库文献数 / 章节数）。"""
    library_count = await session.scalar(
        select(func.count())
        .select_from(LibraryEntry)
        .where(LibraryEntry.project_id == project_id, LibraryEntry.status == "selected")
    )
    section_count = await session.scalar(
        select(func.count())
        .select_from(PaperSection)
        .join(PaperDocument, PaperDocument.id == PaperSection.document_id)
        .where(PaperDocument.project_id == project_id)
    )
    return {
        "library_count": int(library_count or 0),
        "section_count": int(section_count or 0),
    }
