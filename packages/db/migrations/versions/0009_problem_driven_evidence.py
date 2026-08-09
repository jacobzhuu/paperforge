"""add problem-driven evidence and research-question schema

Revision ID: 0009_problem_evidence
Revises: 0008_project_soft_delete
Create Date: 2026-07-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009_problem_evidence"
down_revision: str | None = "0008_project_soft_delete"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("document_file", sa.Column("license", sa.String(128), nullable=True))

    op.create_table(
        "evidence_unit",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("work_id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("grade", sa.String(32), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("text_hash", sa.String(64), nullable=False),
        sa.Column("page", sa.Integer(), nullable=True),
        sa.Column("section_path", sa.Text(), nullable=True),
        sa.Column("paragraph_index", sa.Integer(), nullable=True),
        sa.Column("object_ref", sa.String(64), nullable=True),
        sa.Column("char_start", sa.Integer(), nullable=True),
        sa.Column("char_end", sa.Integer(), nullable=True),
        sa.Column("source_document_file_id", sa.UUID(), nullable=True),
        sa.Column("extraction_model", sa.String(128), nullable=True),
        sa.Column("source_hash", sa.String(80), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["work_id"], ["scholarly_work.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["paper_project.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["source_document_file_id"], ["document_file.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "work_id",
            "project_id",
            "text_hash",
            "source_document_file_id",
            name="uq_evidence_unit_source_text",
        ),
    )
    for column in ("work_id", "project_id", "source_document_file_id", "source_hash"):
        op.create_index(f"ix_evidence_unit_{column}", "evidence_unit", [column])
    op.create_index("ix_evidence_unit_project_grade", "evidence_unit", ["project_id", "grade"])
    op.create_index("ix_evidence_unit_work_grade", "evidence_unit", ["work_id", "grade"])

    op.create_table(
        "evidence_measurement",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("evidence_unit_id", sa.UUID(), nullable=False),
        sa.Column("metric_name", sa.String(128), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("unit", sa.String(32), nullable=True),
        sa.Column("ci_low", sa.Float(), nullable=True),
        sa.Column("ci_high", sa.Float(), nullable=True),
        sa.Column("std", sa.Float(), nullable=True),
        sa.Column("dataset", sa.String(255), nullable=True),
        sa.Column("task", sa.String(255), nullable=True),
        sa.Column("model_family", sa.String(255), nullable=True),
        sa.Column("sample_size", sa.Integer(), nullable=True),
        sa.Column("split", sa.String(128), nullable=True),
        sa.Column("comparability_key", sa.String(40), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["evidence_unit_id"], ["evidence_unit.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "evidence_unit_id",
            "metric_name",
            "dataset",
            "split",
            "value",
            name="uq_evidence_measurement_value",
        ),
    )
    op.create_index(
        "ix_evidence_measurement_evidence_unit_id",
        "evidence_measurement",
        ["evidence_unit_id"],
    )
    op.create_index(
        "ix_evidence_measurement_comparability",
        "evidence_measurement",
        ["comparability_key"],
    )

    op.create_table(
        "research_question",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("parent_id", sa.UUID(), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("order_index", sa.Integer(), nullable=False),
        sa.Column(
            "comparison_dimensions_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "expected_evidence_kinds_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "answer_status",
            sa.String(32),
            server_default="insufficient_evidence",
            nullable=False,
        ),
        sa.Column("generator", sa.String(128), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["project_id"], ["paper_project.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["parent_id"], ["research_question.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "kind", "order_index", name="uq_question_project_order"),
    )
    op.create_index("ix_research_question_project_id", "research_question", ["project_id"])
    op.create_index("ix_research_question_parent_id", "research_question", ["parent_id"])
    op.create_index(
        "ix_research_question_project_parent",
        "research_question",
        ["project_id", "parent_id"],
    )

    op.create_table(
        "question_evidence_link",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("research_question_id", sa.UUID(), nullable=False),
        sa.Column("evidence_unit_id", sa.UUID(), nullable=False),
        sa.Column("stance", sa.String(24), nullable=False),
        sa.Column("condition_note", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("manually_overridden", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["research_question_id"], ["research_question.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["evidence_unit_id"], ["evidence_unit.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "research_question_id",
            "evidence_unit_id",
            name="uq_question_evidence_link",
        ),
    )
    op.create_index(
        "ix_question_evidence_link_research_question_id",
        "question_evidence_link",
        ["research_question_id"],
    )
    op.create_index(
        "ix_question_evidence_link_evidence_unit_id",
        "question_evidence_link",
        ["evidence_unit_id"],
    )
    op.create_index(
        "ix_question_evidence_stance",
        "question_evidence_link",
        ["research_question_id", "stance"],
    )

    op.add_column("claim_evidence_anchor", sa.Column("evidence_unit_id", sa.UUID(), nullable=True))
    op.add_column(
        "claim_evidence_anchor", sa.Column("comparability_ok", sa.Boolean(), nullable=True)
    )
    op.add_column("claim_evidence_anchor", sa.Column("grade_ok", sa.Boolean(), nullable=True))
    op.create_foreign_key(
        "fk_claim_evidence_anchor_evidence_unit",
        "claim_evidence_anchor",
        "evidence_unit",
        ["evidence_unit_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_claim_evidence_anchor_evidence_unit_id",
        "claim_evidence_anchor",
        ["evidence_unit_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_claim_evidence_anchor_evidence_unit_id", table_name="claim_evidence_anchor")
    op.drop_constraint(
        "fk_claim_evidence_anchor_evidence_unit",
        "claim_evidence_anchor",
        type_="foreignkey",
    )
    for column in ("grade_ok", "comparability_ok", "evidence_unit_id"):
        op.drop_column("claim_evidence_anchor", column)
    op.drop_table("question_evidence_link")
    op.drop_table("research_question")
    op.drop_table("evidence_measurement")
    op.drop_table("evidence_unit")
    op.drop_column("document_file", "license")
