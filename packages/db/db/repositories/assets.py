"""用户素材仓储（设计 §4.3 user_asset）。

`parsed_json` 是「正文数字只能来自素材」这条红线的事实来源，
只由 `ingest.parse_asset` 的确定性解析写入，LLM 无权改动。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models.paper import UserAsset

ASSET_KINDS = frozenset({"dataset", "result_table", "figure", "method_note", "code", "bib"})


async def create_asset(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    kind: str,
    title: str | None,
    description: str | None = None,
    object_key: str | None = None,
    parsed: dict[str, Any] | None = None,
) -> UserAsset:
    if kind not in ASSET_KINDS:
        raise ValueError(f"unsupported asset kind: {kind}")
    asset = UserAsset(
        project_id=project_id,
        kind=kind,
        title=title,
        description=description,
        object_key=object_key,
        parsed_json=parsed,
    )
    session.add(asset)
    await session.flush()
    return asset


async def list_assets(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    kind: str | None = None,
) -> list[UserAsset]:
    stmt = (
        select(UserAsset).where(UserAsset.project_id == project_id).order_by(UserAsset.created_at)
    )
    if kind:
        stmt = stmt.where(UserAsset.kind == kind)
    return list((await session.scalars(stmt)).all())


async def get_asset(session: AsyncSession, asset_id: uuid.UUID) -> UserAsset | None:
    return await session.get(UserAsset, asset_id)


async def delete_asset(session: AsyncSession, asset: UserAsset) -> None:
    await session.delete(asset)
    await session.flush()


async def parsed_asset_payloads(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> list[dict[str, Any]]:
    """供 NUMLINT 与渲染使用的解析结果集合。"""
    return [
        asset.parsed_json
        for asset in await list_assets(session, project_id)
        if isinstance(asset.parsed_json, dict)
    ]


async def asset_render_index(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> dict[str, dict[str, Any]]:
    """asset_ref（``ua_<id前8位>`` 或素材 id）→ parsed_json，供渲染期确定性展开。"""
    index: dict[str, dict[str, Any]] = {}
    for asset in await list_assets(session, project_id):
        if not isinstance(asset.parsed_json, dict):
            continue
        index[str(asset.id)] = asset.parsed_json
        index[f"ua_{str(asset.id)[:8]}"] = asset.parsed_json
    return index
