"""Zero-paid-call audit of immutable benchmark snapshots. No DB or network access.

uv run python -m evals.agent_writing.replay_evaluator --source DIR --output NEW_DIR
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

from paperforge_worker.pipelines.quality import _PLACEHOLDER_RE, _body_text, placeholder_sections
from paperforge_worker.pipelines.review_inputs import (
    MAX_REVIEW_INPUT_CHARS,
    REVIEW_INPUT_VERSION,
    build_review_input,
)

OLD_PLACEHOLDER = re.compile(
    r"(?:待实验补充|待补充实验数据|待补充|尚无满足定位与可比性要求的证据|"
    r"no evidence meeting the required provenance|TODO|TBD|PLACEHOLDER|\[待[^\]]*\])",
    re.IGNORECASE,
)


def audit(source: Path, output: Path) -> dict:
    if output.exists():
        raise ValueError("output exists; use a new audit directory")
    source = source.resolve()
    paths = [
        source / "cold.json",
        source / "fixture.json",
        source / "review/db-snapshot.json",
        source / "review/reviewer-inputs.json",
    ]
    runs, fixture, snapshot, old_inputs = [json.loads(p.read_text()) for p in paths]
    manifest = json.loads((source / "review/input-sha256.json").read_text())
    for name, expected in manifest.items():
        if hashlib.sha256((source / name).read_bytes()).hexdigest() != expected:
            raise ValueError(f"historical artifact changed: {name}")
    outline = fixture["benchmark_outline"]
    whitelist = {}
    for bundle in outline["sub_question_bundles"]:
        for unit in bundle["evidence"]:
            cite, work = unit.get("cite_key"), str(unit.get("work_id") or "")
            if cite and work:
                if cite in whitelist and whitelist[cite] != work:
                    raise ValueError("conflicting fixture citation ownership")
                whitelist[cite] = work
    output.mkdir(parents=True)
    results, packets = [], []
    for run in runs:
        rows = [r for r in snapshot["sections"] if r["document_id"] == run["document_id"]]
        by_section = {r["section_key"]: r for r in rows}
        missing_keys = sorted({k for r in rows for k in r["cite_keys_json"]} - whitelist.keys())
        if missing_keys:
            raise ValueError(f"fixture lacks citation ownership: {missing_keys}")
        reviewed = []
        for old in old_inputs[run["job_id"]]:
            row = by_section[old["section_key"]]
            payload = build_review_input(
                section_key=row["section_key"],
                question=old["question"],
                prose=old["prose"],
                body=row["body_ir_json"],
                evidence=snapshot["evidence"],
                whitelist=whitelist,
                scope={
                    "project_id": fixture["source_project_id"],
                    "document_id": run["document_id"],
                },
            )
            selected = {e["evidence_id"] for e in payload["evidence"]}
            visible = set(old["visible_evidence_ids"])
            serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
            name = f"{run['job_id']}-{row['section_key']}.json"
            (output / name).write_text(serialized)
            reviewed.append(
                {
                    "section": row["section_key"],
                    "artifact": name,
                    "input_hash": payload["input_hash"],
                    "coverage": payload["coverage"],
                    "old_visible_bound_ids": len(selected & visible),
                    "new_visible_bound_ids": len(selected),
                    "newly_visible_ids": sorted(selected - visible),
                    "input_chars": len(serialized),
                    "within_budget": len(serialized) <= MAX_REVIEW_INPUT_CHARS,
                    "historical_prompt_hash_verified": old["prompt_hash_verified"],
                    "new_semantic_verdict": "unassessed_no_paid_calls",
                }
            )
        changes = []
        for row in rows:
            for bi, block in enumerate(row["body_ir_json"].get("blocks") or []):
                text = _body_text({"blocks": [block]})
                before, after = (
                    bool(OLD_PLACEHOLDER.search(text)),
                    bool(_PLACEHOLDER_RE.search(text)),
                )
                if before != after:
                    changes.append(
                        {
                            "section": row["section_key"],
                            "block": bi,
                            "text": text,
                            "old_placeholder": before,
                            "new_placeholder": after,
                            "reason": "ordinary_gap_prose_not_explicit_placeholder",
                        }
                    )
        old_hard = Counter(run["hard_violations"])
        projected = old_hard.copy()
        projected.pop("placeholders_present", None)
        if placeholder_sections([SimpleNamespace(**r) for r in rows]):
            projected["placeholders_present"] = 1
        results.append(
            {
                "job_id": run["job_id"],
                "sections": reviewed,
                "placeholder_changes": changes,
                "historical_hard": dict(old_hard),
                "placeholder_only_hard_projection": dict(projected),
                "other_hard_rules": "historical results retained, not re-evaluated",
                "historical_semantic_verdicts": run["semantic_verdicts"],
            }
        )
    # Copy existing blind materials byte-for-byte; no new scores or condition leakage.
    if (source / "blind-packet").is_dir():
        shutil.copytree(source / "blind-packet", output / "blind-packet")
        packets.append("blind-packet/")
    summary = {
        "version": REVIEW_INPUT_VERSION,
        "runs": len(results),
        "sections": sum(len(r["sections"]) for r in results),
        "paid_calls": 0,
        "old_visible_bound_ids": sum(
            s["old_visible_bound_ids"] for r in results for s in r["sections"]
        ),
        "new_visible_bound_ids": sum(
            s["new_visible_bound_ids"] for r in results for s in r["sections"]
        ),
        "unresolved_binding_issues": sum(
            s["coverage"]["binding_issues"] for r in results for s in r["sections"]
        ),
        "oversized_inputs": sum(not s["within_budget"] for r in results for s in r["sections"]),
        "placeholder_changed_runs": sum(bool(r["placeholder_changes"]) for r in results),
        "semantic_validation": "pending paid evaluator replay and human blind review",
        "release_eligible": False,
        "blind_materials": packets,
        "input_sha256": {
            str(p.relative_to(source)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths
        },
    }
    (output / "audit.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    (output / "README.md").write_text(
        "# 零付费 Evaluator 回放\n\n"
        "旧评审结果来自历史记录；新输入按真实 PaperIR 绑定重建。未调用新语义评审模型。\n\n"
        "audit.json 的 hard projection 只重新执行 placeholder 规则，其他 hard 判断保留历史值；"
        "不能把它当作完整的新质量报告。绑定问题另行逐项记录在输入 artifact。\n\n"
        "blind-packet 为原有匿名材料副本；评分保持未完成。条件映射不在盲审包内。\n"
    )
    return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(audit(args.source, args.output), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
