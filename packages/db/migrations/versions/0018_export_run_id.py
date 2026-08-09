"""bind export artifacts to their real generation job

Revision ID: 0018_export_run_id
Revises: 0017_original_grounding
Create Date: 2026-08-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018_export_run_id"
down_revision: str | None = "0017_original_grounding"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("export_artifact", sa.Column("export_run_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "fk_export_artifact_export_run",
        "export_artifact",
        "generation_job",
        ["export_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_export_artifact_export_run_id", "export_artifact", ["export_run_id"])


def downgrade() -> None:
    op.drop_index("ix_export_artifact_export_run_id", table_name="export_artifact")
    op.drop_constraint("fk_export_artifact_export_run", "export_artifact", type_="foreignkey")
    op.drop_column("export_artifact", "export_run_id")
