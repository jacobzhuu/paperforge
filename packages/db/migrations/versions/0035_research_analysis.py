"""Snapshot-bound research analysis and durable citation shadow outbox."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0035_research_analysis"
down_revision = "0034_evidence_embedding"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "research_analysis",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "project_id",
            sa.UUID(),
            sa.ForeignKey("paper_project.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "job_id",
            sa.UUID(),
            sa.ForeignKey("generation_job.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("input_json", pg.JSONB(), nullable=False),
        sa.Column("result_json", pg.JSONB()),
        sa.Column("proposal_json", pg.JSONB()),
        sa.Column("committed_document_id", sa.UUID(), sa.ForeignKey("paper_document.id")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_research_analysis_project_id", "research_analysis", ["project_id"])
    op.create_table(
        "citation_shadow",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "project_id",
            sa.UUID(),
            sa.ForeignKey("paper_project.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "job_id",
            sa.UUID(),
            sa.ForeignKey("generation_job.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("fingerprint", sa.String(64), nullable=False, unique=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("payload_json", pg.JSONB(), nullable=False),
        sa.Column("result_json", pg.JSONB()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_citation_shadow_project_id", "citation_shadow", ["project_id"])
    op.create_index("ix_citation_shadow_status", "citation_shadow", ["status"])


def downgrade():
    op.drop_table("citation_shadow")
    op.drop_table("research_analysis")
