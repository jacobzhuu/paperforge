"""项目仓储（设计 §4.3 paper_project / §4.7 项目 REST）。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from db.models.library import LibraryEntry
from db.models.paper import (
    ClaimEntailmentCache,
    ClaimEvidenceAnchor,
    PaperDocument,
    PaperProject,
    PaperSection,
)

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
    publication_title: str | None = None,
    authors: list[str] | None = None,
    author_details: list[dict[str, Any]] | None = None,
    keywords: list[str] | None = None,
    owner_id: uuid.UUID,
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
        publication_title=(publication_title or "").strip() or None,
        authors_json=(
            [
                str(item.get("name") or "").strip()
                for item in (author_details or [])
                if str(item.get("name") or "").strip()
            ]
            if author_details is not None
            else [item.strip() for item in (authors or []) if item.strip()]
        )
        or None,
        author_details_json=_clean_author_details(author_details),
        keywords_json=[item.strip() for item in (keywords or []) if item.strip()] or None,
    )
    session.add(project)
    await session.flush()
    return project


async def get_project(session: AsyncSession, project_id: uuid.UUID) -> PaperProject | None:
    return await session.get(PaperProject, project_id)


async def apply_generated_publication_metadata(
    session: AsyncSession,
    project: PaperProject,
    *,
    title: str,
    keywords: list[str],
) -> tuple[bool, bool]:
    """只补齐空白投稿字段，绝不覆盖用户已经编辑过的题名或关键词。"""
    title_added = False
    keywords_added = False
    if not (project.publication_title or "").strip() and title.strip():
        project.publication_title = title.strip()
        title_added = True
    if not (project.keywords_json or []):
        cleaned = [item.strip() for item in keywords if item.strip()]
        if cleaned:
            project.keywords_json = list(dict.fromkeys(cleaned))
            keywords_added = True
    if title_added or keywords_added:
        project.metadata_confirmed_at = None
        from db.repositories.quality import invalidate_quality_reports_for_project

        await invalidate_quality_reports_for_project(session, project.id)
        await session.flush()
    return title_added, keywords_added


async def get_owned_project(
    session: AsyncSession,
    project_id: uuid.UUID,
    owner_id: uuid.UUID,
    *,
    include_deleted: bool = False,
) -> PaperProject | None:
    """按 owner 取项目。

    **软删除的收口点**：API 的 `authorize_project_request` 是 router 级依赖，
    所有 project 作用域路由都经过它，所以在这里过滤一次，等于删除后整个项目的
    每一个端点自动 404。只有「恢复」「彻底删除」这类必须看见墓碑的路径才传
    ``include_deleted=True``。
    """
    stmt = select(PaperProject).where(
        PaperProject.id == project_id,
        PaperProject.owner_id == owner_id,
    )
    if not include_deleted:
        stmt = stmt.where(PaperProject.deleted_at.is_(None))
    return await session.scalar(stmt)


async def list_projects(
    session: AsyncSession,
    *,
    owner_id: uuid.UUID,
    limit: int = 100,
    deleted: bool = False,
) -> list[PaperProject]:
    """项目列表。``deleted=True`` 取回收站（只列已软删除的）。"""
    stmt = select(PaperProject).where(PaperProject.owner_id == owner_id)
    stmt = stmt.where(
        PaperProject.deleted_at.is_not(None) if deleted else PaperProject.deleted_at.is_(None)
    )
    # 回收站按删除时间排：用户找的是「刚才误删的那个」。
    order = PaperProject.deleted_at.desc() if deleted else PaperProject.created_at.desc()
    return list((await session.scalars(stmt.order_by(order).limit(limit))).all())


async def soft_delete_project(session: AsyncSession, project: PaperProject) -> PaperProject:
    """软删除：项目立刻从列表与所有路由上消失，产物一件不动。幂等。"""
    if project.deleted_at is None:
        project.deleted_at = datetime.now(UTC)
        await session.flush()
        await _refresh_server_defaults(session, project)
    return project


async def restore_project(session: AsyncSession, project: PaperProject) -> PaperProject:
    """从回收站恢复。幂等。"""
    if project.deleted_at is not None:
        project.deleted_at = None
        await session.flush()
        await _refresh_server_defaults(session, project)
    return project


async def _refresh_server_defaults(session: AsyncSession, project: PaperProject) -> None:
    """把 flush 之后失效的服务端列读回来。

    `updated_at` 是 `onupdate=func.now()`，值由数据库算，flush 发出 UPDATE 之后这一列
    就被标记为过期。之后第一次读它会触发一次隐式 SELECT——而读它的
    `_project_response` 是个同步函数，同步上下文里发不出 IO，于是抛
    `MissingGreenlet`，恢复接口直接 500：回收站里的「恢复」按钮点了就报错。
    在这里用异步的方式读回来，同步的响应构造就不会再碰 IO。
    """
    await session.refresh(project)


async def list_purgeable_projects(
    session: AsyncSession,
    *,
    before: datetime,
    limit: int = 500,
) -> list[PaperProject]:
    """保留期已过、可以真删的项目。供 `paperforge-admin purge-projects` 使用。"""
    return list(
        (
            await session.scalars(
                select(PaperProject)
                .where(
                    PaperProject.deleted_at.is_not(None),
                    PaperProject.deleted_at < before,
                )
                .order_by(PaperProject.deleted_at)
                .limit(limit)
            )
        ).all()
    )


async def purge_project(session: AsyncSession, project: PaperProject) -> None:
    """真删行。所有 project 关联表都是 ON DELETE CASCADE（含 0008 补的成本台账）。

    对象存储不在事务里，调用方必须**先**删对象再调它——反过来的话一旦删行成功、
    删对象失败，object_key 就再也查不出来了，存储里留下永远没人认领的垃圾。

    ``claim_entailment_cache`` 是唯一的例外：它故意不带 project_id（一条判定要能服务所有
    项目），因此 CASCADE 够不到它，而它的 ``reason`` 是模型写的、会复述稿件论断的散文。
    删项目前先清掉只属于该项目的缓存行。
    """
    await _purge_orphaned_claim_entailment_cache(session, project.id)
    await session.delete(project)
    await session.flush()


async def _purge_orphaned_claim_entailment_cache(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> int:
    """Delete cached verdicts that no surviving project's claims would reach.

    Scoped by ``(claim_hash, evidence_hash)`` rather than by project, because that is the only
    linkage the cache has.  A pair still anchored by another project is kept: the cache is shared
    by design, and the identical claim/excerpt text is already present in that other project.
    """
    owned = select(ClaimEvidenceAnchor.claim_hash, ClaimEvidenceAnchor.evidence_hash).where(
        ClaimEvidenceAnchor.project_id == project_id,
        # Cache rows always carry both hashes; an anchor without one can never match a cache row,
        # and letting a NULL into the tuple subquery would only muddy the comparison.
        ClaimEvidenceAnchor.evidence_hash.is_not(None),
    )
    # NOT EXISTS, not NOT IN: ``evidence_hash`` is nullable.  Postgres row-constructor comparison
    # short-circuits to FALSE when any element differs, so a NULL poisons the predicate only when
    # the other element matches -- i.e. when a surviving project holds an anchor for the *same*
    # claim whose excerpt was never located.  That is reachable, and under ``NOT IN`` it makes the
    # predicate UNKNOWN for every row, silently deleting nothing.  Guarded by
    # test_a_null_evidence_hash_elsewhere_cannot_block_the_purge.
    still_anchored = (
        select(1)
        .where(
            ClaimEvidenceAnchor.project_id != project_id,
            ClaimEvidenceAnchor.claim_hash == ClaimEntailmentCache.claim_hash,
            ClaimEvidenceAnchor.evidence_hash == ClaimEntailmentCache.evidence_hash,
        )
        .exists()
    )
    result = await session.execute(
        delete(ClaimEntailmentCache).where(
            tuple_(ClaimEntailmentCache.claim_hash, ClaimEntailmentCache.evidence_hash).in_(owned),
            ~still_anchored,
        )
    )
    return int(result.rowcount or 0)


#: `update_project` 里「不传就是不改」的哨兵。
#: 不能用 None 当哨兵——`topic=None` 是合法的「清空主题」，与「别动主题」不是一回事。
_UNSET: Any = object()


async def update_project(
    session: AsyncSession,
    project: PaperProject,
    *,
    title: str | None = _UNSET,
    topic: str | None = _UNSET,
    venue_template: str | None = _UNSET,
    language: str | None = _UNSET,
    citation_style: str | None = _UNSET,
    writing_mode: str | None = _UNSET,
    contribution_points: list[str] | None = _UNSET,
    publication_title: str | None = _UNSET,
    authors: list[str] | None = _UNSET,
    author_details: list[dict[str, Any]] | None = _UNSET,
    keywords: list[str] | None = _UNSET,
    metadata_confirmed: bool | None = _UNSET,
) -> PaperProject:
    """
    局部更新项目元数据。枚举值非法时抛 ValueError（API 转 422）。

    **不允许改 `paper_type`**：论文类型决定管线形状（`apps/web/lib/pipeline.ts` 的
    REVIEW_FLOW / ORIGINAL_FLOW）、大纲结构（IMRaD vs 主题章）与是否做数字一致性
    lint。项目一旦有了文献、大纲或正文，中途换类型只会得到一份自相矛盾的稿子——
    那是「新建一个项目」，不是「改一个字段」。

    topic / contribution_points 落在 `scope_json` 里而不是独立列，与 `create_project`
    保持一致；这里做的是**合并**而非整体替换，避免把 SCOPE 生成出来的关键词矩阵
    连带清空（那是 `update_project_scope` 的职责）。
    """
    # title 与 topic 不同：topic=None 是「清空主题」，title=None 是非法的——
    # 论文永远得有个题目。两者都会走到这里，所以必须分别判。
    if title is not _UNSET:
        if title is None or not title.strip():
            raise ValueError("project title must not be empty")
        project.title = title.strip()
    if language is not _UNSET:
        if language not in LANGUAGES:
            raise ValueError(f"unsupported language: {language}")
        project.language = language
    if citation_style is not _UNSET:
        if citation_style not in CITATION_STYLES:
            raise ValueError(f"unsupported citation_style: {citation_style}")
        project.citation_style = citation_style
    if writing_mode is not _UNSET:
        if writing_mode not in WRITING_MODES:
            raise ValueError(f"unsupported writing_mode: {writing_mode}")
        project.writing_mode = writing_mode
    if venue_template is not _UNSET:
        project.venue_template = venue_template
    publication_metadata_changed = any(
        value is not _UNSET for value in (publication_title, authors, author_details, keywords)
    )
    if publication_title is not _UNSET:
        project.publication_title = (
            publication_title.strip() if isinstance(publication_title, str) else None
        ) or None
    if authors is not _UNSET:
        project.authors_json = [item.strip() for item in (authors or []) if item.strip()] or None
        project.author_details_json = None
    if author_details is not _UNSET:
        cleaned = _clean_author_details(author_details)
        project.author_details_json = cleaned
        project.authors_json = [item["name"] for item in (cleaned or [])] or None
    if keywords is not _UNSET:
        project.keywords_json = [item.strip() for item in (keywords or []) if item.strip()] or None
    if metadata_confirmed is not _UNSET:
        project.metadata_confirmed_at = datetime.now(UTC) if metadata_confirmed else None
    elif publication_metadata_changed:
        # A previously confirmed title/byline must not remain confirmed after it changes.
        project.metadata_confirmed_at = None

    if publication_metadata_changed or metadata_confirmed is not _UNSET:
        from db.repositories.quality import invalidate_quality_reports_for_project

        await invalidate_quality_reports_for_project(session, project.id)

    if topic is not _UNSET or contribution_points is not _UNSET:
        scope = dict(project.scope_json or {})
        if topic is not _UNSET:
            if topic and topic.strip():
                scope["topic"] = topic.strip()
            else:
                scope.pop("topic", None)
        if contribution_points is not _UNSET:
            points = [p.strip() for p in (contribution_points or []) if p.strip()]
            if points:
                scope["contribution_points"] = points
            else:
                scope.pop("contribution_points", None)
        # 赋新 dict 而不是原地改：scope_json 是 JSON 列，原地 mutate SQLAlchemy 检测不到。
        project.scope_json = scope or None

    await session.flush()
    # 必须 refresh 而不是只 flush。
    #
    # `TimestampMixin.updated_at` 带 `onupdate=func.now()`——这是**服务端**求值的，
    # 所以 UPDATE 落库后 SQLAlchemy 会把该属性标记为 expired 等待回读。之后任何一次
    # `project.updated_at` 都是**同步**属性访问触发的隐式 IO，在 asyncpg 下直接
    # 抛 MissingGreenlet（实测：PATCH 端点构造响应时必炸）。
    # INSERT 路径没这个问题（服务端默认值走 RETURNING 一次取回），所以
    # `create_project` 不需要这一步。
    await session.refresh(project)
    return project


async def update_project_scope(
    session: AsyncSession,
    project: PaperProject,
    scope: dict[str, Any],
) -> PaperProject:
    """整体替换 scope（可编辑、可随时重生成——设计 §3.2 去掉协议锁定语义）。"""
    project.scope_json = scope
    await session.flush()
    return project


def _clean_author_details(values: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    if not values:
        return None
    cleaned: list[dict[str, Any]] = []
    for index, raw in enumerate(values):
        name = " ".join(str(raw.get("name") or "").split())
        if not name:
            continue
        affiliations = []
        seen: set[str] = set()
        for value in raw.get("affiliations") or []:
            item = " ".join(str(value).split())[:240]
            key = item.casefold()
            if item and key not in seen:
                seen.add(key)
                affiliations.append(item)
        cleaned.append(
            {
                "id": str(raw.get("id") or f"author-{index + 1}"),
                "name": name,
                "affiliations": affiliations,
                "email": str(raw.get("email")) if raw.get("email") else None,
                "orcid": str(raw.get("orcid")) if raw.get("orcid") else None,
                "corresponding": bool(raw.get("corresponding", False)),
            }
        )
    return cleaned or None


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
