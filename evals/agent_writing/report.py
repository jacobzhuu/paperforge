from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from statistics import median

CONDITIONS = ("legacy", "dag_serial", "dag_parallel")


def analyze(runs: list[dict]) -> dict:
    groups = {
        condition: [r for r in runs if r["condition"] == condition] for condition in CONDITIONS
    }
    if any(not rows for rows in groups.values()):
        raise ValueError("all three A/B/C conditions are required")
    identities = [
        {(r["fixture_hash"], r["repeat"], r["cache_mode"], r["model_config_hash"]) for r in rows}
        for rows in groups.values()
    ]
    if any(len(ids) != len(rows) for ids, rows in zip(identities, groups.values(), strict=True)):
        raise ValueError("duplicate paired run")
    if not all(ids == identities[0] for ids in identities):
        raise ValueError("conditions must have the same inputs, model, cache mode and repeats")
    summary = {
        key: {
            "runs": len(rows),
            "writing_seconds_median": median(r["writing_seconds"] for r in rows),
            "tokens_median": (
                median(r["tokens"] for r in rows)
                if all(r.get("tokens") is not None for r in rows)
                else None
            ),
            "unpriced_calls": sum(r.get("unpriced_calls", 0) for r in rows),
            "failed_runs": sum(not r.get("complete") for r in rows),
        }
        for key, rows in groups.items()
    }
    serial, parallel = summary["dag_serial"], summary["dag_parallel"]
    speedup = 1 - parallel["writing_seconds_median"] / max(serial["writing_seconds_median"], 0.001)
    token_growth = None
    if serial["tokens_median"] and parallel["tokens_median"] is not None:
        token_growth = parallel["tokens_median"] / serial["tokens_median"] - 1
    # Human quality is never synthesized from mock/LLM scores. A missing review
    # means 'not evaluated', not a passing release gate.
    reviewed = all(
        r.get("expert_verified") and r.get("expert_scores") and "hard_violations" in r for r in runs
    )
    quality_ok = False
    if reviewed:
        by_pair = {
            condition: {
                (r["fixture_hash"], r["repeat"], r["cache_mode"], r["model_config_hash"]): r
                for r in rows
            }
            for condition, rows in groups.items()
        }
        quality_ok = True
        for identity in identities[0]:
            a, b, c = [by_pair[condition][identity] for condition in CONDITIONS]
            for before, after in ((a, b), (b, c)):
                quality_ok &= Counter(after["hard_violations"]) <= Counter(
                    before["hard_violations"]
                )
                for metric in ("organization", "evidence", "consistency"):
                    quality_ok &= after["expert_scores"][metric] >= (
                        before["expert_scores"][metric] - 0.2
                    )
    eligible = (
        reviewed
        and quality_ok
        and len(identities[0]) >= 3
        and speedup >= 0.15
        and token_growth is not None
        and token_growth <= 0.10
        and all(not s["failed_runs"] for s in summary.values())
    )
    return {
        "conditions": summary,
        "writing_time_reduction": speedup,
        "token_growth": token_growth,
        "expert_review_complete": reviewed,
        "release_eligible": bool(eligible),
        "scope": "writing and evaluation; not total Agent throughput or capacity",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("runs", type=Path)
    args = parser.parse_args()
    print(json.dumps(analyze(json.loads(args.runs.read_text())), indent=2))


if __name__ == "__main__":
    main()
