"""prove a generation job is still owned by a live process

Revision ID: 0027_job_heartbeat
Revises: 0026_claim_entailment
Create Date: 2026-08-19

A job row that is killed rather than stopped leaves nothing behind: the container is gone before
any shutdown handler runs, so the row keeps saying ``running`` forever.  The UI then shows a task
that has been "in progress" for days, and ``ensure_project_job_slot`` refuses every new job on that
project.  ``heartbeat_at`` is the missing liveness signal: a worker stamps it while it holds the
job, so "no heartbeat for a while" separates a slow stage from an owner that no longer exists.

Existing non-terminal rows are backfilled from ``created_at`` so they age out under the same rule
instead of being trusted forever.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0027_job_heartbeat"
down_revision = "0026_claim_entailment"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "generation_job", sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.execute(
        "UPDATE generation_job SET heartbeat_at = created_at "
        "WHERE heartbeat_at IS NULL AND status IN ('queued', 'running')"
    )
    op.create_index(
        "ix_generation_job_status_heartbeat",
        "generation_job",
        ["status", "heartbeat_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_generation_job_status_heartbeat", table_name="generation_job")
    op.drop_column("generation_job", "heartbeat_at")
