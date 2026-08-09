"""视觉资产生命周期仓储。"""

from __future__ import annotations

import hashlib
import json
import re
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

_CAPTION_NUMBER_PREFIX_RE = re.compile(
    r"^\s*(?:(?:图|表)\s*[一二三四五六七八九十百0-9]+|(?:figure|fig\.?|table)\s*[A-Z]?\d+)\s*[.:：、\-—]?\s*",
    re.IGNORECASE,
)


def sanitize_figure_caption(caption: str) -> str:
    """题注只保存语义文本，图号由渲染模板统一生成。"""
    return _CAPTION_NUMBER_PREFIX_RE.sub("", " ".join((caption or "").split())).strip()


def logical_visual_slot(section_key: str, block_index: int, figure_label: str) -> str:
    return f"{section_key}:{block_index if block_index >= 0 else figure_label}"


def semantic_projection(value: Any) -> Any:
    """去掉「未设置」的字段，得到 spec 的语义投影。

    `input_hash` 用来判断「这条建议是不是已经提过了」。若直接对整个 spec 求哈希，
    **给 spec 增加一个可选字段就会让所有历史资产的哈希失效**——下一次 visual_plan
    会把已有建议原样再提一遍。规格是会长的（语义层 prompt、比例、negative prompt
    都要加），所以哈希必须只看真正被设定的内容：

      - None 与空集合视作「未设置」，不参与哈希；
      - 因此新增可选字段在未填写时，哈希与老版本完全一致。
    """
    if isinstance(value, dict):
        projected = {}
        for key, item in value.items():
            reduced = semantic_projection(item)
            if reduced is None:
                continue
            projected[key] = reduced
        return projected or None
    if isinstance(value, (list, tuple)):
        projected_list = [semantic_projection(item) for item in value]
        projected_list = [item for item in projected_list if item is not None]
        return projected_list or None
    if value is None or value == "":
        return None
    return value


#: 参与「这条建议是不是已经提过了」判断的字段之外的措辞类字段。
#:
#: `refined_prompt` 是文本模型对同一份语义的润色结果，每次规划的用词都不一样。
#: 把它算进哈希，等于每轮 visual_plan 都会把同一张插图重新提一遍。
_VOLATILE_SPEC_KEYS = ("refined_prompt",)


def visual_input_hash(spec: dict[str, Any]) -> str:
    stable = {key: value for key, value in spec.items() if key not in _VOLATILE_SPEC_KEYS}
    projected = semantic_projection(stable) or {}
    encoded = json.dumps(
        projected, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
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
    paper_snapshot_hash: str | None = None,
    suggestion_reason: str | None = None,
    source_section_keys: list[str] | None = None,
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
        caption=sanitize_figure_caption(caption),
        alt_text=alt_text,
        target_section_key=target_section_key,
        suggested_block_index=suggested_block_index,
        figure_label=figure_label or "pending",
        spec_json=spec,
        input_hash=visual_input_hash(spec),
        document_version=document_version,
        version=version,
        supersedes_id=supersedes_id,
        paper_snapshot_hash=paper_snapshot_hash,
        suggestion_reason=suggestion_reason,
        source_section_keys=list(source_section_keys) if source_section_keys else None,
        logical_slot_key=(
            logical_visual_slot(
                target_section_key,
                suggested_block_index,
                figure_label or "pending",
            )
            if target_section_key is not None and suggested_block_index is not None
            else None
        ),
    )
    session.add(visual)
    await session.flush()
    if visual.figure_label == "pending":
        visual.figure_label = f"fig:va_{str(visual.id)[:8]}"
        await session.flush()
    return visual


def document_snapshot_hash(sections: list[Any]) -> str:
    """当前正文的内容指纹，用于判断视觉建议是否已过期。

    只看 `section_key` 与正文 IR：改标题、改段落、增删章节都会让指纹变化，
    而重新保存一份内容相同的章节不会。规划器把它写进 `paper_snapshot_hash`，
    读侧比对后给出「建议基于旧版正文」的提示——**只提示，不自动删除**任何
    已经生成好的资产。
    """
    payload = [
        {"key": getattr(row, "section_key", ""), "body": getattr(row, "body_ir_json", None) or {}}
        for row in sections
    ]
    payload.sort(key=lambda item: item["key"])
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


async def get_visual(session: AsyncSession, visual_id: uuid.UUID) -> VisualAsset | None:
    return await session.get(VisualAsset, visual_id)


async def list_visuals(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    kind: str | None = None,
    generation_status: str | None = None,
    review_status: str | None = None,
    active_only: bool = False,
) -> list[VisualAsset]:
    stmt = select(VisualAsset).where(VisualAsset.project_id == project_id)
    if kind:
        stmt = stmt.where(VisualAsset.kind == kind)
    if generation_status:
        stmt = stmt.where(VisualAsset.generation_status == generation_status)
    if review_status:
        stmt = stmt.where(VisualAsset.review_status == review_status)
    if active_only:
        stmt = stmt.where(VisualAsset.is_active.is_(True))
    return list((await session.scalars(stmt.order_by(VisualAsset.created_at.desc()))).all())


async def active_visual_for_slot(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    logical_slot_key: str,
) -> VisualAsset | None:
    return await session.scalar(
        select(VisualAsset).where(
            VisualAsset.project_id == project_id,
            VisualAsset.logical_slot_key == logical_slot_key,
            VisualAsset.is_active.is_(True),
        )
    )


async def activate_visual(
    session: AsyncSession,
    visual: VisualAsset,
    *,
    section_key: str,
    block_index: int,
) -> VisualAsset | None:
    """原子化切换同一逻辑槽位的活动版本，返回被替换的旧版本。"""
    slot = logical_visual_slot(section_key, block_index, visual.figure_label)
    previous = await active_visual_for_slot(
        session,
        project_id=visual.project_id,
        logical_slot_key=slot,
    )
    if previous is not None and previous.id != visual.id:
        previous.is_active = False
        await session.flush()
    visual.logical_slot_key = slot
    visual.is_active = True
    await session.flush()
    return previous if previous is not None and previous.id != visual.id else None


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


async def latest_successful_visual_attempts(
    session: AsyncSession,
    visual_ids: list[uuid.UUID],
) -> dict[uuid.UUID, VisualGenerationAttempt]:
    """批量返回每张视觉最近一次成功生成记录，供列表展示准确生成时间。

    不能用 ``visual_asset.updated_at``：批准插入、修改标题等操作都会刷新它，
    那不是图片真正完成生成的时间。
    """
    if not visual_ids:
        return {}
    rows = list(
        (
            await session.scalars(
                select(VisualGenerationAttempt)
                .where(
                    VisualGenerationAttempt.visual_id.in_(visual_ids),
                    VisualGenerationAttempt.error_code.is_(None),
                )
                .order_by(
                    VisualGenerationAttempt.created_at.desc(),
                    VisualGenerationAttempt.id.desc(),
                )
            )
        ).all()
    )
    latest: dict[uuid.UUID, VisualGenerationAttempt] = {}
    for row in rows:
        latest.setdefault(row.visual_id, row)
    return latest
