"""Selective risk/coverage analysis; repeated observations are never extra labels."""

from __future__ import annotations

import json
import math
import statistics
from pathlib import Path

from llm_runtime.decision import validate_decision_response
from paperforge_worker.pipelines.citation_decisions import decision_request

from evals.jev_shadow.freeze import save


def ratio(n, d):
    return n / d if d else None


def wilson_upper(errors: int, n: int, z: float = 1.95996398454) -> float | None:
    if not n:
        return None
    p = errors / n
    return (p + z * z / (2 * n) + z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / (
        1 + z * z / n
    )


def threshold_metrics(rows: list[dict], threshold: float) -> dict:
    certain = [r for r in rows if not r["ambiguous"]]
    accepted_all = [
        r
        for r in rows
        if r.get("confidence") is not None
        and r["confidence"] >= threshold
        and r.get("score") is not None
    ]
    accepted = [r for r in accepted_all if not r["ambiguous"]]
    tp = sum(r["score"] < 2 and r["grade"] < 2 for r in accepted)
    fp = sum(r["score"] < 2 and r["grade"] >= 2 for r in accepted)
    fn = sum(r["score"] >= 2 and r["grade"] < 2 for r in accepted)
    tn = len(accepted) - tp - fp - fn
    ambiguous_total = sum(r["ambiguous"] for r in rows)
    uncertain_pass = sum(r["ambiguous"] for r in accepted_all)
    # Conservative bound: count harmful replacements, without credit for beneficial
    # replacements. This upper-bounds the paired mixed-policy error increase.
    harm_fp = harm_fn = 0
    for r in accepted:
        if r.get("baseline_score") is None:
            continue
        if r["grade"] >= 2 and r["score"] < 2 and r["baseline_score"] >= 0.5:
            harm_fp += 1
        if r["grade"] < 2 and r["score"] >= 2 and r["baseline_score"] < 0.5:
            harm_fn += 1
    negatives = sum(r["grade"] < 2 for r in certain)
    positives = len(certain) - negatives
    upper = wilson_upper(fp + fn, len(accepted))
    precision, recall = ratio(tp, tp + fp), ratio(tp, tp + fn)
    fp_increase = wilson_upper(harm_fp, positives, 1.64485362695)
    fn_increase = wilson_upper(harm_fn, negatives, 1.64485362695)
    coverage = ratio(len(accepted_all), len(rows)) or 0
    ambiguous_rate = ratio(uncertain_pass, ambiguous_total) if ambiguous_total else 0
    missing_baseline = sum(r.get("baseline_score") is None for r in certain)
    checks = {
        "accepted_at_least_100": len(accepted) >= 100,
        "error_upper_at_most_5pct": upper is not None and upper <= 0.05,
        "weak_precision_at_least_95pct": precision is not None and precision >= 0.95,
        "weak_recall_at_least_80pct": recall is not None and recall >= 0.8,
        "ambiguous_acceptance_at_most_5pct": ambiguous_rate <= 0.05,
        "coverage_at_least_30pct": coverage >= 0.3,
        "paired_fp_noninferiority": fp_increase is not None and fp_increase <= 0.02,
        "paired_fn_noninferiority": fn_increase is not None and fn_increase <= 0.02,
        "baseline_complete": missing_baseline == 0,
    }
    return {
        "threshold": threshold,
        "n": len(rows),
        "accepted": len(accepted_all),
        "accepted_certain": len(accepted),
        "coverage": coverage,
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "accuracy": ratio(tp + tn, len(accepted)),
        "error_upper_95": upper,
        "weak_precision": precision,
        "weak_recall_within_accepted": recall,
        "weak_recall_over_all_labels": ratio(tp, negatives),
        "false_weak_rate": ratio(fp, fp + tn),
        "miss_weak_rate": ratio(fn, fn + tp),
        "ambiguous_accepted": uncertain_pass,
        "ambiguous_acceptance_rate": ambiguous_rate,
        "paired_fp_increase_upper_95": fp_increase,
        "paired_fn_increase_upper_95": fn_increase,
        "checks": checks,
        "eligible": all(checks.values()),
    }


def percentile(values: list[float], q: float):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(q * len(ordered)) - 1)]


def call_summary(records: list[dict]) -> dict:
    latencies = [r["latency_ms"] for r in records if r.get("latency_ms") is not None]
    return {
        "calls": len(records),
        "errors": sum(bool(r.get("error_code")) for r in records),
        "p50_ms": percentile(latencies, 0.5),
        "p95_ms": percentile(latencies, 0.95),
        "input_tokens": sum(r.get("input_tokens") or 0 for r in records),
        "output_tokens": sum(r.get("output_tokens") or 0 for r in records),
        "priced_cost": sum(r.get("cost_estimate") or 0 for r in records),
        "unpriced_calls": sum(r.get("cost_estimate") is None for r in records),
    }


