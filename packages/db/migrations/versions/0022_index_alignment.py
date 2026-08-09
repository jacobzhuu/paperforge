"""align index definitions with ORM models

Revision ID: 0022_index_alignment
Revises: 0021_question_locking
Create Date: 2026-08-09

`alembic check` 从未在 CI 里跑过（仓库此前没有版本控制），因此模型与迁移之间
积累了七处**只涉及索引**的漂移：没有表、列、类型、约束或默认值的差异，所以
不存在数据正确性风险，只影响查询计划与「从零 upgrade 出来的库是否可复现」。

逐项说明（括号内是漂移方向）：

1. `ix_claim_evidence_user_asset_id` →  `ix_claim_evidence_anchor_user_asset_id`
   （改名）模型在列上写了 `index=True`，SQLAlchemy 因此自动命名为
   `ix_<表名>_<列名>`；0006 建表时手写了较短的名字。同表其余六个索引都已经是
   `ix_claim_evidence_anchor_*`，所以这一个才是异类。用 ALTER INDEX RENAME 而不是
   autogenerate 建议的 drop+create：索引定义完全相同，重建没有意义，改名是原子的
   目录操作，不重扫表。

2. `ix_eligibility_decision_project_id`（新增）
3. `ix_eligibility_decision_work_id`（新增）
   0012 建表时只建了复合索引 `ix_eligibility_project_decision(project_id, decision)`
   与唯一约束 `uq_eligibility_project_work(project_id, work_id)`，但模型在两个外键列上
   都声明了 `index=True`。读路径上 project_id 已被上述两者的前缀覆盖，work_id 则完全
   没有索引；两列都是 CASCADE 外键，删除父行时的级联查找需要它们。

4. `ix_evidence_unit_task_topical(task_id, topical_status)`（删除）
5. `ix_evidence_unit_task_id(task_id)`（新增）
   0011 建了复合索引，模型只在 task_id 上写了 `index=True`，且 `__table_args__` 里
   没有声明这个复合索引。核对过全部查询：`topical_status` 从未出现在任何 WHERE 子句里，
   `EvidenceUnit.task_id` 也没有在 SQL 层过滤过——qmatrix 是把证据全量读出来后在
   Python 里按 task_id 筛的（见 `pipelines/qmatrix.py:_rank_candidates`）。因此这个复合
   索引服务不了任何查询，只在 EVIDENCE 阶段批量插入时增加写开销。删掉它、保留单列
   外键索引，既消除漂移也少一份写放大。

6. `ix_research_question_task_id`（新增）
   同 2/3：模型声明了 `index=True`，0011 没建。表很小（生产 52 行），索引本身开销
   可忽略，但保持模型与库一致比留一处「无害的漂移」更重要——漂移一旦被容忍，
   下一次真正有害的漂移就没人看得出来。

为什么不用 CREATE INDEX CONCURRENTLY：受影响四张表在生产上分别是 5190 / 5966 /
7328 / 52 行，普通 CREATE INDEX 是毫秒级；CONCURRENTLY 需要跳出事务
（autocommit_block），失败还会留下 INVALID 索引需要人工清理，对这个规模不值得。

所有语句都带 IF EXISTS / IF NOT EXISTS 守卫：蓝绿部署下可能存在被手工修过的库，
迁移不应该因为「要建的索引已经在了」而中断。
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0022_index_alignment"
down_revision: str | None = "0021_question_locking"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. 改名而非重建：定义相同，只有名字不符合模型的自动命名。
    op.execute(
        "ALTER INDEX IF EXISTS ix_claim_evidence_user_asset_id "
        "RENAME TO ix_claim_evidence_anchor_user_asset_id"
    )
    # 从零 upgrade 的库不会有旧名字，改名是空操作；这一条兜住那种情况。
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_claim_evidence_anchor_user_asset_id "
        "ON claim_evidence_anchor (user_asset_id)"
    )

    # 2 & 3. CASCADE 外键列缺索引。
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_eligibility_decision_project_id "
        "ON eligibility_decision (project_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_eligibility_decision_work_id "
        "ON eligibility_decision (work_id)"
    )

    # 4 & 5. 复合索引没有任何查询使用，换成模型声明的单列外键索引。
    op.execute("DROP INDEX IF EXISTS ix_evidence_unit_task_topical")
    op.execute("CREATE INDEX IF NOT EXISTS ix_evidence_unit_task_id ON evidence_unit (task_id)")

    # 6.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_research_question_task_id ON research_question (task_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_research_question_task_id")
    op.execute("DROP INDEX IF EXISTS ix_evidence_unit_task_id")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_evidence_unit_task_topical "
        "ON evidence_unit (task_id, topical_status)"
    )
    op.execute("DROP INDEX IF EXISTS ix_eligibility_decision_work_id")
    op.execute("DROP INDEX IF EXISTS ix_eligibility_decision_project_id")
    op.execute(
        "ALTER INDEX IF EXISTS ix_claim_evidence_anchor_user_asset_id "
        "RENAME TO ix_claim_evidence_user_asset_id"
    )
