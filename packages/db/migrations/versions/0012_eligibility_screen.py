"""add auditable literature eligibility decisions

Revision ID: 0012_eligibility_screen
Revises: 0011_task_ontology
Create Date: 2026-07-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012_eligibility_screen"
down_revision: str | None = "0011_task_ontology"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "eligibility_decision",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("work_id", sa.UUID(), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("criterion_hits_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("anchor_facet_hit", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("decided_by", sa.String(24), nullable=False),
        sa.Column("model", sa.String(128), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["project_id"], ["paper_project.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["work_id"], ["scholarly_work.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("project_id", "work_id", name="uq_eligibility_project_work"),
    )
    op.create_index(
        "ix_eligibility_project_decision", "eligibility_decision", ["project_id", "decision"]
    )


def downgrade() -> None:
    op.drop_index("ix_eligibility_project_decision", table_name="eligibility_decision")
    op.drop_table("eligibility_decision")
