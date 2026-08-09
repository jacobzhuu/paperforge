"""领域词表本体化（P0-5 / R16）。

核心断言有两条：

* **等价性**——对声明了词表的任务，重构后的 `_structured_payload` 与重构前逐字段相同；
* **领域外留空**——对空词表（generic.scholarly），这些字段留空，而不是像以前那样
  把任何提到 "BERT" 的化学论文标成 pretrained_backbone=BERT。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from db.repositories.tasks import (
    GENERIC_TASK_SLUG,
    TaskSpec,
    TaskVocabulary,
    compile_dataset_pattern,
    compile_metric_name_pattern,
    compile_metric_pattern,
    metric_alternation,
    task_ontology_warnings,
    task_spec_to_payload,
    validate_task_payloads,
    vocabulary_for_tasks,
)
from paperforge_worker.pipelines.evidence import _structured_payload

ROOT = Path(__file__).resolve().parents[3]
SEED_FILE = ROOT / "packages/db/db/seeds/task_definitions.json"

# 与迁移 0024 回填给 bgc 域的词表一致。等价性测试靠它复现重构前的行为。
BGC_VOCABULARY = TaskVocabulary(
    model_families=("CNN", "BiLSTM", "LSTM", "GRU", "Transformer", "GNN", "CRF", "BERT"),
    input_representations=("nucleotide", "amino acid", "Pfam domain", "domain graph"),
    pretrained_backbones=("ESM2", "ProtBERT", "DNABERT", "BERT", "RoBERTa"),
    split_strategies=(
        ("leave-one-genome-out", "leave-one-genome-out"),
        ("cluster-based", "cluster-based"),
        ("temporal split", "temporal"),
        ("random split", "random"),
    ),
    task_variants=(
        ("multi-class", "multi-class"),
        ("multiclass", "multi-class"),
        ("binary", "binary detection"),
        ("detection", "binary detection"),
        ("targeted", "targeted attack"),
    ),
)

BGC_TASK = TaskSpec(
    slug="bgc.identification",
    domain="bgc",
    labels={"en": "BGC identification"},
    metrics=("AUROC", "MCC"),
    datasets=("MIBiG",),
    inclusion_cues=("biosynthetic gene cluster", "bgc"),
    vocabulary=BGC_VOCABULARY,
)

GENERIC_TASK = TaskSpec(
    slug=GENERIC_TASK_SLUG,
    domain="generic",
    labels={"en": "General scholarly work"},
    dimensions=("dataset", "metric_name", "split"),
)

BGC_TEXT = (
    "A BGC identification Transformer used a cluster-based split with a random split fallback. "
    "The multi-class classifier encodes each amino acid sequence with an ESM2-650M backbone "
    "and a domain graph. We trained with AdamW, learning rate 0.001, batch size 32, 20 epochs."
)

CHEMISTRY_TEXT = (
    "We model reaction yields with a gradient-boosted regressor over Morgan fingerprints. "
    "BERT is mentioned only as related work in natural language processing. "
    "Training used AdamW with learning rate 0.001 over 20 epochs."
)


def _payload(text: str, tasks: list[TaskSpec]) -> dict:
    return _structured_payload(
        fulltext=text,
        structured_objects=[],
        datasets=[],
        metrics=[],
        records=[],
        tasks=tasks,
        dataset_pattern=compile_dataset_pattern([name for t in tasks for name in t.datasets]),
    )


# --- 等价性：声明了词表的任务，行为与重构前一致 ----------------------------


def test_declared_vocabulary_reproduces_the_previous_hardcoded_behaviour() -> None:
    payload = _payload(BGC_TEXT, [BGC_TASK])
    # 这些正是重构前那几条硬编码正则会给出的结果。
    assert payload["model_families"] == ["Transformer"]
    assert payload["input_representations"] == ["amino acid", "domain graph"]
    assert payload["pretrained_backbone"] == "ESM2-650M"
    assert payload["split_strategy"] == "cluster-based"
    assert payload["task_variant"] == "multi-class"


def test_split_strategy_priority_follows_list_order() -> None:
    """cluster-based 排在 random split 之前，两者同时出现时前者胜出。"""
    both = _payload("We used a cluster-based split and a random split.", [BGC_TASK])
    assert both["split_strategy"] == "cluster-based"
    only_random = _payload("We used a random split.", [BGC_TASK])
    assert only_random["split_strategy"] == "random"


def test_backbone_version_suffix_is_preserved() -> None:
    """词表里存裸名 ESM2，正文里的 ESM2-650M 仍要被完整识别。"""
    assert _payload("Encoded with ESM2-650M.", [BGC_TASK])["pretrained_backbone"] == "ESM2-650M"
    assert _payload("Encoded with ESM2.", [BGC_TASK])["pretrained_backbone"] == "ESM2"


# --- 领域外：空词表留空，而不是填错 ----------------------------------------


def test_empty_vocabulary_leaves_fields_empty_instead_of_guessing() -> None:
    payload = _payload(CHEMISTRY_TEXT, [GENERIC_TASK])
    assert payload["model_families"] == []
    assert payload["input_representations"] == []
    # 重构前这里会是 "BERT"，仅仅因为相关工作里提了一句。
    assert payload["pretrained_backbone"] is None
    assert payload["split_strategy"] is None
    assert payload["task_variant"] is None
    # 通用训练超参不属于领域词表，仍然照常抽取。
    assert payload["optimizer"] == "AdamW"
    assert payload["learning_rate"] == 0.001
    assert payload["epochs"] == 20


def test_no_tasks_at_all_is_not_a_crash() -> None:
    payload = _payload(BGC_TEXT, [])
    assert payload["model_families"] == []
    assert payload["pretrained_backbone"] is None


def test_baselines_field_is_gone() -> None:
    """曾经的 `[A-Z][A-Za-z0-9+-]{2,24}` 会把 The/We/Table 当成基线方法。"""
    assert "baselines" not in _payload(BGC_TEXT, [BGC_TASK])


# --- vocabulary_for_tasks ---------------------------------------------------


def test_merging_tasks_deduplicates_and_preserves_order() -> None:
    first = TaskSpec(
        slug="a.one",
        domain="d",
        vocabulary=TaskVocabulary(
            model_families=("CNN", "GRU"),
            split_strategies=(("temporal split", "temporal"),),
        ),
    )
    second = TaskSpec(
        slug="a.two",
        domain="d",
        vocabulary=TaskVocabulary(
            model_families=("GRU", "SASRec"),
            split_strategies=(("random split", "random"), ("temporal split", "ignored")),
        ),
    )
    merged = vocabulary_for_tasks([first, second])
    assert merged.model_families == ("CNN", "GRU", "SASRec")
    # 先声明者定义规范名；后来的同 cue 不覆盖它。
    assert merged.split_strategies == (
        ("temporal split", "temporal"),
        ("random split", "random"),
    )


def test_empty_task_list_yields_an_empty_vocabulary() -> None:
    assert vocabulary_for_tasks([]).empty is True


def test_malformed_vocabulary_degrades_to_empty() -> None:
    from db.repositories.tasks import _vocabulary_from_row

    assert _vocabulary_from_row(None).empty is True
    assert _vocabulary_from_row("not an object").empty is True
    assert _vocabulary_from_row({"model_families": "not a list"}).model_families == ()
    # 结构坏掉的 cue 条目跳过，不影响同列表里合法的条目。
    partial = _vocabulary_from_row({"split_strategies": [{"cue": "ok"}, 42, {"name": "no cue"}]})
    assert partial.split_strategies == (("ok", "ok"),)


# --- 指标词表：领域词只能来自本体 -------------------------------------------


def test_universal_core_excludes_domain_metrics() -> None:
    universal = metric_alternation([])
    for domain_metric in ("tanimoto", "ndcg", "asr"):
        assert domain_metric not in universal.casefold()
    for shared in ("accuracy", "precision", "auroc", "rmse"):
        assert shared in universal.casefold()


def test_ontology_metrics_extend_the_pattern_both_ways() -> None:
    prose = compile_metric_pattern(["NDCG@K"])
    names = compile_metric_name_pattern(["NDCG@K"])
    assert prose.search("NDCG@10 = 0.41") is not None
    assert names.search("ndcg@10") is not None
    # @K 展开成两条：带下标和不带下标都要认。
    assert names.search("ndcg") is not None


# --- 本体作者化 -------------------------------------------------------------


def test_seed_file_is_committed_and_valid() -> None:
    assert SEED_FILE.exists(), "the canonical ontology seed must be committed"
    payloads = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    assert validate_task_payloads(payloads) == []
    slugs = {item["slug"] for item in payloads}
    assert GENERIC_TASK_SLUG in slugs


def test_seed_file_round_trips_through_the_spec_projection() -> None:
    payloads = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    for item in payloads:
        spec = TaskSpec(
            slug=item["slug"],
            domain=item["domain"],
            labels=item["labels"],
            metrics=tuple(item["metrics"]),
            datasets=tuple(item["datasets"]),
            dimensions=tuple(item["dimensions"]),
            inclusion_cues=tuple(item["inclusion_cues"]),
            exclusion_cues=tuple(item["exclusion_cues"]),
            vocabulary=TaskVocabulary(
                model_families=tuple(item["vocabulary"]["model_families"]),
                input_representations=tuple(item["vocabulary"]["input_representations"]),
                pretrained_backbones=tuple(item["vocabulary"]["pretrained_backbones"]),
                split_strategies=tuple(
                    (entry["cue"], entry["name"])
                    for entry in item["vocabulary"]["split_strategies"]
                ),
                task_variants=tuple(
                    (entry["cue"], entry["name"]) for entry in item["vocabulary"]["task_variants"]
                ),
            ),
        )
        assert task_spec_to_payload(spec) == item


def test_validation_rejects_structural_errors() -> None:
    errors = validate_task_payloads(
        [
            {"slug": "Bad Slug", "domain": "d", "labels": {"en": "x"}},
            {"slug": "ok.one", "domain": "", "labels": {}},
            {"slug": "ok.one", "domain": "d", "labels": {"en": "x"}},
            {"slug": "ok.two", "domain": "d", "labels": {"en": "x"}, "metrics": ["", "F1"]},
            {
                "slug": "ok.three",
                "domain": "d",
                "labels": {"en": "x"},
                "vocabulary": {"unknown_key": []},
            },
        ]
    )
    joined = " | ".join(errors)
    assert "slug must match" in joined
    assert "domain is required" in joined
    assert "duplicate slug" in joined
    assert "metrics must be a list of non-empty strings" in joined
    assert "unknown vocabulary keys" in joined


def test_validation_accepts_a_minimal_task() -> None:
    assert validate_task_payloads([{"slug": "x.y", "domain": "d", "labels": {"en": "X"}}]) == []


def test_shared_cues_are_a_warning_not_an_error() -> None:
    """现有 recsys 任务刻意共用线索；把它当错误会拒掉真实本体。"""
    payloads = [
        {"slug": "a.one", "domain": "d", "labels": {"en": "A"}, "inclusion_cues": ["shared"]},
        {"slug": "a.two", "domain": "d", "labels": {"en": "B"}, "inclusion_cues": ["shared"]},
    ]
    assert validate_task_payloads(payloads) == []
    warnings = task_ontology_warnings(payloads)
    assert len(warnings) == 1
    assert "shared" in warnings[0]


# --- lint 本身 --------------------------------------------------------------


def test_ontology_lint_passes_on_the_current_tree() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/ontology_literal_lint.py")],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_ontology_lint_catches_a_reintroduced_literal(tmp_path: Path) -> None:
    """把域名词写回受保护文件时，lint 必须失败——否则它形同虚设。

    做法是把真实脚本复制到临时树里，让它的 ROOT/TARGETS 自然指向一个含违例的
    fixture，而不是在进程内改模块全局量：这样测的是脚本本身，而不是替身。
    """
    exit_code, output = _run_lint_against(tmp_path, 'MODELS = r"SASRec|ESM2"\n')
    assert exit_code == 1, output
    assert "SASRec" in output
    assert "ESM2" in output


def test_ontology_lint_ignores_literals_inside_comments(tmp_path: Path) -> None:
    """注释里提到某个词是在解释历史，不该被当成硬编码。"""
    exit_code, output = _run_lint_against(tmp_path, '# SASRec used to be hardcoded here.\nX = 1\n')
    assert exit_code == 0, output


def _run_lint_against(tmp_path: Path, body: str) -> tuple[int, str]:
    """把真实 lint 脚本指向一个临时 fixture 文件后运行它。"""
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir(exist_ok=True)
    source = (ROOT / "scripts/ontology_literal_lint.py").read_text(encoding="utf-8")
    source = source.replace(
        'ROOT / "services/worker/paperforge_worker/pipelines/evidence.py",',
        'ROOT / "offender.py",',
    )
    for path in (
        "services/worker/paperforge_worker/pipelines/qdecomp.py",
        "services/worker/paperforge_worker/pipelines/experiment_extraction.py",
        "packages/db/db/repositories/tasks.py",
    ):
        source = source.replace(f'    ROOT / "{path}",\n', "")
    (scripts_dir / "lint.py").write_text(source, encoding="utf-8")
    (tmp_path / "offender.py").write_text(body, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(scripts_dir / "lint.py")], capture_output=True, text=True
    )
    return result.returncode, result.stdout + result.stderr
