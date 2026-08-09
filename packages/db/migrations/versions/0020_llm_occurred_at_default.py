"""keep LLM occurrence timestamp compatible with draining old workers

Revision ID: 0020_llm_occurred_at_default
Revises: 0019_llm_call_observability
Create Date: 2026-08-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020_llm_occurred_at_default"
down_revision: str | None = "0019_llm_call_observability"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("llm_call_log", "occurred_at", server_default=sa.text("now()"))


def downgrade() -> None:
    op.alter_column("llm_call_log", "occurred_at", server_default=None)
