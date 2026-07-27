"""视觉资产生命周期仓储。"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models.paper import (
    UserAsset,
    VisualAsset,
    VisualGenerationAttempt,
    VisualSourceAsset,
)

VISUAL_KINDS = frozenset({"chart", "diagram", "ai_image"})
GENERATION_STATUSES = frozenset({"proposed", "queued", "running", "ready", "failed"})
REVIEW_STATUSES = frozenset({"pending", "approved", "rejected"})


def visual_input_hash(spec: dict[str, Any]) -> str:
    encoded = json.dumps(spec, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


async def create_visual(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    kind: str,
    spec: dict[str, Any],
    title: str | None = None,
    caption: str = "",
    alt_text: str = "",
    target_section_key: str | None = None,
    suggested_block_index: int | None = None,
    document_version: int | None = None,
    version: int = 1,
    supersedes_id: uuid.UUID | None = None,
    generation_status: str = "proposed",
    figure_label: str | None = None,
) -> VisualAsset:
    if kind not in VISUAL_KINDS:
        raise ValueError(f"unsupported visual kind: {kind}")
    if generation_status not in GENERATION_STATUSES:
        raise ValueError(f"unsupported generation status: {generation_status}")
    visual = VisualAsset(
        project_id=project_id,
        kind=kind,
        generation_status=generation_status,
        review_status="pending",
        title=title,
        caption=caption,
        alt_text=alt_text,
        target_section_key=target_section_key,
        suggested_block_index=suggested_block_index,
        figure_label=figure_label or "pending",
        spec_json=spec,
        input_hash=visual_input_hash(spec),
        document_version=document_version,
        version=version,
        supersedes_id=supersedes_id,
    )
    session.add(visual)
    await session.flush()
    if visual.figure_label == "pending":
        visual.figure_label = f"fig:va_{str(visual.id)[:8]}"
        await session.flush()
    return visual


async def get_visual(session: AsyncSession, visual_id: uuid.UUID) -> VisualAsset | None:
    return await session.get(VisualAsset, visual_id)


async def list_visuals(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    kind: str | None = None,
    generation_status: str | None = None,
    review_status: str | None = None,
) -> list[VisualAsset]:
    stmt = select(VisualAsset).where(VisualAsset.project_id == project_id)
    if kind:
        stmt = stmt.where(VisualAsset.kind == kind)
    if generation_status:
        stmt = stmt.where(VisualAsset.generation_status == generation_status)
    if review_status:
        stmt = stmt.where(VisualAsset.review_status == review_status)
    return list((await session.scalars(stmt.order_by(VisualAsset.created_at.desc()))).all())


async def add_visual_source(
    session: AsyncSession,
    *,
    visual_id: uuid.UUID,
    user_asset: UserAsset,
    source_hash: str,
) -> VisualSourceAsset:
    dependency = VisualSourceAsset(
        visual_id=visual_id,
        user_asset_id=user_asset.id,
        source_hash=source_hash,
    )
    session.add(dependency)
    await session.flush()
    return dependency


async def visual_source_dependency_count(session: AsyncSession, user_asset_id: uuid.UUID) -> int:
    return int(
        await session.scalar(
            select(func.count(VisualSourceAsset.id)).where(
                VisualSourceAsset.user_asset_id == user_asset_id
            )
        )
        or 0
    )


async def record_visual_attempt(
    session: AsyncSession,
    *,
    visual_id: uuid.UUID,
    provider: str,
    model: str | None = None,
    request_id: str | None = None,
    latency_ms: int | None = None,
    output_width: int | None = None,
    output_height: int | None = None,
    usage: dict[str, Any] | None = None,
    cost_estimate: float | None = None,
    error_code: str | None = None,
) -> VisualGenerationAttempt:
    attempt = VisualGenerationAttempt(
        visual_id=visual_id,
        provider=provider,
        model=model,
        request_id=request_id,
        latency_ms=latency_ms,
        output_width=output_width,
        output_height=output_height,
        usage_json=usage,
        cost_estimate=cost_estimate,
        error_code=error_code,
    )
    session.add(attempt)
    await session.flush()
    return attempt
