"""record which extractor produced each experiment dimension

Revision ID: 0023_experiment_extraction
Revises: 0022_index_alignment
Create Date: 2026-08-09

P0-3 第一步（docs/plans/PAPERFORGE_P0_REMEDIATION_PLAN.md Phase 2）。

在引入 LLM 结构化抽取之前，先让每一行实验结果都能回答两个问题：

* `extraction_source`——这一格的维度是正则读出来的、模型读出来的，还是两者一致；
* `locator_verified`——它的 `source_location` 是否真的落在本文档的某个
  `DocumentChunk` 上。模型可以凭空写出「p.7, table:3」，但如果本文档解析后
  根本没有第 7 页或 table:3，这条定位就是假的。未核验的单元格照常入库
  （诊断需要看到它），但不得进入跨研究比较。

`extraction_conflict_json` 留给两条路径对同一格给出不同维度的情况：保留模型那一版
（它读得到协议章节），同时把两版都记下来，让「为什么这个 dataset 变了」可复核。

全部字段可空或带 server default，蓝绿部署下正在排空的旧 worker 依旧可以 INSERT。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0023_experiment_extraction"
down_revision: str | None = "0022_index_alignment"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "experiment_result",
        sa.Column("extraction_source", sa.String(24), nullable=True),
    )
    op.add_column(
        "experiment_result",
        sa.Column(
            "locator_verified",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "experiment_result",
        sa.Column("extraction_conflict_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "evidence_measurement",
        sa.Column("extraction_source", sa.String(24), nullable=True),
    )
    op.add_column(
        "evidence_measurement",
        sa.Column(
            "locator_verified",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    # 影子模式下要按来源统计核验率，这个组合是唯一的查询形状。
    op.create_index(
        "ix_experiment_result_extraction_source",
        "experiment_result",
        ["structured_extraction_id", "extraction_source"],
    )
    # 历史行都来自正则通道；留空会让影子期的统计把它们算成「未知来源」。
    op.execute("UPDATE experiment_result SET extraction_source = 'regex' WHERE extraction_source IS NULL")
    op.execute(
        "UPDATE evidence_measurement SET extraction_source = 'regex' WHERE extraction_source IS NULL"
    )


def downgrade() -> None:
    op.drop_index("ix_experiment_result_extraction_source", table_name="experiment_result")
    op.drop_column("evidence_measurement", "locator_verified")
    op.drop_column("evidence_measurement", "extraction_source")
    op.drop_column("experiment_result", "extraction_conflict_json")
    op.drop_column("experiment_result", "locator_verified")
    op.drop_column("experiment_result", "extraction_source")
