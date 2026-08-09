"""lock user-authored research questions and record adaptation history

Revision ID: 0021_question_locking
Revises: 0020_llm_occurred_at_default
Create Date: 2026-08-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0021_question_locking"
down_revision: str | None = "0020_llm_occurred_at_default"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "research_question",
        sa.Column("origin", sa.String(16), nullable=False, server_default="auto"),
    )
    op.add_column(
        "research_question",
        sa.Column("locked", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    # Existing rows PATCHed through the questions workbench are the only ones that
    # ever carried generator='user'; they are exactly the edits regeneration used
    # to destroy, so they become locked user-origin rows.
    op.execute(
        sa.text(
            """
            UPDATE research_question
            SET origin = 'user', locked = true
            WHERE generator = 'user'
            """
        )
    )

    op.create_table(
        "research_question_revision",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "research_question_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("research_question.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("old_text", sa.Text(), nullable=False),
        sa.Column("new_text", sa.Text(), nullable=False),
        sa.Column("reason", sa.String(64), nullable=False),
        sa.Column("detail_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("research_question_revision")
    op.drop_column("research_question", "locked")
    op.drop_column("research_question", "origin")
