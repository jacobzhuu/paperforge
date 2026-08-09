"""move model/representation/backbone/split vocabulary into task_definition

Revision ID: 0024_task_vocabulary
Revises: 0023_experiment_extraction
Create Date: 2026-08-09

P0-5 / R16。`evidence.py` 里还硬编码着五组领域词表——模型族、输入表征、预训练骨干、
划分策略、任务变体——它们只覆盖两个领域（BGC 蛋白建模与序列推荐）。对化学、临床、
材料或社科的论文，这些字段要么恒为空，要么给出**错误**的匹配（比如任何提到
"BERT" 的文本都会被填成 pretrained_backbone=BERT）。

词表搬进 `task_definition.vocabulary_json`，形状：

    {
      "model_families":        ["CNN", "SASRec", ...],
      "input_representations": ["item sequence", ...],
      "pretrained_backbones":  ["ESM2", ...],
      "split_strategies":      [{"cue": "leave-one-out", "name": "leave-one-out"}],
      "task_variants":         [{"cue": "multi-class", "name": "multi-class"}]
    }

`split_strategies` / `task_variants` 用 (cue, name) 对而不是裸字符串：原实现是
「命中提示词 → 归一到规范名」，比如 "temporal split" → "temporal"。列表顺序即匹配
优先级，与被替换的 if 链保持一致，否则同一篇论文的归一结果会变。

同时插入 `generic.scholarly`：**空词表、空数据集、空指标白名单**。它是未绑定任务的
项目的新默认（由 TASK_PROFILE_FALLBACK 控制切换）。空词表意味着这些字段留空——
对领域外论文来说，留空严格优于填错。

注意：这里是 13 行任务（bgc 7 + recsys_attack 6），不是计划里写的 7 行。
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0024_task_vocabulary"
down_revision: str | None = "0023_experiment_extraction"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# 通用机器学习架构在两个领域里都会出现，因此两边都声明。词表的语义是
# 「**这个任务的论文预计会报告什么**」，不是「世界上存在什么」——所以重复是正确的，
# 而让 generic.scholarly 继承它们才是错的。
_SHARED_ARCHITECTURES = ["CNN", "BiLSTM", "LSTM", "GRU", "Transformer", "GNN", "CRF"]

# 顺序即优先级，与被替换的 `_task_variant` if 链逐条对应。
_SHARED_VARIANTS = [
    {"cue": "multi-class", "name": "multi-class"},
    {"cue": "multiclass", "name": "multi-class"},
    {"cue": "binary", "name": "binary detection"},
    {"cue": "detection", "name": "binary detection"},
    {"cue": "targeted", "name": "targeted attack"},
]

BGC_VOCABULARY = {
    "model_families": [*_SHARED_ARCHITECTURES, "BERT"],
    "input_representations": ["nucleotide", "amino acid", "Pfam domain", "domain graph"],
    "pretrained_backbones": ["ESM2", "ProtBERT", "DNABERT", "BERT", "RoBERTa"],
    # 与 `_split_strategy` 原有的 if 顺序一致：专用切分优先于通用切分。
    "split_strategies": [
        {"cue": "leave-one-genome-out", "name": "leave-one-genome-out"},
        {"cue": "cluster-based", "name": "cluster-based"},
        {"cue": "temporal split", "name": "temporal"},
        {"cue": "random split", "name": "random"},
    ],
    "task_variants": _SHARED_VARIANTS,
}

RECSYS_VOCABULARY = {
    "model_families": ["SASRec", "BERT4Rec", "GRU4Rec", *_SHARED_ARCHITECTURES],
    "input_representations": ["item sequence", "user sequence"],
    "pretrained_backbones": ["BERT", "RoBERTa"],
    "split_strategies": [
        {"cue": "leave-one-genome-out", "name": "leave-one-genome-out"},
        {"cue": "cluster-based", "name": "cluster-based"},
        {"cue": "temporal split", "name": "temporal"},
        {"cue": "random split", "name": "random"},
    ],
    "task_variants": _SHARED_VARIANTS,
}

GENERIC_SLUG = "generic.scholarly"


def upgrade() -> None:
    op.add_column(
        "task_definition",
        sa.Column("vocabulary_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    for domain, vocabulary in (("bgc", BGC_VOCABULARY), ("recsys_attack", RECSYS_VOCABULARY)):
        op.execute(
            sa.text(
                "UPDATE task_definition SET vocabulary_json = CAST(:payload AS jsonb) "
                "WHERE domain = :domain"
            ).bindparams(payload=json.dumps(vocabulary, ensure_ascii=False), domain=domain)
        )
    # 任何未来新增的领域先拿到空词表而不是 NULL，读取端因此不必区分两者。
    op.execute("UPDATE task_definition SET vocabulary_json = '{}'::jsonb WHERE vocabulary_json IS NULL")

    op.execute(
        sa.text(
            """
            INSERT INTO task_definition (
                slug, domain, label_i18n_json, metric_whitelist_json, dataset_whitelist_json,
                dimension_schema_json, exclusion_cues_json, inclusion_cues_json, vocabulary_json,
                created_at, updated_at
            ) VALUES (
                :slug, 'generic', CAST(:labels AS jsonb), '[]'::jsonb, '[]'::jsonb,
                CAST(:dimensions AS jsonb), '[]'::jsonb, '[]'::jsonb, '{}'::jsonb,
                now(), now()
            )
            ON CONFLICT (slug) DO NOTHING
            """
        ).bindparams(
            slug=GENERIC_SLUG,
            labels=json.dumps({"en": "General scholarly work", "zh": "通用学术研究"}),
            dimensions=json.dumps(["dataset", "metric_name", "split"]),
        )
    )


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM task_definition WHERE slug = :slug").bindparams(slug=GENERIC_SLUG))
    op.drop_column("task_definition", "vocabulary_json")
