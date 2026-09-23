"""Pairwise hard gates; missing data and unknown accounting never pass."""

from collections import Counter
from statistics import median

PROTOCOL = "writing-efficiency-v3"


def analyze(rows, reviews):
    groups = {c: [r for r in rows if r["condition"] == c] for c in ("baseline", "optimized")}
    if any(len(g) != 3 for g in groups.values()):
        raise ValueError("requires exactly three paired runs per arm")
    identities = [
        {(r["fixture_hash"], r["model_config_hash"], r["repeat"], r["cache_mode"]) for r in g}
        for g in groups.values()
    ]
    if len(identities[0]) != 3 or identities[0] != identities[1]:
        raise ValueError("paired inputs and repeats differ")
    if any(r["protocol"] != PROTOCOL for r in rows):
        raise ValueError("wrong protocol")
    before, after = groups["baseline"], groups["optimized"]
    speed = 1 - median(r["writing_seconds"] for r in after) / median(
        r["writing_seconds"] for r in before
    )
    known = all(r.get("tokens") is not None and r.get("unpriced_calls") == 0 for r in rows)
    growth = (
        median(r["tokens"] for r in after) / max(1, median(r["tokens"] for r in before)) - 1
        if known
        else None
    )
    hard_ok = True
    for a in before:
        b = next(r for r in after if r["repeat"] == a["repeat"])
        hard_ok &= Counter(b["hard_violations"]) <= Counter(a["hard_violations"])
    reviewed = set(reviews) == {"0", "1", "2"} and all(
        v.get("source") == "ai_blind_review"
        and v.get("assessed") is True
        and v.get("passed") is True
        for v in reviews.values()
    )
    return {
        "protocol": PROTOCOL,
        "writing_time_reduction": speed,
        "token_growth": growth,
        "hard_violations_nonincreasing": bool(hard_ok),
        "ai_review_passed": reviewed,
        "human_review_complete": False,
        "accounting_complete": known,
        "release_eligible": bool(
            speed >= 0.15
            and growth is not None
            and growth <= 0.1
            and hard_ok
            and reviewed
            and all(r["complete"] for r in rows)
        ),
        "review_source": "ai_blind_review",
        "scope": "relative improvement; existing quality blockers remain authoritative",
    }
