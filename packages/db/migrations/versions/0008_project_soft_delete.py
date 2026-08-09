"""add project soft delete and cascade llm_call_log foreign keys

Revision ID: 0008_project_soft_delete
Revises: 0007_structured_authors
Create Date: 2026-07-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_project_soft_delete"
down_revision: str | None = "0007_structured_authors"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "paper_project",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        op.f("ix_paper_project_deleted_at"), "paper_project", ["deleted_at"], unique=False
    )

    # 成本台账此前没写 ondelete，默认 NO ACTION：保留期后真删项目会被这两条外键顶回来。
    # 其余所有 project 关联表本来就是 CASCADE，这里补齐最后两条。
    op.drop_constraint("llm_call_log_project_id_fkey", "llm_call_log", type_="foreignkey")
    op.create_foreign_key(
        "llm_call_log_project_id_fkey",
        "llm_call_log",
        "paper_project",
        ["project_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.drop_constraint("llm_call_log_job_id_fkey", "llm_call_log", type_="foreignkey")
    op.create_foreign_key(
        "llm_call_log_job_id_fkey",
        "llm_call_log",
        "generation_job",
        ["job_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint("llm_call_log_job_id_fkey", "llm_call_log", type_="foreignkey")
    op.create_foreign_key(
        "llm_call_log_job_id_fkey",
        "llm_call_log",
        "generation_job",
        ["job_id"],
        ["id"],
    )
    op.drop_constraint("llm_call_log_project_id_fkey", "llm_call_log", type_="foreignkey")
    op.create_foreign_key(
        "llm_call_log_project_id_fkey",
        "llm_call_log",
        "paper_project",
        ["project_id"],
        ["id"],
    )
    op.drop_index(op.f("ix_paper_project_deleted_at"), table_name="paper_project")
    op.drop_column("paper_project", "deleted_at")
