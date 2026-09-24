"""Coverage-aware evaluator metrics. Missing human judgments never become passes."""

import argparse
import json
from pathlib import Path


def evaluate(cases, predictions):
    if len({c["id"] for c in cases}) != len(cases):
        raise ValueError("duplicate case id")
    labels = {
        case["id"]: case["label"]
        for case in cases
        if case.get("label") in {"supported", "contradicted", "insufficient"}
        and case.get("reviewer")
        and case.get("rationale")
    }
    if len({p["id"] for p in predictions}) != len(predictions):
        raise ValueError("duplicate prediction")
    known = {c["id"] for c in cases}
    if any(p["id"] not in known for p in predictions):
        raise ValueError("prediction not in evaluation set")
    assessed = {
        p["id"]: p["label"]
        for p in predictions
        if p.get("label") in {"supported", "contradicted", "insufficient"}
    }
    paired = labels.keys() & assessed.keys()
    false_accept = sum(
        labels[key] != "supported" and assessed[key] == "supported" for key in paired
    )
    false_reject = sum(
        labels[key] == "supported" and assessed[key] != "supported" for key in paired
    )
    return {
        "cases": len(cases),
        "human_labelled": len(labels),
        "model_assessed": len(assessed),
        "paired": len(paired),
        "unassessed": len(cases) - len(assessed),
        "false_accept": false_accept if paired else None,
        "false_reject": false_reject if paired else None,
        "accuracy": sum(labels[k] == assessed[k] for k in paired) / len(paired) if paired else None,
        "human_review_complete": len(labels) == len(cases) and bool(cases),
        "release_eligible": False,  # This report alone never releases a writing configuration.
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--predictions", type=Path)
    args = parser.parse_args()
    print(
        json.dumps(
            evaluate(
                json.loads(args.cases.read_text()),
                json.loads(args.predictions.read_text()) if args.predictions else [],
            ),
            indent=2,
        )
    )
