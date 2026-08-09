"""add original-paper asset grounding to claim audits

Revision ID: 0017_original_grounding
Revises: 0016_library_entry_status_width
Create Date: 2026-08-01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017_original_grounding"
down_revision: str | None = "0016_library_entry_status_width"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("claim_evidence_anchor", sa.Column("user_asset_id", sa.UUID(), nullable=True))
    op.add_column(
        "claim_evidence_anchor",
        sa.Column("source_key", sa.String(160), nullable=True),
    )
    op.execute(
        sa.text(
            "UPDATE claim_evidence_anchor SET source_key = "
            "CASE WHEN cite_key IS NULL THEN 'none' ELSE 'cite:' || cite_key END"
        )
    )
    # PostgreSQL's former UNIQUE(report, claim, cite_key) constraint allowed
    # multiple rows when cite_key was NULL.  They all map to the canonical
    # ``none`` source key, so collapse those legacy duplicates before making
    # source_key non-null and unique.  Prefer the most useful/reviewed anchor
    # and use stable tie-breakers so the data migration is deterministic.
    op.execute(
        sa.text(
            "WITH ranked AS ("
            " SELECT id, row_number() OVER ("
            "  PARTITION BY quality_report_id, claim_hash, source_key"
            "  ORDER BY is_core DESC,"
            "   (manual_status <> 'unreviewed') DESC,"
            "   (evidence_excerpt IS NOT NULL) DESC,"
            "   (evidence_unit_id IS NOT NULL) DESC,"
            "   support_score DESC NULLS LAST, created_at DESC, id DESC"
            " ) AS duplicate_rank"
            " FROM claim_evidence_anchor"
            ") DELETE FROM claim_evidence_anchor AS anchor"
            " USING ranked"
            " WHERE anchor.id = ranked.id AND ranked.duplicate_rank > 1"
        )
    )
    op.alter_column("claim_evidence_anchor", "source_key", nullable=False)
    op.alter_column(
        "claim_evidence_anchor",
        "support_status",
        existing_type=sa.String(24),
        type_=sa.String(48),
        existing_nullable=False,
    )
    op.create_foreign_key(
        "fk_claim_evidence_user_asset",
        "claim_evidence_anchor",
        "user_asset",
        ["user_asset_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_claim_evidence_user_asset_id",
        "claim_evidence_anchor",
        ["user_asset_id"],
    )
    op.drop_constraint(
        "uq_claim_evidence_report_claim_cite",
        "claim_evidence_anchor",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_claim_evidence_report_claim_source",
        "claim_evidence_anchor",
        ["quality_report_id", "claim_hash", "source_key"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_claim_evidence_report_claim_source",
        "claim_evidence_anchor",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_claim_evidence_report_claim_cite",
        "claim_evidence_anchor",
        ["quality_report_id", "claim_hash", "cite_key"],
    )
    op.drop_index("ix_claim_evidence_user_asset_id", table_name="claim_evidence_anchor")
    op.drop_constraint("fk_claim_evidence_user_asset", "claim_evidence_anchor", type_="foreignkey")
    op.drop_column("claim_evidence_anchor", "source_key")
    op.drop_column("claim_evidence_anchor", "user_asset_id")
