"""add task-parameterized experiment-v2 result dimensions

Revision ID: 0013_experiment_v2_fields
Revises: 0012_eligibility_screen
Create Date: 2026-07-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_experiment_v2_fields"
down_revision: str | None = "0012_eligibility_screen"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = (
    ("task_id", sa.String(96)),
    ("task_variant", sa.String(255)),
    ("dataset_version", sa.String(128)),
    ("split_strategy", sa.String(128)),
    ("train_size", sa.Integer()),
    ("val_size", sa.Integer()),
    ("test_size", sa.Integer()),
    ("positive_count", sa.Integer()),
    ("negative_count", sa.Integer()),
    ("negative_sampling", sa.Text()),
    ("label_source", sa.Text()),
    ("model_family", sa.String(255)),
    ("architecture_detail", sa.Text()),
    ("pretrained_backbone", sa.String(255)),
    ("input_representation", sa.String(255)),
    ("param_count", sa.Integer()),
    ("loss_function", sa.String(255)),
    ("optimizer", sa.String(128)),
    ("learning_rate", sa.Float()),
    ("batch_size", sa.Integer()),
    ("epochs", sa.Integer()),
    ("regularization", sa.Text()),
    ("ci_low", sa.Float()),
    ("ci_high", sa.Float()),
    ("std", sa.Float()),
    ("baseline_name", sa.String(255)),
    ("baseline_value", sa.Float()),
    ("anchor_strength", sa.String(24)),
)


def upgrade() -> None:
    for name, type_ in _COLUMNS:
        op.add_column("experiment_result", sa.Column(name, type_, nullable=True))
    op.create_foreign_key(
        "fk_experiment_result_task",
        "experiment_result",
        "task_definition",
        ["task_id"],
        ["slug"],
        ondelete="SET NULL",
    )
    op.create_index("ix_experiment_result_task_id", "experiment_result", ["task_id"])


def downgrade() -> None:
    op.drop_index("ix_experiment_result_task_id", table_name="experiment_result")
    op.drop_constraint("fk_experiment_result_task", "experiment_result", type_="foreignkey")
    for name, _type in reversed(_COLUMNS):
        op.drop_column("experiment_result", name)
