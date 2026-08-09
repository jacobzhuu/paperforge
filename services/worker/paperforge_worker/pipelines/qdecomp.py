"""QDECOMP 阶段：把 scope 中的问题分解树持久化。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from db import (
    infer_task_id,
    list_task_definitions,
    replace_project_task_profile,
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
        await replace_project_task_profile(
            session,
            project_id=context.project_id,
            task_ids=[item["task_id"] for item in enriched if item["task_id"]],
        )
    return QuestionDecompositionOutcome(
        core_question=core,
        sub_question_count=len(enriched),
        generator=generator,
    )


__all__ = ["QuestionDecompositionOutcome", "persist_question_decomposition"]
