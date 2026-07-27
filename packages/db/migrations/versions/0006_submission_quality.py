"""add submission quality reports and evidence anchors

Revision ID: 0006_submission_quality
Revises: 0005_visual_planning
Create Date: 2026-07-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_submission_quality"
down_revision: str | None = "0005_visual_planning"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("paper_project", sa.Column("publication_title", sa.Text(), nullable=True))
    op.add_column(
        "paper_project",
        sa.Column("authors_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "paper_project",
        sa.Column("keywords_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "paper_project", sa.Column("metadata_confirmed_at", sa.DateTime(timezone=True), nullable=True)
    )

    op.add_column("visual_asset", sa.Column("logical_slot_key", sa.String(160), nullable=True))
    op.add_column(
        "visual_asset",
        sa.Column("is_active", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.execute(
        """
        UPDATE visual_asset
        SET logical_slot_key = COALESCE(target_section_key, 'unplaced') || ':' ||
            COALESCE(suggested_block_index::text, figure_label)
        """
    )
    op.execute(
        """
        UPDATE visual_asset AS visual
        SET is_active = TRUE
        WHERE visual.id IN (
            SELECT DISTINCT ON (project_id, logical_slot_key) id
            FROM visual_asset
            WHERE review_status = 'approved'
            ORDER BY project_id, logical_slot_key, created_at DESC, id DESC
        )
        """
    )
    op.create_index("ix_visual_asset_logical_slot_key", "visual_asset", ["logical_slot_key"])
    op.create_index(
        "uq_visual_asset_active_slot",
        "visual_asset",
        ["project_id", "logical_slot_key"],
        unique=True,
        postgresql_where=sa.text("is_active = true"),
    )

    op.create_table(
        "quality_report",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("document_id", sa.UUID(), nullable=False),
        sa.Column("document_version", sa.Integer(), nullable=False),
        sa.Column("paper_snapshot_hash", sa.String(64), nullable=False),
        sa.Column("quality_profile", sa.String(16), nullable=False),
        sa.Column("review_style", sa.String(16), nullable=False),
        sa.Column("readiness_status", sa.String(32), nullable=False),
        sa.Column("stale", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("blockers_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("warnings_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("scores_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("metrics_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("layout_checks_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["paper_document.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["paper_project.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_quality_report_project_id", "quality_report", ["project_id"])
    op.create_index("ix_quality_report_document_id", "quality_report", ["document_id"])
    op.create_index(
        "ix_quality_report_project_profile_created",
        "quality_report",
        ["project_id", "quality_profile", "created_at"],
    )

    op.create_table(
        "claim_evidence_anchor",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("quality_report_id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("document_id", sa.UUID(), nullable=False),
        sa.Column("section_id", sa.UUID(), nullable=False),
        sa.Column("work_id", sa.UUID(), nullable=True),
        sa.Column("section_key", sa.String(64), nullable=False),
        sa.Column("claim_hash", sa.String(64), nullable=False),
        sa.Column("claim_text", sa.Text(), nullable=False),
        sa.Column("claim_kind", sa.String(24), nullable=False),
        sa.Column("is_core", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("cite_key", sa.String(128), nullable=True),
        sa.Column("source_kind", sa.String(24), nullable=False),
        sa.Column("source_page", sa.Integer(), nullable=True),
        sa.Column("source_section", sa.Text(), nullable=True),
        sa.Column("source_paragraph", sa.Integer(), nullable=True),
        sa.Column("evidence_excerpt", sa.Text(), nullable=True),
        sa.Column("evidence_hash", sa.String(64), nullable=True),
        sa.Column("support_status", sa.String(24), nullable=False),
        sa.Column("support_score", sa.Float(), nullable=True),
        sa.Column("manual_status", sa.String(24), server_default="unreviewed", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["paper_document.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["paper_project.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["quality_report_id"], ["quality_report.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["section_id"], ["paper_section.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["work_id"], ["scholarly_work.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "quality_report_id", "claim_hash", "cite_key", name="uq_claim_evidence_report_claim_cite"
        ),
    )
    for column in ("quality_report_id", "project_id", "document_id", "section_id", "work_id"):
        op.create_index(f"ix_claim_evidence_anchor_{column}", "claim_evidence_anchor", [column])
    op.create_index(
        "ix_claim_evidence_project_core", "claim_evidence_anchor", ["project_id", "is_core"]
    )

    op.add_column("export_artifact", sa.Column("quality_report_id", sa.UUID(), nullable=True))
    op.add_column(
        "export_artifact",
        sa.Column("quality_profile", sa.String(16), server_default="draft", nullable=False),
    )
    op.add_column(
        "export_artifact",
        sa.Column("readiness_status", sa.String(32), server_default="unassessed", nullable=False),
    )
    op.add_column("export_artifact", sa.Column("paper_snapshot_hash", sa.String(64), nullable=True))
    op.create_foreign_key(
        "fk_export_artifact_quality_report",
        "export_artifact",
        "quality_report",
        ["quality_report_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_export_artifact_quality_report_id", "export_artifact", ["quality_report_id"])


def downgrade() -> None:
    op.drop_index("ix_export_artifact_quality_report_id", table_name="export_artifact")
    op.drop_constraint("fk_export_artifact_quality_report", "export_artifact", type_="foreignkey")
    for column in ("paper_snapshot_hash", "readiness_status", "quality_profile", "quality_report_id"):
        op.drop_column("export_artifact", column)
    op.drop_table("claim_evidence_anchor")
    op.drop_table("quality_report")
    op.drop_index("uq_visual_asset_active_slot", table_name="visual_asset")
    op.drop_index("ix_visual_asset_logical_slot_key", table_name="visual_asset")
    op.drop_column("visual_asset", "is_active")
    op.drop_column("visual_asset", "logical_slot_key")
    for column in ("metadata_confirmed_at", "keywords_json", "authors_json", "publication_title"):
        op.drop_column("paper_project", column)
