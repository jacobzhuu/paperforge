"""keep the sentences the writing rules removed, and why

Revision ID: 0029_sentence_downgrade
Revises: 0028_llm_request_shape
Create Date: 2026-08-24

The sentence-level evidence rules delete prose.  R6 alone removed every numeric
sentence bound to evidence it judged unlocated, and `enforce_sentence_evidence_rules`
did collect the discards onto `paragraph["downgraded_sentences"]` — but
`to_ir_section()` only ever reads `paragraph["sentences"]`, so the trail died at
the Draft-to-IR boundary.  Across 434 delivered production sections, zero carried
any record of a removal.  The comment on that code claims it keeps an audit trail
"for quality/claim review"; nothing downstream could read one.

The asset-grounding rule was worse: it `continue`d past the sentence without even
collecting it.

One row per removed or rewritten sentence, written in the same transaction as the
section it came from, and replaced wholesale when that section is rewritten — the
same lifecycle `citation_usage` already has.  `text` is the sentence as the model
wrote it, captured before the rule blanks it.

`job_id` is nullable because the writing stage runs without a job in tests and in
the shadow evaluation harness.  Every other column is nullable-or-defaulted so a
draining old worker under blue/green can keep INSERTing into the tables it knows.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0029_sentence_downgrade"
down_revision = "0028_llm_request_shape"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sentence_downgrade",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id",
            UUID(as_uuid=True),
            sa.ForeignKey("paper_project.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "job_id",
            UUID(as_uuid=True),
            sa.ForeignKey("generation_job.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "document_id",
            UUID(as_uuid=True),
            sa.ForeignKey("paper_document.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "section_id",
            UUID(as_uuid=True),
            sa.ForeignKey("paper_section.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("section_key", sa.String(length=64), nullable=False),
        sa.Column("paragraph_index", sa.Integer(), nullable=False),
        sa.Column("sentence_index", sa.Integer(), nullable=False),
        sa.Column("rule", sa.String(length=48), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False, server_default="removed"),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("cite_keys_json", JSONB(), nullable=True),
        sa.Column("evidence_ids_json", JSONB(), nullable=True),
        sa.Column("locator_status", sa.String(length=24), nullable=False, server_default="unknown"),
        sa.Column("locators_json", JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    # 按章节汇总（质检报告的每节明细）与按整篇汇总（走 document_id）都要快。
    op.create_index(
        "ix_sentence_downgrade_section", "sentence_downgrade", ["section_id"]
    )
    op.create_index(
        "ix_sentence_downgrade_document_rule", "sentence_downgrade", ["document_id", "rule"]
    )
    op.create_index(
        "ix_sentence_downgrade_project", "sentence_downgrade", ["project_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_sentence_downgrade_project", table_name="sentence_downgrade")
    op.drop_index("ix_sentence_downgrade_document_rule", table_name="sentence_downgrade")
    op.drop_index("ix_sentence_downgrade_section", table_name="sentence_downgrade")
    op.drop_table("sentence_downgrade")
