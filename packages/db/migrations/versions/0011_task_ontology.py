"""add task ontology and evidence-routing provenance

Revision ID: 0011_task_ontology
Revises: 0010_fulltext_matrix
Create Date: 2026-07-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011_task_ontology"
down_revision: str | None = "0010_fulltext_matrix"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "task_definition",
        sa.Column("slug", sa.String(96), primary_key=True),
        sa.Column("domain", sa.String(64), nullable=False),
        sa.Column("label_i18n_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("metric_whitelist_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("dataset_whitelist_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("dimension_schema_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("exclusion_cues_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_table(
        "project_task_profile",
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("task_id", sa.String(96), nullable=False),
        sa.Column("is_core", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("order_index", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["project_id"], ["paper_project.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["task_definition.slug"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("project_id", "task_id"),
    )
    op.add_column("research_question", sa.Column("task_id", sa.String(96), nullable=True))
    op.create_foreign_key(
        "fk_research_question_task",
        "research_question",
        "task_definition",
        ["task_id"],
        ["slug"],
        ondelete="SET NULL",
    )
    op.add_column("evidence_unit", sa.Column("task_id", sa.String(96), nullable=True))
    op.add_column(
        "evidence_unit",
        sa.Column("topical_status", sa.String(16), server_default="uncertain", nullable=False),
    )
    op.add_column(
        "evidence_unit",
        sa.Column("anchor_strength", sa.String(24), server_default="prose_only", nullable=False),
    )
    op.add_column("evidence_unit", sa.Column("locator_display", sa.Text(), nullable=True))
    op.create_foreign_key(
        "fk_evidence_unit_task",
        "evidence_unit",
        "task_definition",
        ["task_id"],
        ["slug"],
        ondelete="SET NULL",
    )
    op.create_index("ix_evidence_unit_task_topical", "evidence_unit", ["task_id", "topical_status"])

    tasks = [
        ("bgc.identification", "BGC identification", "生物合成基因簇识别"),
        ("bgc.classification", "BGC classification", "生物合成基因簇分类"),
        ("bgc.product_structure_prediction", "BGC product structure prediction", "产物结构预测"),
        ("bgc.product_activity_prediction", "BGC product activity prediction", "产物活性预测"),
        (
            "benchmark.dataset_construction",
            "Benchmark and dataset construction",
            "基准与数据集构建",
        ),
        ("tool.engineering", "Tool engineering", "工具工程与部署"),
        ("validation.wetlab", "Wet-lab validation", "湿实验验证"),
    ]
    task_table = sa.table(
        "task_definition",
        sa.column("slug", sa.String),
        sa.column("domain", sa.String),
        sa.column("label_i18n_json", postgresql.JSONB),
        sa.column("metric_whitelist_json", postgresql.JSONB),
        sa.column("dataset_whitelist_json", postgresql.JSONB),
        sa.column("dimension_schema_json", postgresql.JSONB),
        sa.column("exclusion_cues_json", postgresql.JSONB),
    )
    op.bulk_insert(
        task_table,
        [
            {
                "slug": slug,
                "domain": "bgc",
                "label_i18n_json": {"en": en, "zh": zh},
                "metric_whitelist_json": [
                    "AUROC",
                    "AUPRC",
                    "F1",
                    "macro-F1",
                    "F-measure",
                    "MCC",
                    "precision",
                    "recall",
                    "Tanimoto",
                    "top-k accuracy",
                ],
                "dataset_whitelist_json": ["MIBiG", "antiSMASH-DB", "IMG-ABC"],
                "dimension_schema_json": [
                    "dataset",
                    "dataset_version",
                    "split_strategy",
                    "metric_name",
                ],
                "exclusion_cues_json": ["RNA secondary structure", "histopathology grading"],
            }
            for slug, en, zh in tasks
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_evidence_unit_task_topical", table_name="evidence_unit")
    op.drop_constraint("fk_evidence_unit_task", "evidence_unit", type_="foreignkey")
    for column in ("locator_display", "anchor_strength", "topical_status", "task_id"):
        op.drop_column("evidence_unit", column)
    op.drop_constraint("fk_research_question_task", "research_question", type_="foreignkey")
    op.drop_column("research_question", "task_id")
    op.drop_table("project_task_profile")
    op.drop_table("task_definition")
