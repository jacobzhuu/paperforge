"""add private literature PDF uploads and project-scoped document access

Revision ID: 0015_private_literature_pdfs
Revises: 0014_cross_lang_routing
Create Date: 2026-07-31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015_private_literature_pdfs"
down_revision: str | None = "0014_cross_lang_routing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    ]


def upgrade() -> None:
    op.add_column(
        "library_entry",
        sa.Column(
            "literature_role",
            sa.String(32),
            server_default=sa.text("'general'"),
            nullable=False,
        ),
    )
    # Preserve the only existing user-authored importance signal.
    op.execute(
        sa.text("UPDATE library_entry SET literature_role = 'core' WHERE user_pinned IS TRUE")
    )
    op.create_check_constraint(
        "ck_library_entry_literature_role",
        "library_entry",
        "literature_role IN ('general','core','background','method','benchmark','controversy')",
    )

    op.add_column(
        "document_file",
        sa.Column("project_id", sa.UUID(), nullable=True),
    )
    op.add_column(
        "document_file",
        sa.Column(
            "access_scope",
            sa.String(16),
            server_default=sa.text("'shared'"),
            nullable=False,
        ),
    )
    op.create_foreign_key(
        "fk_document_file_project",
        "document_file",
        "paper_project",
        ["project_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_document_file_project_id",
        "document_file",
        ["project_id"],
    )
    op.create_check_constraint(
        "ck_document_file_access_scope_project",
        "document_file",
        "(access_scope = 'shared' AND project_id IS NULL) OR "
        "(access_scope = 'private' AND project_id IS NOT NULL)",
    )
    op.drop_constraint(
        "uq_document_file_work_content",
        "document_file",
        type_="unique",
    )
    op.create_index(
        "uq_document_file_shared_work_content",
        "document_file",
        ["work_id", "content_hash"],
        unique=True,
        postgresql_where=sa.text("access_scope = 'shared'"),
    )
    op.create_index(
        "uq_document_file_private_project_work_content",
        "document_file",
        ["project_id", "work_id", "content_hash"],
        unique=True,
        postgresql_where=sa.text("access_scope = 'private'"),
    )

    op.create_table(
        "literature_pdf_upload",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("object_key", sa.String(512), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column(
            "mime",
            sa.String(128),
            server_default=sa.text("'application/pdf'"),
            nullable=False,
        ),
        sa.Column("bytes", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column(
            "status",
            sa.String(32),
            server_default=sa.text("'matching'"),
            nullable=False,
        ),
        sa.Column(
            "extracted_metadata_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "match_candidates_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("matched_work_id", sa.UUID(), nullable=True),
        sa.Column("document_file_id", sa.UUID(), nullable=True),
        sa.Column("match_method", sa.String(64), nullable=True),
        sa.Column("match_confidence", sa.Float(), nullable=True),
        sa.Column(
            "match_metadata_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "error_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN "
            "('matching','needs_confirmation','match_failed','parsing','extracting',"
            "'ready','parse_failed','rejected')",
            name="ck_literature_pdf_upload_status",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["paper_project.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["matched_work_id"],
            ["scholarly_work.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["document_file_id"],
            ["document_file.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "object_key",
            name="uq_literature_pdf_upload_object_key",
        ),
    )
    op.create_index(
        "ix_literature_pdf_upload_project_id",
        "literature_pdf_upload",
        ["project_id"],
    )
    op.create_index(
        "ix_literature_pdf_upload_content_hash",
        "literature_pdf_upload",
        ["content_hash"],
    )
    op.create_index(
        "ix_literature_pdf_upload_matched_work_id",
        "literature_pdf_upload",
        ["matched_work_id"],
    )
    op.create_index(
        "ix_literature_pdf_upload_document_file_id",
        "literature_pdf_upload",
        ["document_file_id"],
    )
    op.create_index(
        "ix_literature_pdf_upload_project_status_created",
        "literature_pdf_upload",
        ["project_id", "status", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("literature_pdf_upload")

    op.drop_index(
        "uq_document_file_private_project_work_content",
        table_name="document_file",
    )
    op.drop_index(
        "uq_document_file_shared_work_content",
        table_name="document_file",
    )
    # This intentionally fails rather than discarding data if an installation
    # has private rows whose (work_id, content_hash) collide across projects.
    op.create_unique_constraint(
        "uq_document_file_work_content",
        "document_file",
        ["work_id", "content_hash"],
    )
    op.drop_constraint(
        "ck_document_file_access_scope_project",
        "document_file",
        type_="check",
    )
    op.drop_index("ix_document_file_project_id", table_name="document_file")
    op.drop_constraint(
        "fk_document_file_project",
        "document_file",
        type_="foreignkey",
    )
    op.drop_column("document_file", "access_scope")
    op.drop_column("document_file", "project_id")

    op.drop_constraint(
        "ck_library_entry_literature_role",
        "library_entry",
        type_="check",
    )
    op.drop_column("library_entry", "literature_role")
