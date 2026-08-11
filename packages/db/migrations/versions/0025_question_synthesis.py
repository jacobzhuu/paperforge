"""narrative cross-study synthesis per sub-question

Revision ID: 0025_question_synthesis
Revises: 0024_task_vocabulary
Create Date: 2026-08-12

P0-4 / Phase 4。SYNTH 目前是数立场：同一 ``comparability_key`` 下出现 supports 与
contradicts 就叫 conflicting，否则 consistent。它给不出「在什么条件下成立、为什么
不一致」，正文因此只能罗列文献。

这张表存放**增补的**叙述性综合：claim / agreement / conditional / conflict / gap。
确定性 bundle 仍是权威结构——``answer_status``、证据归属、``comparison_clusters``
一个都不由本表改写，于是 readiness 与写作前置门禁的输入与开关状态无关。

``bundle_hash`` 是幂等键，唯一索引落在 (research_question_id, bundle_hash) 上：
完整流水线里 SYNTH 最多跑 4 次（初次 + 两轮补充 + PDF 上传刷新），bundle 没变就命中
既有行，一次调用都不额外花。bundle 变了则写新行，旧行留作历史。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0025_question_synthesis"
down_revision = "0024_task_vocabulary"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "question_synthesis",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "research_question_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("research_question.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("paper_project.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # 'llm:<model>' | 'deterministic'
        sa.Column("generator", sa.String(length=128), nullable=False),
        sa.Column("bundle_hash", sa.String(length=64), nullable=False),
        sa.Column("claim", sa.Text(), nullable=True),
        sa.Column("agreement_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("conditional_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("conflict_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("gap_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_question_synthesis_research_question_id",
        "question_synthesis",
        ["research_question_id"],
    )
    op.create_index(
        "ix_question_synthesis_project_id",
        "question_synthesis",
        ["project_id"],
    )
    op.create_unique_constraint(
        "uq_question_synthesis_bundle",
        "question_synthesis",
        ["research_question_id", "bundle_hash"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_question_synthesis_bundle", "question_synthesis", type_="unique")
    op.drop_index("ix_question_synthesis_project_id", table_name="question_synthesis")
    op.drop_index("ix_question_synthesis_research_question_id", table_name="question_synthesis")
    op.drop_table("question_synthesis")
