"""Versioned writing state and project-scoped verification memory (additive)."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0031_writing_context"
down_revision = "0030_job_dispatch"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("paper_document", sa.Column("writing_state_json", JSONB))
    op.add_column("paper_section", sa.Column("generation_json", JSONB))
    op.add_column("claim_entailment_cache", sa.Column("project_id", UUID(as_uuid=True)))
    op.create_foreign_key(
        "fk_entailment_cache_project", "claim_entailment_cache", "paper_project",
        ["project_id"], ["id"], ondelete="CASCADE",
    )
    op.create_index("ix_claim_entailment_cache_project_id", "claim_entailment_cache", ["project_id"])


def downgrade():
    op.drop_index("ix_claim_entailment_cache_project_id", "claim_entailment_cache")
    op.drop_constraint("fk_entailment_cache_project", "claim_entailment_cache", type_="foreignkey")
    op.drop_column("claim_entailment_cache", "project_id")
    op.drop_column("paper_section", "generation_json")
    op.drop_column("paper_document", "writing_state_json")
