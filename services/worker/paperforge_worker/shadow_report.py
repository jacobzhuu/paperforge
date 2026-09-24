"""One-shot, read-only shadow report. Never calls models or changes any release gate."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter, defaultdict
from pathlib import Path

from sqlalchemy import select

from paperforge_worker.orchestration.writing_graph import fingerprint

TRUST_GATE = {
    "version": "shadow-trust-v1",
    "minimum_documents": 10,
    "minimum_projects": 3,
    "minimum_repeated_sections": 20,
    "required_repeats": 3,
    "minimum_semantic_positive": 50,
    "minimum_semantic_negative": 50,
    "minimum_deterministic_positive": 20,
    "minimum_consistency": 0.95,
    "maximum_failure_rate": 0.02,
    "maximum_ambiguity_rate": 0.05,
    "maximum_fp_rate": 0.05,
    "maximum_fn_rate": 0.05,
    "maximum_deterministic_fn": 0,
}


def checks(raw):
    value = raw.get("claim_checks")
    return (
        [
            c
            for c in value
            if isinstance(c, dict)
            and isinstance(c.get("claim_id"), str)
            and isinstance(c.get("status"), str)
        ]
        if isinstance(value, list)
        else []
    )


def signature(row):
    if row["status"] != "assessed":
        return None
    raw = row["llm"]
    return fingerprint(
        {
            "dimensions": {
                k: raw.get(k)
                for k in (
                    "answers_question",
                    "support",
                    "calibration",
                    "synthesis_mode",
                    "gap_declared",
                )
            },
            "claims": sorted((c["claim_id"], c["status"]) for c in checks(raw)),
        }
    )


def baseline_differences(frozen, results):
    if not frozen.get("baseline_comparable"):
        return {"status": "not_comparable", "claims": []}
    anchors = defaultdict(list)
    for a in frozen.get("baseline_claim_anchors", []):
        anchors[(a["section_key"], a["claim_text"].strip())].append(a["support_status"])
    claims = {m["input_hash"]: {c["claim_id"]: c for c in m["claims"]} for m in frozen["sections"]}
    comparisons = []
    for r in results:
        for c in checks(r["llm"]):
            original = claims.get(r["input_hash"], {}).get(c["claim_id"])
            statuses = (
                anchors.get((r["section_key"], original["text"].strip())) if original else None
            )
            llm_supported = c["status"] in {"supported", "inference"}
            prior_supported = bool(statuses) and all(
                s in {"supported", "attribution_supported"} for s in statuses
            )
            comparisons.append(
                {
                    "claim_id": c["claim_id"],
                    "repeat": r["repeat"],
                    "online_statuses": statuses,
                    "llm_status": c["status"],
                    "disagreement": None
                    if not statuses
                    or c["status"] in {"gap", "nonfactual", "uncertain"}
                    or r["status"] != "assessed"
                    else prior_supported != llm_supported,
                }
            )
    return {
        "status": "snapshot_matched",
        "claims": comparisons,
        "note": "Binding eligibility and entailment have different meanings; this is not gold.",
    }


def summarize(runs: list[dict], gold: list[dict] | None = None) -> dict:
    groups = defaultdict(list)
    inputs = {}
    expected_groups = set()
    diffs = []
    total_expected = 0
    calls = []
    projects, documents = set(), set()
    for run in runs:
        frozen = run["input_json"]
        projects.add(str(run["project_id"]))
        documents.add((str(run["project_id"]), frozen["document_id"], frozen["snapshot"]))
        for m in frozen["sections"]:
            inputs[m["input_hash"]] = m
            expected_groups.add(m["input_hash"])
            total_expected += frozen["repeats"]
        baseline = frozen.get("baseline") or {}
        diffs.append(
            {
                "run_id": run["id"],
                "online_report_id": baseline.get("id"),
                "snapshot_matched": frozen.get("baseline_comparable", False),
                "online_readiness": baseline.get("readiness_status"),
                "claim_differences": baseline_differences(frozen, run["results_json"]),
                "online_blockers": baseline.get("blockers_json", []),
                "online_claim_anchors": frozen.get("baseline_claim_anchors", []),
                "shadow": [
                    {
                        "section_key": r["section_key"],
                        "repeat": r["repeat"],
                        "deterministic": r["deterministic"],
                        "status": r["status"],
                        "llm_checks": checks(r["llm"]),
                    }
                    for r in run["results_json"]
                ],
                "note": "Side-by-side evidence, not FP/FN; missing baseline is not comparable.",
            }
        )
        for r in run["results_json"]:
            groups[r["input_hash"]].append(r)
            calls.extend(r.get("calls", []))
    rows = [r for rs in groups.values() for r in rs]
    stable = sum(
        len(rs) == 3
        and all(signature(r) is not None for r in rs)
        and len({signature(r) for r in rs}) == 1
        and {r["repeat"] for r in rs} == {1, 2, 3}
        for rs in groups.values()
    )
    failures = sum(r["status"] != "assessed" for r in rows)
    ambiguity = sum(r.get("source_ambiguity", False) for r in rows)
    agreement = stable / len(expected_groups) if expected_groups else None
    confusions = {
        k: Counter(tp=0, tn=0, fp=0, fn=0, unassessed=0, unknown_positive=0, unknown_negative=0)
        for k in ("llm", "deterministic")
    }
    seen = set()
    gold_ambiguous = 0
    for label in gold or []:
        key = (label["input_hash"], label["claim_id"], label["layer"])
        if key in seen:
            raise ValueError("duplicate gold label")
        seen.add(key)
        material = inputs.get(key[0])
        claim = next(
            (c for c in (material or {}).get("claims", []) if c["claim_id"] == key[1]), None
        )
        if claim is None or label["layer"] not in confusions:
            raise ValueError("gold must reference an exact frozen input/claim/layer")
        if not label.get("reviewer") or not label.get("rationale") or not label.get("source_quote"):
            raise ValueError("gold requires independent reviewer, rationale and source quote")
        source_text = (
            claim["text"] + "\n" + "\n".join(e.get("text", "") for e in material["evidence"])
        )
        if label["source_quote"] not in source_text:
            raise ValueError("gold source quote cannot be verified")
        if label["label"] == "ambiguous":
            gold_ambiguous += 1
            continue
        if label["label"] not in {"violation", "clean"}:
            raise ValueError("invalid gold label")
        rs = groups[key[0]]
        positives, unknown = [], not rs or {r["repeat"] for r in rs} != {1, 2, 3}
        for r in rs:
            if label["layer"] == "deterministic":
                positives.append(any(c["claim_id"] == key[1] for c in r["deterministic"]))
            else:
                check = next((c for c in checks(r["llm"]) if c["claim_id"] == key[1]), None)
                unknown |= (
                    r["status"] != "assessed" or check is None or check["status"] == "uncertain"
                )
                positives.append(
                    check is not None and check["status"] in {"unsupported", "contradicted"}
                )
        c = confusions[label["layer"]]
        c["unassessed"] += bool(unknown)
        # Only fully assessed repeat groups produce observed FP/FN. Unknowns
        # contribute to pessimistic bounds used by the trust gate, not fabricated errors.
        if unknown:
            c["unknown_positive" if label["label"] == "violation" else "unknown_negative"] += 1
        elif label["label"] == "violation":
            c["fn" if not all(positives) else "tp"] += 1
        else:
            c["fp" if any(positives) else "tn"] += 1
    for c in confusions.values():
        c["fp_rate"] = c["fp"] / (c["fp"] + c["tn"]) if c["fp"] + c["tn"] else None
        c["fn_rate"] = c["fn"] / (c["fn"] + c["tp"]) if c["fn"] + c["tp"] else None
        positives = c["tp"] + c["fn"] + c["unknown_positive"]
        negatives = c["tn"] + c["fp"] + c["unknown_negative"]
        c["worst_case_fn_rate"] = (
            (c["fn"] + c["unknown_positive"]) / positives if positives else None
        )
        c["worst_case_fp_rate"] = (
            (c["fp"] + c["unknown_negative"]) / negatives if negatives else None
        )
    failure_rate = failures / len(rows) if rows else None
    ambiguity_rate = ambiguity / len(rows) if rows else None
    llm, deterministic = confusions["llm"], confusions["deterministic"]
    g = TRUST_GATE
    conditions = {
        "cohort": len(documents) >= g["minimum_documents"]
        and len(projects) >= g["minimum_projects"],
        "complete": bool(runs)
        and all(r["status"] == "completed" for r in runs)
        and len(rows) == total_expected,
        "repeat_coverage": len(expected_groups) >= g["minimum_repeated_sections"],
        "consistency": agreement is not None and agreement >= g["minimum_consistency"],
        "failure_rate": failure_rate is not None and failure_rate <= g["maximum_failure_rate"],
        "ambiguity_rate": ambiguity_rate is not None
        and ambiguity_rate <= g["maximum_ambiguity_rate"],
        "semantic_gold": llm["tp"] + llm["fn"] >= g["minimum_semantic_positive"]
        and llm["tn"] + llm["fp"] >= g["minimum_semantic_negative"],
        "semantic_fp_fn": (
            llm["worst_case_fp_rate"] is not None
            and llm["worst_case_fp_rate"] <= g["maximum_fp_rate"]
            and llm["worst_case_fn_rate"] is not None
            and llm["worst_case_fn_rate"] <= g["maximum_fn_rate"]
        ),
        "deterministic_gold": deterministic["tp"] + deterministic["fn"]
        >= g["minimum_deterministic_positive"],
        "deterministic_no_misses": deterministic["fn"] == 0 and deterministic["unassessed"] == 0,
    }
    return {
        "gate": TRUST_GATE,
        "conditions": conditions,
        "recommend_abc_review": all(conditions.values()),
        "automatic_actions": False,
        "documents": len(documents),
        "projects": len(projects),
        "expected_evaluations": total_expected,
        "finished_evaluations": len(rows),
        "failed_evaluations": failures,
        "failure_rate": failure_rate,
        "ambiguity_rate": ambiguity_rate,
        "stable_sections": stable,
        "repeat_consistency": agreement,
        "confusions": confusions,
        "gold_ambiguous": gold_ambiguous,
        "run_statuses": Counter(r["status"] for r in runs),
        "deterministic_findings": sum(len(r["deterministic"]) for r in rows),
        "llm_disagrees_with_deterministic": sum(
            not any(
                c["claim_id"] == finding["claim_id"]
                and c["status"] in {"unsupported", "contradicted"}
                for c in checks(r["llm"])
            )
            for r in rows
            for finding in r["deterministic"]
        ),
        "tokens": sum((c.get("input_tokens") or 0) + (c.get("output_tokens") or 0) for c in calls),
        "known_cost": sum(c.get("cost_estimate") or 0 for c in calls),
        "unpriced_calls": sum(c.get("cost_estimate") is None for c in calls),
        "differences": diffs,
        "limitations": "Bounded risk sample; repeated calls are not independent gold examples. "
        "Missing gold is unknown, not zero FP/FN. No release gate modification.",
    }


async def main(args):
    from db.models.paper import EvaluatorShadowRun
    from db.session import make_engine, make_session_factory

    from paperforge_worker.config import WorkerSettings
    from paperforge_worker.evaluator_shadow import VERSION

    settings = WorkerSettings()
    engine = make_engine(settings.database_url)
    try:
        async with make_session_factory(engine)() as session:
            rows = list(
                (
                    await session.scalars(
                        select(EvaluatorShadowRun)
                        .where(EvaluatorShadowRun.version == VERSION)
                        .order_by(EvaluatorShadowRun.created_at)
                    )
                ).all()
            )
            runs = [
                {
                    "id": str(r.id),
                    "project_id": str(r.project_id),
                    "status": r.status,
                    "input_json": r.input_json,
                    "results_json": r.results_json,
                }
                for r in rows
            ]
        report = summarize(runs, json.loads(args.gold.read_text()) if args.gold else None)
        args.output.mkdir(parents=True, exist_ok=False)
        (args.output / "runs.json").write_text(json.dumps(runs, ensure_ascii=False, indent=2))
        (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps({k: v for k, v in report.items() if k != "differences"}, indent=2))
    finally:
        await engine.dispose()


def cli():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--gold", type=Path)
    asyncio.run(main(parser.parse_args()))


if __name__ == "__main__":
    cli()