def audit_http(directory: Path) -> dict:
    summary = {
        "attempts": 0,
        "unresolved": 0,
        "http_errors": 0,
        "jev_parse_errors": 0,
        "raw_parsed_mismatches": 0,
        "jev_answers": 0,
        "reserved_usd": 0.0,
    }
    for path in sorted((directory / "http").glob("*.reservation.json")):
        request = json.loads(path.read_text())
        summary["attempts"] += 1
        summary["reserved_usd"] += request["reserved_usd"]
        response_path = path.with_name(path.name.replace("reservation", "response"))
        if not response_path.exists():
            summary["unresolved"] += 1
            continue
        response = json.loads(response_path.read_text())
        if response.get("status") != 200:
            summary["http_errors"] += 1
            continue
        if request["kind"] != "jev":
            continue
        try:
            payload = json.loads(response["body"])
        except ValueError:
            summary["jev_parse_errors"] += 1
            continue
        answers, _, _, error = validate_decision_response(
            payload, questions=request["request"]["questions"]
        )
        if error:
            summary["jev_parse_errors"] += 1
        elif answers is not None:
            summary["jev_answers"] += len(answers)
            summary["raw_parsed_mismatches"] += sum(
                answer != payload["answers"][key] for key, answer in answers.items()
            )
    return summary


def ablation_summary(directory: Path) -> dict:
    observations: dict[str, list] = {}
    repeats: dict[str, list] = {}
    for path in sorted(directory.glob("ablation.*.json")):
        data = json.loads(path.read_text())
        result = data["result"]
        for i, sample_id in enumerate(data["sample_ids"]):
            answer = (result.get("answers") or {}).get(f"item_{i}")
            if answer is None:
                continue
            observations.setdefault(data["variant"], []).append(answer)
            if data["variant"] == "batch40":
                repeats.setdefault(sample_id, []).append(answer)
    return {
        "variants": {
            name: {
                "observations": len(items),
                "zero_confidence": sum(i["confidence"] == 0 for i in items),
                "mean_confidence": statistics.mean(i["confidence"] for i in items),
                "weak_count": sum(i["score"] < 2 for i in items),
            }
            for name, items in observations.items()
        },
        "repeat_pairs": len(repeats),
        "repeat_weak_flips": sum(
            len({i["score"] < 2 for i in items}) > 1 for items in repeats.values()
        ),
        "repeat_mean_score_sd": statistics.mean(
            statistics.pstdev(i["score"] for i in items) for items in repeats.values()
        )
        if repeats
        else None,
    }


def audit_event_chain(directory: Path, corpus: dict, phase: str) -> dict:
    """Match first bound request for each unique frozen batch to persisted events.

    Main passes precede development ablations; test projects are disjoint. Failed
    requests remain missing, not silently replaced with a later successful repeat.
    """
    requests = []
    for path in sorted((directory / "http").glob("*.reservation.json")):
        record = json.loads(path.read_text())
        if record["kind"] == "jev":
            requests.append((record["request"], path))
    audit = {
        "matched_batches": 0,
        "raw_unavailable_batches": 0,
        "compared_answers": 0,
        "mismatches": 0,
    }
    for gi, group in enumerate(corpus["groups"]):
        if group["split"] != phase:
            continue
        state, questions = decision_request(group["pairs"])
        path = next(
            (p for r, p in requests if r["state"] == state and r["questions"] == questions), None
        )
        event_path = directory / f"{phase}.group-{gi:02d}.json"
        if path is None or not event_path.exists():
            audit["raw_unavailable_batches"] += 1
            continue
        response_path = path.with_name(path.name.replace("reservation", "response"))
        response = json.loads(response_path.read_text()) if response_path.exists() else {}
        if response.get("status") != 200:
            audit["raw_unavailable_batches"] += 1
            continue
        payload = json.loads(response["body"])
        answers, _, _, error = validate_decision_response(payload, questions=questions)
        if error:
            audit["raw_unavailable_batches"] += 1
            continue
        audit["matched_batches"] += 1
        event = json.loads(event_path.read_text())["events"][0]["payload"]
        items = {item["index"]: item for item in event["items"]}
        for i in range(len(group["pairs"])):
            raw = answers[f"item_{i}"]
            item = items.get(i, {})
            audit["compared_answers"] += 1
            audit["mismatches"] += any(
                item.get(k) != raw[k] for k in ("score", "confidence", "probabilities")
            )
    return audit


