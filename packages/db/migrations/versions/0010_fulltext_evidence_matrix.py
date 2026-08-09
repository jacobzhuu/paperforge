"""persist full-text audit trail, chunks, structured experiments, and canonical work links.

Revision ID: 0010_fulltext_matrix
Revises: 0009_problem_evidence
Create Date: 2026-07-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010_fulltext_matrix"
down_revision: str | None = "0009_problem_evidence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    ]


def upgrade() -> None:
    op.add_column("scholarly_work", sa.Column("canonical_work_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "fk_scholarly_work_canonical",
        "scholarly_work",
        "scholarly_work",
        ["canonical_work_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_scholarly_work_canonical_work_id", "scholarly_work", ["canonical_work_id"])
    op.create_unique_constraint(
        "uq_library_entry_project_work", "library_entry", ["project_id", "work_id"]
    )
    op.add_column("document_file", sa.Column("content_hash", sa.String(64), nullable=True))
    op.create_index("ix_document_file_content_hash", "document_file", ["content_hash"])
    op.create_unique_constraint(
        "uq_document_file_work_content", "document_file", ["work_id", "content_hash"]
    )
    for name, column in (
        ("attack_goal", sa.Text()),
        ("threat_model", sa.Text()),
        ("victim_model", sa.String(255)),
        ("attack_budget_json", postgresql.JSONB(astext_type=sa.Text())),
        ("protocol_json", postgresql.JSONB(astext_type=sa.Text())),
    ):
        op.add_column("evidence_measurement", sa.Column(name, column, nullable=True))

    op.create_table(
        "work_version_relation",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("source_work_id", sa.UUID(), nullable=False),
        sa.Column("target_work_id", sa.UUID(), nullable=False),
        sa.Column("relation_type", sa.String(32), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="1"),
        sa.Column("rationale_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["source_work_id"], ["scholarly_work.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["target_work_id"], ["scholarly_work.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_work_id", "target_work_id", "relation_type", name="uq_work_version_relation"
        ),
    )
    op.create_index(
        "ix_work_version_relation_source_work_id", "work_version_relation", ["source_work_id"]
    )
    op.create_index(
        "ix_work_version_relation_target_work_id", "work_version_relation", ["target_work_id"]
    )

    op.create_table(
        "fulltext_attempt",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("job_id", sa.UUID(), nullable=True),
        sa.Column("work_id", sa.UUID(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("mime_type", sa.String(128), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["project_id"], ["paper_project.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["job_id"], ["generation_job.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["work_id"], ["scholarly_work.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("project_id", "job_id", "work_id"):
        op.create_index(f"ix_fulltext_attempt_{column}", "fulltext_attempt", [column])
    op.create_index(
        "ix_fulltext_attempt_project_work",
        "fulltext_attempt",
        ["project_id", "work_id", "created_at"],
    )

    op.create_table(
        "document_parse",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("document_file_id", sa.UUID(), nullable=False),
        sa.Column("parser_version", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("extracted_text", sa.Text(), nullable=True),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["document_file_id"], ["document_file.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "document_file_id", "parser_version", name="uq_document_parse_file_version"
        ),
    )
    op.create_index("ix_document_parse_document_file_id", "document_parse", ["document_file_id"])
    op.create_table(
        "document_chunk",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("document_parse_id", sa.UUID(), nullable=False),
        sa.Column("chunk_no", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("content_role", sa.String(32), nullable=True),
        sa.Column("page", sa.Integer(), nullable=True),
        sa.Column("section_path", sa.Text(), nullable=True),
        sa.Column("object_ref", sa.String(64), nullable=True),
        sa.Column("char_start", sa.Integer(), nullable=True),
        sa.Column("char_end", sa.Integer(), nullable=True),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["document_parse_id"], ["document_parse.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_parse_id", "chunk_no", name="uq_document_chunk_parse_no"),
    )
    op.create_index("ix_document_chunk_document_parse_id", "document_chunk", ["document_parse_id"])
    op.create_index(
        "ix_document_chunk_parse_role", "document_chunk", ["document_parse_id", "content_role"]
    )

    op.create_table(
        "structured_extraction",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("work_id", sa.UUID(), nullable=False),
        sa.Column("document_file_id", sa.UUID(), nullable=False),
        sa.Column("schema_version", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("payload_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("source_hash", sa.String(80), nullable=True),
        sa.Column("extraction_model", sa.String(128), nullable=True),
        sa.Column("validation_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["work_id"], ["scholarly_work.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_file_id"], ["document_file.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "work_id",
            "document_file_id",
            "schema_version",
            name="uq_structured_extraction_source_schema",
        ),
    )
    for column in ("work_id", "document_file_id", "source_hash"):
        op.create_index(f"ix_structured_extraction_{column}", "structured_extraction", [column])
    op.create_table(
        "experiment_result",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("structured_extraction_id", sa.UUID(), nullable=False),
        sa.Column("evidence_unit_id", sa.UUID(), nullable=True),
        sa.Column("record_kind", sa.String(32), nullable=False, server_default="main_result"),
        sa.Column("task", sa.String(255), nullable=True),
        sa.Column("attack_goal", sa.Text(), nullable=True),
        sa.Column("threat_model", sa.Text(), nullable=True),
        sa.Column("victim_model", sa.String(255), nullable=True),
        sa.Column("surrogate_model", sa.String(255), nullable=True),
        sa.Column("dataset", sa.String(255), nullable=True),
        sa.Column("attack_budget_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("metric_name", sa.String(128), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("unit", sa.String(32), nullable=True),
        sa.Column("clean_value", sa.Float(), nullable=True),
        sa.Column("delta_value", sa.Float(), nullable=True),
        sa.Column("comparability_key", sa.String(40), nullable=False),
        sa.Column("source_location", sa.Text(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["structured_extraction_id"], ["structured_extraction.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["evidence_unit_id"], ["evidence_unit.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "structured_extraction_id",
            "metric_name",
            "dataset",
            "victim_model",
            "value",
            "source_location",
            name="uq_experiment_result_cell",
        ),
    )
    op.create_index(
        "ix_experiment_result_structured_extraction_id",
        "experiment_result",
        ["structured_extraction_id"],
    )
    op.create_index(
        "ix_experiment_result_evidence_unit_id", "experiment_result", ["evidence_unit_id"]
    )
    op.create_index(
        "ix_experiment_result_comparability", "experiment_result", ["comparability_key"]
    )


def downgrade() -> None:
    op.drop_table("experiment_result")
    op.drop_table("structured_extraction")
    op.drop_table("document_chunk")
    op.drop_table("document_parse")
    op.drop_table("fulltext_attempt")
    op.drop_table("work_version_relation")
    for column in (
        "protocol_json",
        "attack_budget_json",
        "victim_model",
        "threat_model",
        "attack_goal",
    ):
        op.drop_column("evidence_measurement", column)
    op.drop_constraint("uq_document_file_work_content", "document_file", type_="unique")
    op.drop_index("ix_document_file_content_hash", table_name="document_file")
    op.drop_column("document_file", "content_hash")
    op.drop_constraint("uq_library_entry_project_work", "library_entry", type_="unique")
    op.drop_index("ix_scholarly_work_canonical_work_id", table_name="scholarly_work")
    op.drop_constraint("fk_scholarly_work_canonical", "scholarly_work", type_="foreignkey")
    op.drop_column("scholarly_work", "canonical_work_id")
