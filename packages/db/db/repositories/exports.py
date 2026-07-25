"""导出产物仓储（设计 §4.3 export_artifact）。"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models.paper import ExportArtifact


async def list_export_artifacts(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    limit: int = 50,
) -> list[ExportArtifact]:
    return list(
        (
            await session.scalars(
                select(ExportArtifact)
                .where(ExportArtifact.project_id == project_id)
                .order_by(ExportArtifact.created_at.desc())
                .limit(limit)
            )
        ).all()
    )
