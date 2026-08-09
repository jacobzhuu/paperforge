#!/usr/bin/env python3
"""Fail CI when domain-specific literals reappear in ontology-driven modules (R16).

领域知识必须住在 ``task_definition`` 行里，不能回到 Python 源码里。这条 lint 是那条
规则的执行点。

它此前只覆盖**数据集名**，而同一个文件里还硬编码着模型族、输入表征、预训练骨干和
划分策略——审计 §4.6 记录了这个缺口。Phase 3 把那些词表搬进了
``task_definition.vocabulary_json``，这里相应地把它们加进禁用集，防止回潮。

已知豁免见 ``ALLOWED``：目前只有 BPR 一项，它在 ``loss_function`` 的通用列表里，
留待损失函数一并本体化时处理（Phase 3 报告的残留项）。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGETS = [
    ROOT / "services/worker/paperforge_worker/pipelines/evidence.py",
    ROOT / "services/worker/paperforge_worker/pipelines/qdecomp.py",
    ROOT / "services/worker/paperforge_worker/pipelines/experiment_extraction.py",
    ROOT / "packages/db/db/repositories/tasks.py",
]

# 这些必须来自 task_definition 行，而不是 Python 源码。
FORBIDDEN = re.compile(
    r"\b(?:"
    # 数据集
    r"MIBiG|antiSMASH-DB|IMG-ABC|DeepBGC|GECCO|clusterFinder|BiG-SLiCE|"
    r"MovieLens|Amazon Beauty|LastFM|"
    # 领域模型族
    r"SASRec|BERT4Rec|GRU4Rec|"
    # 预训练骨干
    r"ESM2|ProtBERT|DNABERT|"
    # 输入表征
    r"Pfam domain|domain graph|item sequence|user sequence|amino acid|"
    # 划分策略
    r"leave-one-genome-out|"
    # 领域指标（不属于"通用"核心）
    r"[Tt]animoto"
    r")\b"
)

#: 已知且有意保留的豁免：(相对路径, 字面量)。每一条都要有理由。
ALLOWED: frozenset[tuple[str, str]] = frozenset(
    {
        # BPR 住在 _structured_payload 的 loss_function 通用列表里。损失函数整体尚未
        # 本体化；单独为一个词开一类词表不划算。见 Phase 3 报告「残留」。
        ("services/worker/paperforge_worker/pipelines/evidence.py", "BPR"),
    }
)


def main() -> int:
    failures: list[str] = []
    for path in TARGETS:
        if not path.exists():
            failures.append(f"{path.relative_to(ROOT)}: target file is missing")
            continue
        relative = str(path.relative_to(ROOT))
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            # 注释里提到某个词是在解释历史，不是在硬编码它。
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            for match in FORBIDDEN.finditer(line):
                if (relative, match.group(0)) in ALLOWED:
                    continue
                failures.append(f"{relative}:{line_number}: {match.group(0)}")
    if failures:
        print("Domain literals found outside task_definition (R16):")
        for item in failures:
            print(f"  {item}")
        print(
            "\nMove these into task_definition (metric/dataset whitelists or "
            "vocabulary_json) and load them through db.repositories.tasks."
        )
        return 1
    print(f"ontology_literal_lint: ok ({len(TARGETS)} files checked)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
