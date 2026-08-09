"""QDECOMP 阶段：把 scope 中的问题分解树持久化。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from db import (
    infer_task_id,
    list_task_definitions,
    replace_research_questions,
)

from paperforge_worker.context import JobContext
from paperforge_worker.pipelines.scope import deterministic_scope, normalize_scope


@dataclass(frozen=True)
class QuestionDecompositionOutcome:
    core_question: str
    sub_question_count: int
    generator: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "core_question": self.core_question,
            "sub_question_count": self.sub_question_count,
            "generator": self.generator,
        }


async def persist_question_decomposition(
    context: JobContext,
    *,
    scope: dict[str, Any],
    topic: str,
    language: str,
) -> QuestionDecompositionOutcome:
    """兼容旧 scope：缺少 sub_questions 时从 subtopics 确定性升格。"""
    if not scope.get("sub_questions"):
        fallback = deterministic_scope(topic, language=language)
        raw = {**fallback, **scope}
        # normalize_scope 会保留旧 scope 的检索字段，并补齐严格问题 schema。
        scope = normalize_scope(raw, topic=topic, language=language)
    core = str(scope.get("research_question") or "").strip()
    sub_questions = [
        item
        for item in scope.get("sub_questions") or []
        if isinstance(item, dict) and str(item.get("text") or "").strip()
    ]
    async with context.session() as session:
        tasks = await list_task_definitions(session)
        known_slugs = {task.slug for task in tasks}
        enriched: list[dict[str, Any]] = []
        for item in sub_questions:
            text = str(item["text"])
            explicit = str(item.get("task_id") or "").strip()
            task_id = explicit if explicit in known_slugs else (infer_task_id(text, tasks) or "")
            search_query = str(item.get("search_query") or item.get("english_query") or "").strip()
            enriched.append(
                {
                    **item,
                    "task_id": task_id,
                    "search_query": search_query or None,
                    "term_aliases": item.get("term_aliases") or item.get("term_aliases_json"),
                }
            )
        generator = str(scope.get("generator") or "deterministic")
        core_task = infer_task_id(core, tasks)
        await replace_research_questions(
            session,
            project_id=context.project_id,
            core_text=core,
            sub_questions=enriched,
            generator=generator,
            core_task_id=core_task,
        )
        await _bind_inferred_tasks(
            session,
            project_id=context.project_id,
            task_ids=[item["task_id"] for item in enriched if item["task_id"]],
        )
    return QuestionDecompositionOutcome(
        core_question=core,
        sub_question_count=len(enriched),
        generator=generator,
    )


#: 与 API 侧 ``projects._set_user_bound_marker`` 同一个键。用户亲手定过任务集之后，
#: 自动推断不得再覆盖它——与 SCOPE 的 ``generator='user'`` 豁免、研究问题的
#: ``locked`` 是同一套约定。
TASK_PROFILE_USER_BOUND_KEY = "task_profile_user_bound"


async def _bind_inferred_tasks(
    session: Any,
    *,
    project_id: Any,
    task_ids: list[str],
) -> None:
    """把推断出的任务写进绑定表——但绝不破坏已有的绑定。

    此前这里无条件调用 ``replace_project_task_profile``，它先删后插。于是有两个问题：

    * 用户显式绑定过任务，下一次 QDECOMP 会把它悄悄换掉；
    * 更常见的是本轮**一个任务都没推断出来**（``infer_task_id`` 只在本体线索命中时
      才返回结果，生产上 52 条子问题里只有 1 条命中），于是删完不插，把上一轮
      成功推断出来的绑定也一起抹掉。

    现在：有用户标记就完全不碰；没推断出东西就保持原样；只有真的推断出任务、
    且不是用户绑定时才替换。
    """
    from db import get_project, list_project_task_bindings, replace_project_task_profile

    project = await get_project(session, project_id)
    if project is not None and (project.scope_json or {}).get(TASK_PROFILE_USER_BOUND_KEY):
        return
    if not task_ids:
        return
    existing = await list_project_task_bindings(session, project_id)
    if [row.task_id for row in existing] == list(dict.fromkeys(task_ids)):
        return
    await replace_project_task_profile(session, project_id=project_id, task_ids=task_ids)


__all__ = [
    "TASK_PROFILE_USER_BOUND_KEY",
    "QuestionDecompositionOutcome",
    "persist_question_decomposition",
]
