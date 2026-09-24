"""Persist the project execution default; existing projects remain standard."""

import sqlalchemy as sa
from alembic import op

revision = "0037_execution_profile"
down_revision = "0036_shadow_attempt"
branch_labels = depends_on = None


def upgrade():
    op.add_column(
        "paper_project",
        sa.Column("execution_profile", sa.String(16), nullable=False, server_default="standard"),
    )
    op.create_check_constraint(
        "ck_paper_project_execution_profile",
        "paper_project",
        "execution_profile IN ('standard', 'fast_draft')",
    )


def downgrade():
    op.drop_constraint("ck_paper_project_execution_profile", "paper_project", type_="check")
    op.drop_column("paper_project", "execution_profile")
