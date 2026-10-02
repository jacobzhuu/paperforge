"""Add nullable field provenance without changing legacy evidence or in-flight jobs."""

from alembic import op
from sqlalchemy import Column
from sqlalchemy.dialects.postgresql import JSONB

revision = "0039_field_provenance"
down_revision = "0038_draft_revision"
branch_labels = depends_on = None


def upgrade():
    for table in ("experiment_result", "evidence_measurement"):
        op.add_column(table, Column("provenance_json", JSONB, nullable=True))


def downgrade():
    for table in ("evidence_measurement", "experiment_result"):
        op.drop_column(table, "provenance_json")
