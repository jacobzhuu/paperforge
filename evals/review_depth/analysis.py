"""Analyze G3 ablations and double-blind expert scores without optional dependencies."""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from statistics import fmean
from typing import Any

EXPERT_DIMENSIONS = (
    "depth",
    "organization",
    "evidence",
    "criticality",
    "credibility",
    "readability",
)


def wilcoxon_signed_rank(pairs: list[tuple[float, float]]) -> dict[str, float | int]:
    """Two-sided paired Wilcoxon signed-rank test with an exact finite distribution."""
    differences = [right - left for left, right in pairs if not math.isclose(right, left)]
    if not differences:
        return {"n": 0, "w_plus": 0.0, "w_minus": 0.0, "statistic": 0.0, "p_value": 1.0}
    absolute = [abs(value) for value in differences]
    ranks = _average_ranks(absolute)
    w_plus = sum(
        rank
        for rank, difference in zip(ranks, differences, strict=True)
        if difference > 0
    )
    w_minus = sum(
        rank
        for rank, difference in zip(ranks, differences, strict=True)
        if difference < 0
    )

    doubled_ranks = [int(round(rank * 2)) for rank in ranks]
    distribution = Counter({0: 1})
    for rank in doubled_ranks:
        distribution += Counter({value + rank: count for value, count in distribution.items()})
    observed = int(round(w_plus * 2))
    samples = 2 ** len(doubled_ranks)
    lower = sum(count for value, count in distribution.items() if value <= observed) / samples
    upper = sum(count for value, count in distribution.items() if value >= observed) / samples
    return {
        "n": len(differences),
        "w_plus": round(w_plus, 4),
        "w_minus": round(w_minus, 4),
        "statistic": round(min(w_plus, w_minus), 4),
        "p_value": round(min(1.0, 2 * min(lower, upper)), 6),
    }


def _average_ranks(values: list[float]) -> list[float]:
    ordered = sorted(enumerate(values), key=lambda item: item[1])
    ranks = [0.0] * len(values)
    start = 0
    while start < len(ordered):
        end = start + 1
        while end < len(ordered) and math.isclose(ordered[end][1], ordered[start][1]):
            end += 1
        average = ((start + 1) + end) / 2
        for original_index, _value in ordered[start:end]:
            ranks[original_index] = average
        start = end
    return ranks


def summarize_runs(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize automatic metrics, tokens, and wall time by ablation condition."""
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["condition"])].append(row)
    summary: dict[str, Any] = {}
    for condition, condition_rows in sorted(grouped.items()):
        metric_keys = sorted(
            {
                key
                for row in condition_rows
                for key, value in (row.get("metrics") or {}).items()
                if isinstance(value, int | float)
            }
        )
        summary[condition] = {
            "runs": len(condition_rows),
            "metrics": {
                key: round(
                    fmean(
                        float(row["metrics"][key])
                        for row in condition_rows
                        if isinstance((row.get("metrics") or {}).get(key), int | float)
                    ),
                    6,
                )
                for key in metric_keys
            },
            "input_tokens": round(
                fmean(float(row.get("input_tokens") or 0) for row in condition_rows),
                2,
            ),
            "output_tokens": round(
                fmean(float(row.get("output_tokens") or 0) for row in condition_rows),
                2,
            ),
            "wall_seconds": round(
                fmean(float(row.get("wall_seconds") or 0) for row in condition_rows),
                2,
            ),
        }
    return summary


def summarize_expert_scores(
    scores: list[dict[str, Any]],
    blind_key: dict[str, str],
    *,
    baseline: str,
    treatment: str,
) -> dict[str, Any]:
    """Unblind only for analysis and run paired tests per expert-scoring dimension."""
    grouped: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    for row in scores:
        condition = blind_key.get(str(row["blind_doc_id"]))
        if not condition:
            continue
        for dimension in EXPERT_DIMENSIONS:
            value = (row.get("scores") or {}).get(dimension)
            if isinstance(value, int | float):
                grouped[(str(row["topic_id"]), condition, dimension)].append(float(value))
    result: dict[str, Any] = {}
    for dimension in EXPERT_DIMENSIONS:
        pairs: list[tuple[float, float]] = []
        for topic_id in sorted({key[0] for key in grouped}):
            left = grouped.get((topic_id, baseline, dimension), [])
            right = grouped.get((topic_id, treatment, dimension), [])
            if left and right:
                pairs.append((fmean(left), fmean(right)))
        result[dimension] = {
            "baseline_mean": round(fmean(left for left, _right in pairs), 4) if pairs else None,
            "treatment_mean": round(fmean(right for _left, right in pairs), 4) if pairs else None,
            "paired_delta": (
                round(fmean(right - left for left, right in pairs), 4) if pairs else None
            ),
            "wilcoxon": wilcoxon_signed_rank(pairs),
        }
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=Path, required=True)
    parser.add_argument("--scores", type=Path)
    parser.add_argument("--blind-key", type=Path)
    parser.add_argument("--baseline", default="legacy")
    parser.add_argument("--treatment", default="problem_driven")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report: dict[str, Any] = {"automatic": summarize_runs(_read_json(args.runs))}
    if args.scores and args.blind_key:
        report["expert"] = summarize_expert_scores(
            _read_json(args.scores),
            _read_json(args.blind_key),
            baseline=args.baseline,
            treatment=args.treatment,
        )
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
