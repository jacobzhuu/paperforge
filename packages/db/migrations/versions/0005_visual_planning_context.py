"""add visual planning context columns

规划器要能回答两个此前答不上来的问题：这条建议**为什么**被提出来，以及它
**基于哪一版正文**。三列全部可空——历史行不回填，只表现为「没有过期判断」，
不要求重新生成任何已有资产。

Revision ID: 0005_visual_planning
Revises: 0004_auth_tenancy
Create Date: 2026-07-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_visual_planning"
down_revision: str | None = "0004_auth_tenancy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "visual_asset",
        sa.Column("paper_snapshot_hash", sa.String(length=64), nullable=True),
    )
    op.add_column("visual_asset", sa.Column("suggestion_reason", sa.Text(), nullable=True))
    op.add_column(
        "visual_asset",
        sa.Column("source_section_keys", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    _rehash_existing_specs()


def _rehash_existing_specs() -> None:
    """按新的语义投影算法回填 `input_hash`。

    `visual_input_hash` 改为只对**已设定**的字段求哈希（见
    `db.repositories.visuals.semantic_projection`），否则 spec 每加一个可选字段，
    历史资产的哈希就全部失效，下一次 visual_plan 会把已有建议原样再提一遍。

    这里一次性把历史行迁到新算法上。哈希只用于去重判断，重算不影响任何已生成的
    图片、已批准的版本或对象存储路径。
    """
    from db.repositories.visuals import visual_input_hash

    connection = op.get_bind()
    rows = connection.execute(sa.text("SELECT id, spec_json FROM visual_asset")).fetchall()
    for row in rows:
        spec = row.spec_json if isinstance(row.spec_json, dict) else {}
        connection.execute(
            sa.text("UPDATE visual_asset SET input_hash = :digest WHERE id = :id"),
            {"digest": visual_input_hash(spec), "id": row.id},
        )


def downgrade() -> None:
    op.drop_column("visual_asset", "source_section_keys")
    op.drop_column("visual_asset", "suggestion_reason")
    op.drop_column("visual_asset", "paper_snapshot_hash")