def report(directory: Path, phase: str) -> dict:
    from evals.jev_shadow.run import load_corpus, load_labels

    corpus, labels = load_corpus(directory), load_labels(directory)
    by_id = {i["sample_id"]: i for i in labels["items"]}
    groups, unique = [], {}
    for gi, group in enumerate(corpus["groups"]):
        if group["split"] != phase:
            continue
        path = directory / f"{phase}.group-{gi:02d}.json"
        event_items = {}
        if path.exists():
            row = json.loads(path.read_text())
            for event in row["events"]:
                event_items.update({i["index"]: i for i in event["payload"]["items"]})
        merged = []
        for i, pair in enumerate(group["pairs"]):
            item = {
                **by_id[pair["sample_id"]],
                **event_items.get(i, {}),
                "source_kind": pair["source_kind"],
                "language": group["language"],
                "context_length": len(pair["context"]),
                "evidence_length": len(pair["evidence"]),
            }
            merged.append(item)
            unique.setdefault(pair["pair_hash"], item)
        groups.append(merged)
    rows = list(unique.values())
    thresholds = []
    for n in range(21):
        t = n / 20
        metric = threshold_metrics(rows, t)
        metric["whole_batch_coverage"] = ratio(
            sum(
                all(
                    r.get("confidence") is not None
                    and r["confidence"] >= t
                    and r.get("score") is not None
                    for r in g
                )
                for g in groups
            ),
            len(groups),
        )
        # This is a simulation of the existing all-or-nothing gate, not savings
        # achieved in shadow; partial fallback usually still requires one LLM call.
        metric["simulated_llm_calls_per_pass"] = 1 - (metric["whole_batch_coverage"] or 0)
        metric["call_reduction_eligible"] = (
            metric["eligible"] and (metric["whole_batch_coverage"] or 0) >= 0.2
        )
        thresholds.append(metric)
    calls = json.loads((directory / f"{phase}.calls.json").read_text())
    main_jev = [r for r in calls["jev"] if r.get("metadata", {}).get("stage") == "soft_check"]
    candidates = [m for m in thresholds if m["eligible"]]
    if phase == "dev":
        chosen = max(candidates, key=lambda m: (m["coverage"], m["threshold"]), default=None)
        selection = {
            "threshold": chosen["threshold"] if chosen else None,
            "corpus_hash": corpus["corpus_hash"],
            "label_hash": labels["label_hash"],
            "status": "candidate" if chosen else "insufficient_evidence",
            "prompt_version": corpus["version"],
        }
        save(directory / "selection.lock.json", selection)
    else:
        selection = json.loads((directory / "selection.lock.json").read_text())
    chosen_metric = next((m for m in thresholds if m["threshold"] == selection["threshold"]), None)
    agreement = [
        r for r in rows if r.get("score") is not None and r.get("baseline_score") is not None
    ]
    certain_baseline = [
        r for r in rows if not r["ambiguous"] and r.get("baseline_score") is not None
    ]
    confidence = [r["confidence"] for r in rows if r.get("confidence") is not None]
    output = {
        "phase": phase,
        "corpus_hash": corpus["corpus_hash"],
        "label_hash": labels["label_hash"],
        "label_type": labels["label_type"],
        "reviewer": labels["reviewer"],
        "annotator_model": labels.get("annotator_model"),
        "n_unique": len(rows),
        "groups": len(groups),
        "ambiguous_labels": sum(r["ambiguous"] for r in rows),
        "missing_decisions": sum(r.get("score") is None for r in rows),
        "missing_baseline": sum(r.get("baseline_score") is None for r in rows),
        "raw_confidence": {
            "zero": sum(c == 0 for c in confidence),
            "mean": statistics.mean(confidence) if confidence else None,
            "max": max(confidence, default=None),
        },
        "baseline_accuracy": ratio(
            sum((r["baseline_score"] < 0.5) == (r["grade"] < 2) for r in certain_baseline),
            len(certain_baseline),
        ),
        "jev_baseline_agreement": ratio(
            sum((r["score"] < 2) == (r["baseline_score"] < 0.5) for r in agreement), len(agreement)
        ),
        "thresholds": thresholds,
        "selection": selection,
        "selected_threshold_test_passed": bool(chosen_metric and chosen_metric["eligible"]),
        "actual_shadow_calls": {
            "llm": call_summary(calls["llm"]),
            "jev_main": call_summary(main_jev),
            "jev_including_ablations": call_summary(calls["jev"]),
        },
        "actual_llm_calls_per_pass": ratio(len(calls["llm"]), len(groups)),
        "actual_total_model_calls_per_pass": ratio(len(calls["llm"]) + len(main_jev), len(groups)),
        "full_job_calls": None,
        "full_job_calls_note": "Offline quality replay; not a full job run.",
        "limitations": [
            "AI reference labels, not independent human gold",
            "Wilson bounds are pair-level approximations; projects may be correlated",
            "No production fast-path enabled; all reported savings are simulations",
        ],
        "strata": {},
        "http_audit": audit_http(directory),
        "raw_to_event_audit": audit_event_chain(directory, corpus, phase),
        "ablations": ablation_summary(directory),
    }
    for field in ("language", "source_kind"):
        output["strata"][field] = {
            value: threshold_metrics(
                [r for r in rows if r[field] == value], selection["threshold"] or 0
            )
            for value in sorted({r[field] for r in rows})
        }
    output["strata"]["context_length"] = {
        name: threshold_metrics(
            [r for r in rows if low <= r["context_length"] <= high], selection["threshold"] or 0
        )
        for name, low, high in [("under150", 0, 149), ("150to300", 150, 300)]
    }
    save(directory / f"{phase}.report.json", output)
    return {
        "phase": phase,
        "n": len(rows),
        "selection": selection,
        "test_passed": output["selected_threshold_test_passed"],
        "actual_shadow_calls": output["actual_shadow_calls"],
    }
