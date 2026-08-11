"""子问题叙述性综合（P0-4 / Phase 4）仓储。

写入永远带 ``bundle_hash``，因此「同一个 bundle 再综合一次」是查表而不是再花一次
LLM 调用。bundle 变了就写新行，旧行留作历史，不做删除。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models.paper import QuestionSynthesis

DETERMINISTIC_GENERATOR = "deterministic"


async def list_question_syntheses(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    bundle_hashes: Sequence[str] = (),
) -> list[QuestionSynthesis]:
    """按 bundle 指纹取回已有综合。

    ``bundle_hashes`` 为空时返回空列表而不是全表：调用方要的永远是「当前这批 bundle
    对应的行」，把历史行一起读回来只会让匹配变得含糊。
    """
    hashes = [value for value in dict.fromkeys(bundle_hashes) if value]
    if not hashes:
        return []
    result = await session.execute(
        select(QuestionSynthesis)
        .where(QuestionSynthesis.project_id == project_id)
        .where(QuestionSynthesis.bundle_hash.in_(hashes))
        .order_by(QuestionSynthesis.created_at)
    )
    return list(result.scalars().all())


async def upsert_question_synthesis(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    research_question_id: uuid.UUID,
    bundle_hash: str,
    generator: str,
    claim: str | None = None,
    agreement: list[dict[str, Any]] | None = None,
    conditional: list[dict[str, Any]] | None = None,
    conflict: list[dict[str, Any]] | None = None,
    gap: list[dict[str, Any]] | None = None,
) -> QuestionSynthesis:
    """按 (research_question_id, bundle_hash) 归并写入。"""
    existing = await session.execute(
        select(QuestionSynthesis)
        .where(QuestionSynthesis.research_question_id == research_question_id)
        .where(QuestionSynthesis.bundle_hash == bundle_hash)
    )
    row = existing.scalars().first()
    if row is None:
        row = QuestionSynthesis(
            project_id=project_id,
            research_question_id=research_question_id,
            bundle_hash=bundle_hash,
        )
        session.add(row)
    row.generator = generator
    row.claim = claim
    row.agreement_json = list(agreement or [])
    row.conditional_json = list(conditional or [])
    row.conflict_json = list(conflict or [])
    row.gap_json = list(gap or [])
    await session.flush()
    return row


__all__ = [
    "DETERMINISTIC_GENERATOR",
    "list_question_syntheses",
    "upsert_question_synthesis",
]
