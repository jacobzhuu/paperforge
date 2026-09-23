"""Isolated bounded evaluator shadow outbox; no changes to online quality records."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0032_evaluator_shadow"
down_revision = "0031_writing_context"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "evaluator_shadow_run",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "source_job_id",
            UUID(as_uuid=True),
            sa.ForeignKey("generation_job.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "project_id",
            UUID(as_uuid=True),
            sa.ForeignKey("paper_project.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.String(80), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("input_json", JSONB, nullable=False),
        sa.Column("results_json", JSONB, nullable=False),
        sa.Column("error", sa.Text),
        sa.Column("artifact_prefix", sa.Text, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("source_job_id", "version", name="uq_shadow_job_version"),
    )


def downgrade():
    op.drop_table("evaluator_shadow_run")
