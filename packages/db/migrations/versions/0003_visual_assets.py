"""add versioned visual assets and generation provenance

Revision ID: 0003_visual_assets
Revises: 0002_llm_error_code
Create Date: 2026-07-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_visual_assets"
down_revision: str | None = "0002_llm_error_code"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "visual_asset",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("generation_status", sa.String(length=16), nullable=False),
        sa.Column("review_status", sa.String(length=16), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("caption", sa.Text(), nullable=False),
        sa.Column("alt_text", sa.Text(), nullable=False),
        sa.Column("target_section_key", sa.String(length=64), nullable=True),
        sa.Column("suggested_block_index", sa.Integer(), nullable=True),
        sa.Column("figure_label", sa.String(length=128), nullable=False),
        sa.Column("spec_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=True),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("renditions_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("document_version", sa.Integer(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("supersedes_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["project_id"], ["paper_project.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["supersedes_id"], ["visual_asset.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_visual_asset_project_id", "visual_asset", ["project_id"])
    op.create_index("ix_visual_asset_supersedes_id", "visual_asset", ["supersedes_id"])
    op.create_index(
        "ix_visual_asset_project_kind_status",
        "visual_asset",
        ["project_id", "kind", "generation_status"],
    )
    op.create_table(
        "visual_source_asset",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("visual_id", sa.UUID(), nullable=False),
        sa.Column("user_asset_id", sa.UUID(), nullable=False),
        sa.Column("source_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_asset_id"], ["user_asset.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["visual_id"], ["visual_asset.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("visual_id", "user_asset_id", name="uq_visual_source_dependency"),
    )
    op.create_index("ix_visual_source_asset_visual_id", "visual_source_asset", ["visual_id"])
    op.create_index(
        "ix_visual_source_asset_user_asset_id", "visual_source_asset", ["user_asset_id"]
    )
    op.create_table(
        "visual_generation_attempt",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("visual_id", sa.UUID(), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("request_id", sa.String(length=128), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("output_width", sa.Integer(), nullable=True),
        sa.Column("output_height", sa.Integer(), nullable=True),
        sa.Column("usage_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("cost_estimate", sa.Float(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["visual_id"], ["visual_asset.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_visual_generation_attempt_visual_id",
        "visual_generation_attempt",
        ["visual_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_visual_generation_attempt_visual_id", table_name="visual_generation_attempt")
    op.drop_table("visual_generation_attempt")
    op.drop_index("ix_visual_source_asset_user_asset_id", table_name="visual_source_asset")
    op.drop_index("ix_visual_source_asset_visual_id", table_name="visual_source_asset")
    op.drop_table("visual_source_asset")
    op.drop_index("ix_visual_asset_project_kind_status", table_name="visual_asset")
    op.drop_index("ix_visual_asset_supersedes_id", table_name="visual_asset")
    op.drop_index("ix_visual_asset_project_id", table_name="visual_asset")
    op.drop_table("visual_asset")
