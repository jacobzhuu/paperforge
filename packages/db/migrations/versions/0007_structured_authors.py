"""add structured project authors and reusable academic profile

Revision ID: 0007_structured_authors
Revises: 0006_submission_quality
Create Date: 2026-07-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_structured_authors"
down_revision: str | None = "0006_submission_quality"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "app_user",
        sa.Column("academic_profile_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "paper_project",
        sa.Column("author_details_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    # 原数组按原顺序无损转成结构化快照。legacy-* 只在单个项目内作为稳定 UI key，
    # 不承担全局身份含义。
    op.execute(
        """
        UPDATE paper_project AS project
        SET author_details_json = converted.details
        FROM (
            SELECT source.id,
                   jsonb_agg(
                       jsonb_build_object(
                           'id', 'legacy-' || item.ordinality::text,
                           'name', item.name,
                           'affiliations', '[]'::jsonb,
                           'email', NULL,
                           'orcid', NULL,
                           'corresponding', FALSE
                       )
                       ORDER BY item.ordinality
                   ) AS details
            FROM paper_project AS source,
                 jsonb_array_elements_text(
                     CASE
                         WHEN jsonb_typeof(source.authors_json) = 'array'
                         THEN source.authors_json
                         ELSE '[]'::jsonb
                     END
                 )
                 WITH ORDINALITY AS item(name, ordinality)
            GROUP BY source.id
        ) AS converted
        WHERE project.id = converted.id
        """
    )


def downgrade() -> None:
    op.drop_column("paper_project", "author_details_json")
    op.drop_column("app_user", "academic_profile_json")
