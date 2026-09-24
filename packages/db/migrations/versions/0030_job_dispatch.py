"""Durable, deployment-scoped dispatch intents (additive for draining workers)."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0030_job_dispatch"
down_revision = "0029_sentence_downgrade"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "job_dispatch",
        sa.Column(
            "job_id",
            UUID(as_uuid=True),
            sa.ForeignKey("generation_job.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "owner_id",
            UUID(as_uuid=True),
            sa.ForeignKey("app_user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("queue_identity", sa.String(64), nullable=False),
        sa.Column("function", sa.String(120), nullable=False),
        sa.Column("kwargs_json", JSONB, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("dispatched_at", sa.DateTime(timezone=True)),
        sa.Column("attempts", sa.Integer, server_default="0", nullable=False),
        sa.Column("last_error", sa.String(120)),
        sa.Column("execution_token", UUID(as_uuid=True)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("executions", sa.Integer, server_default="0", nullable=False),
    )
    op.create_index("ix_job_dispatch_owner_id", "job_dispatch", ["owner_id"])
    op.create_index("ix_job_dispatch_queue_identity", "job_dispatch", ["queue_identity"])


def downgrade():
    op.drop_table("job_dispatch")
