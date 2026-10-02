"""Isolated, snapshot-bound automatic draft revisions."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0038_draft_revision"
down_revision = "0037_execution_profile"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "draft_revision",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id",
            UUID(as_uuid=True),
            sa.ForeignKey("paper_project.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "source_job_id",
            UUID(as_uuid=True),
            sa.ForeignKey("generation_job.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "job_id",
            UUID(as_uuid=True),
            sa.ForeignKey("generation_job.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "source_document_id",
            UUID(as_uuid=True),
            sa.ForeignKey("paper_document.id"),
            nullable=False,
        ),
        sa.Column(
            "candidate_document_id",
            UUID(as_uuid=True),
            sa.ForeignKey("paper_document.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("source_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("review_json", JSONB),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_draft_revision_project_id", "draft_revision", ["project_id"])


def downgrade():
    op.drop_table("draft_revision")
