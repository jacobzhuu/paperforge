"""研究问题树与问题—证据矩阵仓储。"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models.paper import ProjectTaskProfile, QuestionEvidenceLink, ResearchQuestion

QUESTION_KINDS = frozenset({"core", "sub"})
ANSWER_STATUSES = frozenset({"answered", "partial", "contested", "insufficient_evidence"})
EVIDENCE_STANCES = frozenset({"supports", "contradicts", "conditional", "not_comparable", "gap"})


async def replace_research_questions(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    core_text: str,
    sub_questions: list[dict[str, Any]],
    generator: str | None = None,
    core_task_id: str | None = None,
) -> list[ResearchQuestion]:
    """按位置归并生成的问题树，既不丢用户编辑，也不静默删除人工矩阵判断。

    行为要点：

    * **锁定行不可被重生成覆盖。** ``locked=True`` 的问题占住自己的位置，对应
      位置上新生成的问题被丢弃。以前这里是整棵树硬删重建，用户在问题工作台
      做的每一次编辑都会在下一次 QDECOMP 时消失。
    * **ID 始终稳定**，因此 ``question_evidence_link`` / 大纲 ``question_id``
      不会因为重生成而级联失效；只有文本真的变了的那几行才清掉自动链接并把
      ``answer_status`` 打回 ``insufficient_evidence``。
    * 若被改写或被删除的行上存在人工覆盖的证据链接，调用方必须先显式协调，
      不能借重生成级联删除。
    """
    existing = list(
        (
            await session.scalars(
                select(ResearchQuestion)
                .where(ResearchQuestion.project_id == project_id)
                .order_by(ResearchQuestion.order_index, ResearchQuestion.id)
            )
        ).all()
    )
    existing_core = next((row for row in existing if row.kind == "core"), None)
    existing_subs = [row for row in existing if row.kind == "sub"]
    incoming = [item for item in sub_questions if str(item.get("text") or "").strip()]

    # ---- 1. 先只做计划，不改任何行：人工覆盖检查必须在写入之前。
    core_changed = (
        existing_core is not None
        and not existing_core.locked
        and _normalized_text(existing_core.text) != _normalized_text(core_text)
    )
    plan: list[tuple[str, ResearchQuestion | None, dict[str, Any] | None]] = []
    retext_ids: list[uuid.UUID] = [existing_core.id] if core_changed and existing_core else []
    surplus_ids: list[uuid.UUID] = []
    for index in range(max(len(existing_subs), len(incoming))):
        row = existing_subs[index] if index < len(existing_subs) else None
        item = incoming[index] if index < len(incoming) else None
        if row is not None and row.locked:
            # 用户锁定：保留原问题，丢弃这个位置上自动生成的替代问题。
            plan.append(("keep", row, None))
        elif row is None:
            plan.append(("create", None, item))
        elif item is None:
            plan.append(("delete", row, None))
            surplus_ids.append(row.id)
        else:
            if _normalized_text(row.text) != _normalized_text(item.get("text")):
                retext_ids.append(row.id)
            plan.append(("update", row, item))

    impacted_ids = [*retext_ids, *surplus_ids]
    if impacted_ids:
        manual_link = await session.scalar(
            select(QuestionEvidenceLink.id)
            .where(
                QuestionEvidenceLink.research_question_id.in_(impacted_ids),
                QuestionEvidenceLink.manually_overridden.is_(True),
            )
            .limit(1)
        )
        if manual_link is not None:
            raise ValueError("question_decomposition_conflicts_with_manual_evidence_links")

    # ---- 2. 应用计划。
    if existing_core is None:
        core = ResearchQuestion(
            project_id=project_id,
            text=core_text.strip(),
            kind="core",
            order_index=0,
            comparison_dimensions_json=[],
            expected_evidence_kinds_json=[],
            answer_status="insufficient_evidence",
            generator=generator,
            origin="auto",
            locked=False,
            task_id=core_task_id,
        )
        session.add(core)
        await session.flush()
    else:
        core = existing_core
        if not core.locked:
            core.text = core_text.strip()
            core.generator = generator
            core.task_id = core_task_id
            core.parent_id = None
            core.kind = "core"
            core.order_index = 0

    rows = [core]
    for index, (action, row, item) in enumerate(plan, start=1):
        if action == "delete":
            continue
        if action == "keep" and row is not None:
            rows.append(row)
            continue
        if item is None:
            continue
        target = row
        if target is None:
            target = ResearchQuestion(
                project_id=project_id,
                answer_status="insufficient_evidence",
                origin="auto",
                locked=False,
            )
            session.add(target)
        _update_sub_question(target, item, core_id=core.id, order_index=index, generator=generator)
        rows.append(target)

    if surplus_ids:
        await session.execute(delete(ResearchQuestion).where(ResearchQuestion.id.in_(surplus_ids)))
    if retext_ids:
        # 问题文本变了，旧的自动链接与结论状态就不再成立。
        await clear_automatic_question_evidence_links(session, retext_ids)
        retext_id_set = set(retext_ids)
        for row in rows:
            if row.id in retext_id_set:
                row.answer_status = "insufficient_evidence"
    await session.flush()
    return rows


async def list_research_questions(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    kind: str | None = None,
) -> list[ResearchQuestion]:
    stmt = (
        select(ResearchQuestion)
        .where(ResearchQuestion.project_id == project_id)
        .order_by(ResearchQuestion.order_index, ResearchQuestion.id)
    )
    if kind is not None:
        if kind not in QUESTION_KINDS:
            raise ValueError(f"unsupported question kind: {kind}")
        stmt = stmt.where(ResearchQuestion.kind == kind)
    return list((await session.scalars(stmt)).all())


async def replace_project_task_profile(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    task_ids: list[str],
) -> None:
    """Persist the project task closure used by task-safe evidence routing."""
    await session.execute(
        delete(ProjectTaskProfile).where(ProjectTaskProfile.project_id == project_id)
    )
    for index, task_id in enumerate(dict.fromkeys(task_ids)):
        session.add(
            ProjectTaskProfile(
                project_id=project_id,
                task_id=task_id,
                is_core=True,
                order_index=index,
            )
        )
    await session.flush()


async def set_question_answer_status(
    session: AsyncSession,
    question: ResearchQuestion,
    status: str,
) -> ResearchQuestion:
    if status not in ANSWER_STATUSES:
        raise ValueError(f"unsupported answer status: {status}")
    question.answer_status = status
    await session.flush()
    return question


async def upsert_question_evidence_link(
    session: AsyncSession,
    *,
    research_question_id: uuid.UUID,
    evidence_unit_id: uuid.UUID,
    stance: str,
    condition_note: str | None = None,
    confidence: float | None = None,
    manually_overridden: bool = False,
    routing_mode: str | None = None,
    bridge_source: str | None = None,
) -> QuestionEvidenceLink:
    if stance not in EVIDENCE_STANCES:
        raise ValueError(f"unsupported evidence stance: {stance}")
    link = await session.scalar(
        select(QuestionEvidenceLink)
        .where(
            QuestionEvidenceLink.research_question_id == research_question_id,
            QuestionEvidenceLink.evidence_unit_id == evidence_unit_id,
        )
        .limit(1)
    )
    if link is None:
        link = QuestionEvidenceLink(
            research_question_id=research_question_id,
            evidence_unit_id=evidence_unit_id,
            stance=stance,
        )
        session.add(link)
    # 人工覆盖后，自动重跑不得覆盖 stance。
    if not link.manually_overridden or manually_overridden:
        link.stance = stance
        link.condition_note = condition_note
        link.confidence = confidence
        link.manually_overridden = manually_overridden
        if routing_mode is not None:
            link.routing_mode = routing_mode
        if bridge_source is not None:
            link.bridge_source = bridge_source
    await session.flush()
    return link


async def list_question_evidence_links(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> list[QuestionEvidenceLink]:
    return list(
        (
            await session.scalars(
                select(QuestionEvidenceLink)
                .join(
                    ResearchQuestion,
                    ResearchQuestion.id == QuestionEvidenceLink.research_question_id,
                )
                .where(ResearchQuestion.project_id == project_id)
                .order_by(
                    ResearchQuestion.order_index,
                    QuestionEvidenceLink.confidence.desc().nullslast(),
                )
            )
        ).all()
    )


async def clear_automatic_question_evidence_links(
    session: AsyncSession,
    research_question_ids: list[uuid.UUID],
) -> int:
    if not research_question_ids:
        return 0
    result = await session.execute(
        delete(QuestionEvidenceLink).where(
            QuestionEvidenceLink.research_question_id.in_(research_question_ids),
            QuestionEvidenceLink.manually_overridden.is_(False),
        )
    )
    return int(getattr(result, "rowcount", 0) or 0)


def _clean_strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(
        dict.fromkeys(cleaned for item in value if (cleaned := " ".join(str(item).split())))
    )


def _normalized_text(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def _update_sub_question(
    row: ResearchQuestion,
    item: dict[str, Any],
    *,
    core_id: uuid.UUID,
    order_index: int,
    generator: str | None,
) -> None:
    row.parent_id = core_id
    row.text = str(item.get("text") or "").strip()
    row.kind = "sub"
    row.order_index = order_index
    # 只有未锁定的行才会走到这里，文本此刻由生成器写入，来源随之回到 auto。
    row.origin = "auto"
    row.comparison_dimensions_json = _clean_strings(item.get("comparison_dimensions"))
    row.expected_evidence_kinds_json = _clean_strings(item.get("expected_evidence_kinds"))
    row.generator = generator
    row.task_id = str(item.get("task_id") or "").strip() or None
    row.search_query = (
        str(item.get("search_query") or item.get("english_query") or "").strip() or None
    )
    row.term_aliases_json = item.get("term_aliases") or item.get("term_aliases_json")


__all__ = [
    "ANSWER_STATUSES",
    "EVIDENCE_STANCES",
    "QUESTION_KINDS",
    "clear_automatic_question_evidence_links",
    "list_question_evidence_links",
    "list_research_questions",
    "replace_project_task_profile",
    "replace_research_questions",
    "set_question_answer_status",
    "upsert_question_evidence_link",
]
