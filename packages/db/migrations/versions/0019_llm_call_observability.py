"""retain provider, stage metadata, and real occurrence time for LLM calls

Revision ID: 0019_llm_call_observability
Revises: 0018_export_run_id
Create Date: 2026-08-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0019_llm_call_observability"
down_revision: str | None = "0018_export_run_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("llm_call_log", sa.Column("provider", sa.String(length=32), nullable=True))
    op.add_column(
        "llm_call_log",
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "llm_call_log",
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.alter_column("llm_call_log", "occurred_at", server_default=None)


def downgrade() -> None:
    op.drop_column("llm_call_log", "occurred_at")
    op.drop_column("llm_call_log", "metadata_json")
    op.drop_column("llm_call_log", "provider")
