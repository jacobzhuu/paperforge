"""widen library entry status for candidate_uncertain

Revision ID: 0016_library_entry_status_width
Revises: 0015_private_literature_pdfs
Create Date: 2026-07-31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016_library_entry_status_width"
down_revision: str | None = "0015_private_literature_pdfs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "library_entry",
        "status",
        existing_type=sa.String(16),
        type_=sa.String(32),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.execute(sa.text("UPDATE library_entry SET status = 'candidate' WHERE length(status) > 16"))
    op.alter_column(
        "library_entry",
        "status",
        existing_type=sa.String(32),
        type_=sa.String(16),
        existing_nullable=False,
    )
