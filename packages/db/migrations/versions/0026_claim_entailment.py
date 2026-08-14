"""persist semantic claim verdicts and content-addressed cache

Revision ID: 0026_claim_entailment
Revises: 0025_question_synthesis
Create Date: 2026-08-14

Shadow verification is useful only when its evidence survives the job event stream.  The anchor
columns preserve the exact verdict applied to a quality-report snapshot.  The cache stores no
claim or excerpt text: only hashes, verdict metadata, and a key that also binds verifier version
and model.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0026_claim_entailment"
down_revision = "0025_question_synthesis"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "claim_evidence_anchor", sa.Column("entailment_verdict", sa.String(16), nullable=True)
    )
    op.add_column(
        "claim_evidence_anchor", sa.Column("entailment_confidence", sa.Float(), nullable=True)
    )
    op.add_column(
        "claim_evidence_anchor", sa.Column("entailment_reason", sa.Text(), nullable=True)
    )
    op.add_column(
        "claim_evidence_anchor", sa.Column("entailment_model", sa.String(128), nullable=True)
    )
    op.add_column(
        "claim_evidence_anchor",
        sa.Column("entailment_verifier_version", sa.String(32), nullable=True),
    )
    op.add_column(
        "claim_evidence_anchor", sa.Column("entailment_cached", sa.Boolean(), nullable=True)
    )
    op.add_column(
        "claim_evidence_anchor",
        sa.Column(
            "entailment_review_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.add_column(
        "claim_evidence_anchor",
        sa.Column("entailment_checked_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "claim_entailment_cache",
        sa.Column("cache_key", sa.String(64), primary_key=True, nullable=False),
        sa.Column("claim_hash", sa.String(64), nullable=False),
        sa.Column("evidence_hash", sa.String(64), nullable=False),
        sa.Column("claim_kind", sa.String(24), nullable=False),
        sa.Column("verifier_version", sa.String(32), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("verdict", sa.String(16), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_claim_entailment_cache_claim_evidence",
        "claim_entailment_cache",
        ["claim_hash", "evidence_hash"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_claim_entailment_cache_claim_evidence", table_name="claim_entailment_cache"
    )
    op.drop_table("claim_entailment_cache")
    op.drop_column("claim_evidence_anchor", "entailment_checked_at")
    op.drop_column("claim_evidence_anchor", "entailment_review_json")
    op.drop_column("claim_evidence_anchor", "entailment_cached")
    op.drop_column("claim_evidence_anchor", "entailment_verifier_version")
    op.drop_column("claim_evidence_anchor", "entailment_model")
    op.drop_column("claim_evidence_anchor", "entailment_reason")
    op.drop_column("claim_evidence_anchor", "entailment_confidence")
    op.drop_column("claim_evidence_anchor", "entailment_verdict")
