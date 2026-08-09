"""add cross-language routing fields and recsys_attack task seeds

Revision ID: 0014_cross_lang_routing
Revises: 0013_experiment_v2_fields
Create Date: 2026-07-31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014_cross_lang_routing"
down_revision: str | None = "0013_experiment_v2_fields"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("research_question", sa.Column("search_query", sa.Text(), nullable=True))
    op.add_column(
        "research_question",
        sa.Column("term_aliases_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "question_evidence_link",
        sa.Column("routing_mode", sa.String(32), nullable=True),
    )
    op.add_column(
        "question_evidence_link",
        sa.Column("bridge_source", sa.String(32), nullable=True),
    )
    op.add_column(
        "task_definition",
        sa.Column("inclusion_cues_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )

    # Backfill BGC inclusion cues so topical status can be data-driven.
    op.execute(
        sa.text(
            """
            UPDATE task_definition
            SET inclusion_cues_json = CAST(
              '["biosynthetic gene cluster","bgc","secondary metabolite","antiSMASH","DeepBGC","MIBiG"]'
              AS jsonb
            )
            WHERE domain = 'bgc'
            """
        )
    )

    task_table = sa.table(
        "task_definition",
        sa.column("slug", sa.String),
        sa.column("domain", sa.String),
        sa.column("label_i18n_json", postgresql.JSONB),
        sa.column("metric_whitelist_json", postgresql.JSONB),
        sa.column("dataset_whitelist_json", postgresql.JSONB),
        sa.column("dimension_schema_json", postgresql.JSONB),
        sa.column("exclusion_cues_json", postgresql.JSONB),
        sa.column("inclusion_cues_json", postgresql.JSONB),
    )
    recsys_tasks = [
        {
            "slug": "recsys.poisoning_attack",
            "domain": "recsys_attack",
            "label_i18n_json": {
                "en": "Sequential recommendation poisoning attack",
                "zh": "序列推荐投毒攻击",
            },
            "metric_whitelist_json": [
                "HR@K",
                "NDCG@K",
                "MRR",
                "ASR",
                "ER@K",
                "Recall@K",
                "precision",
                "recall",
                "F1",
            ],
            "dataset_whitelist_json": [
                "MovieLens-1M",
                "MovieLens-20M",
                "Amazon Beauty",
                "Amazon Books",
                "Amazon Games",
                "Steam",
                "Yelp",
                "LastFM",
            ],
            "dimension_schema_json": [
                "dataset",
                "attack_goal",
                "threat_model",
                "attack_budget",
                "metric_name",
            ],
            "exclusion_cues_json": [
                "intrusion detection",
                "histopathology",
                "blockchain",
                "prompt injection",
                "image quality assessment",
                "breast cancer",
                "financial fraud",
                "IoT vision",
            ],
            "inclusion_cues_json": [
                "sequential recommendation",
                "session-based recommendation",
                "poisoning attack",
                "fake user",
                "profile pollution",
            ],
        },
        {
            "slug": "recsys.profile_pollution",
            "domain": "recsys_attack",
            "label_i18n_json": {
                "en": "Profile pollution / fake-user injection",
                "zh": "用户画像污染与虚假用户注入",
            },
            "metric_whitelist_json": ["HR@K", "NDCG@K", "MRR", "ASR", "ER@K", "Recall@K"],
            "dataset_whitelist_json": [
                "MovieLens-1M",
                "MovieLens-20M",
                "Amazon Beauty",
                "Amazon Books",
                "Steam",
                "Yelp",
                "LastFM",
            ],
            "dimension_schema_json": [
                "dataset",
                "attack_goal",
                "threat_model",
                "attack_budget",
                "metric_name",
            ],
            "exclusion_cues_json": [
                "intrusion detection",
                "histopathology",
                "blockchain",
                "prompt injection",
            ],
            "inclusion_cues_json": [
                "profile pollution",
                "fake user",
                "injected profile",
                "sequential recommendation",
            ],
        },
        {
            "slug": "recsys.model_extraction",
            "domain": "recsys_attack",
            "label_i18n_json": {
                "en": "Recommendation model extraction",
                "zh": "推荐模型窃取",
            },
            "metric_whitelist_json": ["HR@K", "NDCG@K", "MRR", "ASR", "accuracy", "F1"],
            "dataset_whitelist_json": [
                "MovieLens-1M",
                "MovieLens-20M",
                "Amazon Beauty",
                "Steam",
                "Yelp",
            ],
            "dimension_schema_json": ["dataset", "threat_model", "metric_name"],
            "exclusion_cues_json": ["blockchain", "prompt injection", "intrusion detection"],
            "inclusion_cues_json": [
                "model extraction",
                "steal recommendation",
                "sequential recommendation",
            ],
        },
        {
            "slug": "recsys.adversarial_defense",
            "domain": "recsys_attack",
            "label_i18n_json": {
                "en": "Adversarial defense for recommenders",
                "zh": "推荐系统对抗防御",
            },
            "metric_whitelist_json": [
                "HR@K",
                "NDCG@K",
                "MRR",
                "ASR",
                "detection rate",
                "F1",
                "precision",
                "recall",
            ],
            "dataset_whitelist_json": [
                "MovieLens-1M",
                "MovieLens-20M",
                "Amazon Beauty",
                "Amazon Books",
                "Steam",
                "Yelp",
            ],
            "dimension_schema_json": ["dataset", "threat_model", "metric_name"],
            "exclusion_cues_json": [
                "intrusion detection",
                "IoT vision",
                "breast cancer",
                "blockchain",
            ],
            "inclusion_cues_json": [
                "adversarial defense",
                "robust recommendation",
                "poison detection",
                "sequential recommendation",
            ],
        },
        {
            "slug": "recsys.robust_training",
            "domain": "recsys_attack",
            "label_i18n_json": {
                "en": "Robust training for sequential recommenders",
                "zh": "序列推荐鲁棒训练",
            },
            "metric_whitelist_json": ["HR@K", "NDCG@K", "MRR", "ASR", "Recall@K"],
            "dataset_whitelist_json": [
                "MovieLens-1M",
                "MovieLens-20M",
                "Amazon Beauty",
                "Steam",
                "Yelp",
            ],
            "dimension_schema_json": ["dataset", "threat_model", "metric_name"],
            "exclusion_cues_json": ["intrusion detection", "blockchain", "prompt injection"],
            "inclusion_cues_json": [
                "robust training",
                "adversarial training",
                "sequential recommendation",
            ],
        },
        {
            "slug": "recsys.benchmark_dataset",
            "domain": "recsys_attack",
            "label_i18n_json": {
                "en": "Attack evaluation datasets and metrics",
                "zh": "攻击评估数据集与指标",
            },
            "metric_whitelist_json": ["HR@K", "NDCG@K", "MRR", "ASR", "ER@K", "Recall@K"],
            "dataset_whitelist_json": [
                "MovieLens-1M",
                "MovieLens-20M",
                "Amazon Beauty",
                "Amazon Books",
                "Amazon Games",
                "Steam",
                "Yelp",
                "LastFM",
            ],
            "dimension_schema_json": ["dataset", "metric_name", "split_strategy"],
            "exclusion_cues_json": ["histopathology", "blockchain", "image quality assessment"],
            "inclusion_cues_json": [
                "MovieLens",
                "Amazon Beauty",
                "attack success rate",
                "NDCG",
                "sequential recommendation",
            ],
        },
    ]
    op.bulk_insert(task_table, recsys_tasks)


def downgrade() -> None:
    for slug in (
        "recsys.poisoning_attack",
        "recsys.profile_pollution",
        "recsys.model_extraction",
        "recsys.adversarial_defense",
        "recsys.robust_training",
        "recsys.benchmark_dataset",
    ):
        op.execute(sa.text("DELETE FROM task_definition WHERE slug = :slug").bindparams(slug=slug))
    op.drop_column("task_definition", "inclusion_cues_json")
    op.drop_column("question_evidence_link", "bridge_source")
    op.drop_column("question_evidence_link", "routing_mode")
    op.drop_column("research_question", "term_aliases_json")
    op.drop_column("research_question", "search_query")
