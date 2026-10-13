"""Durable revision operations; nullable for workers from older deployments."""

from alembic import op
from sqlalchemy import Column
from sqlalchemy.dialects.postgresql import JSONB

revision = "0040_revision_recovery"
down_revision = "0039_field_provenance"
branch_labels = depends_on = None


def upgrade():
    op.add_column("draft_revision", Column("recovery_json", JSONB, nullable=True))


def downgrade():
    op.drop_column("draft_revision", "recovery_json")
