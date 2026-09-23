"""Project-scoped content-addressed embeddings; no change to existing workers."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0034_evidence_embedding"
down_revision = "0033_mcp_web_research"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "evidence_embedding",
        sa.Column(
            "project_id",
            sa.UUID(),
            sa.ForeignKey("paper_project.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "evidence_id",
            sa.UUID(),
            sa.ForeignKey("evidence_unit.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("model", sa.String(160), primary_key=True),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("embedding", postgresql.ARRAY(sa.Float()), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index(
        "ix_evidence_embedding_project_model", "evidence_embedding", ["project_id", "model"]
    )


def downgrade():
    op.drop_table("evidence_embedding")
