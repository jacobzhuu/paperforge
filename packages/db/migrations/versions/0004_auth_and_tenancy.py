"""add accounts, opaque sessions and project ownership

Revision ID: 0004_auth_tenancy
Revises: 0003_visual_assets
Create Date: 2026-07-27
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_auth_tenancy"
down_revision: str | None = "0003_visual_assets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LEGACY_USER_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")


def upgrade() -> None:
    op.create_table(
        "app_user",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=True),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_app_user_email", "app_user", ["email"], unique=True)
    op.create_table(
        "user_session",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index("ix_user_session_user_id", "user_session", ["user_id"])
    op.create_index("ix_user_session_user_revoked", "user_session", ["user_id", "revoked_at"])
    op.create_index("ix_user_session_expires_at", "user_session", ["expires_at"])
    op.create_table(
        "auth_action_token",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("purpose", sa.String(length=24), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name="uq_auth_action_token_hash"),
    )
    op.create_index(
        "ix_auth_action_token_user_purpose", "auth_action_token", ["user_id", "purpose"]
    )
    op.create_index("ix_auth_action_token_expires_at", "auth_action_token", ["expires_at"])

    # Alembic must never receive a password. A disabled placeholder owns legacy rows until
    # the explicit bootstrap-admin command replaces it and activates the account.
    op.execute(
        sa.text(
            """
            INSERT INTO app_user
                (id, email, display_name, password_hash, status, created_at, updated_at)
            VALUES
                (:id, 'legacy@paperforge.local', 'Legacy administrator',
                 '!bootstrap-required', 'disabled', now(), now())
            """
        ).bindparams(sa.bindparam("id", value=LEGACY_USER_ID, type_=postgresql.UUID()))
    )
    op.alter_column(
        "paper_project",
        "owner_id",
        existing_type=sa.String(length=128),
        type_=postgresql.UUID(as_uuid=True),
        postgresql_using="NULLIF(owner_id, '')::uuid",
        nullable=True,
    )
    op.execute(
        sa.text("UPDATE paper_project SET owner_id = :owner WHERE owner_id IS NULL").bindparams(
            sa.bindparam("owner", value=LEGACY_USER_ID, type_=postgresql.UUID())
        )
    )
    op.alter_column("paper_project", "owner_id", existing_type=sa.UUID(), nullable=False)
    op.create_foreign_key(
        "fk_paper_project_owner_id_app_user",
        "paper_project",
        "app_user",
        ["owner_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_paper_project_owner_created", "paper_project", ["owner_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_paper_project_owner_created", table_name="paper_project")
    op.drop_constraint("fk_paper_project_owner_id_app_user", "paper_project", type_="foreignkey")
    op.alter_column("paper_project", "owner_id", existing_type=sa.UUID(), nullable=True)
    op.alter_column(
        "paper_project",
        "owner_id",
        existing_type=postgresql.UUID(as_uuid=True),
        type_=sa.String(length=128),
        postgresql_using="owner_id::text",
        nullable=True,
    )
    op.drop_index("ix_auth_action_token_expires_at", table_name="auth_action_token")
    op.drop_index("ix_auth_action_token_user_purpose", table_name="auth_action_token")
    op.drop_table("auth_action_token")
    op.drop_index("ix_user_session_expires_at", table_name="user_session")
    op.drop_index("ix_user_session_user_revoked", table_name="user_session")
    op.drop_index("ix_user_session_user_id", table_name="user_session")
    op.drop_table("user_session")
    op.drop_index("ix_app_user_email", table_name="app_user")
    op.drop_table("app_user")
