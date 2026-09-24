"""Opt-in real-model evaluator regression; no writing benchmark or database access."""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict
from pathlib import Path

import httpx
from paperforge_worker.config import WorkerSettings
from paperforge_worker.context import JobContext
from paperforge_worker.orchestration.writing_graph import fingerprint
from paperforge_worker.pipelines.review_contract import table_role_conflicts
from paperforge_worker.pipelines.review_inputs import REVIEW_INPUT_VERSION
from paperforge_worker.pipelines.semantic_review import review_section


class ReviewContext(JobContext):
    output: Path

    async def emit(self, name, payload=None, **kwargs):
        with (self.output / "trace.jsonl").open("a") as f:
            f.write(
                json.dumps({"event": name, "payload": payload}, ensure_ascii=False, default=str)
                + "\n"
            )

    async def _save_call(self, record):
        with (self.output / "llm_call_log.jsonl").open("a") as f:
            f.write(json.dumps(asdict(record), ensure_ascii=False, default=str) + "\n")


def claim_outcome(verdict) -> str:
    """Score claim grounding separately from unchanged whole-section acceptance."""
    if verdict is None or not verdict.claim_checks:
        return "unassessed"
    if verdict.deterministic_findings or any(
        c["status"] in {"unsupported", "contradicted"} for c in verdict.claim_checks
    ):
        return "reject"
    if any(c["status"] == "uncertain" for c in verdict.claim_checks):
        return "unassessed"
    return "accept"


async def run(output: Path) -> None:
    import uuid

    output.mkdir(parents=True, exist_ok=False)
    settings = WorkerSettings(storage_backend="filesystem", storage_fs_root=str(output / "objects"))
    context = ReviewContext(
        project_id=uuid.UUID("d86f630b-b837-401d-8ac7-114888cb1926"),
        job_id=uuid.uuid4(),
        settings=settings,
        session_factory=None,
        http_client=httpx.Client(),
        scholar_cache=None,
    )
    context.output = output
    runner = context.llm_runner()
    if not runner.enabled:
        raise RuntimeError("real model required")
    fixtures = json.loads(Path(__file__).with_name("cases.json").read_text())
    import hashlib

    tracked = [Path(__file__), Path(__file__).with_name("cases.json")]
    tracked.extend(Path("services/worker/paperforge_worker/pipelines").glob("review_*.py"))
    tracked.append(Path("services/worker/paperforge_worker/pipelines/semantic_review.py"))
    (output / "source-sha256.json").write_text(
        json.dumps(
            {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(tracked)}, indent=2
        )
    )
    results = []
    try:
        for index, case in enumerate(fixtures):
            claim = case["claim"]
            material = {
                "version": REVIEW_INPUT_VERSION,
                "section_key": f"case{index + 1:03}",
                "question": case["question"],
                "prose": claim["text"],
                "claims": [claim],
                "evidence": case["evidence"],
                "binding_issues": [],
                "scope": {"purpose": "evaluator_trust_regression"},
            }
            material["table_role_conflicts"] = table_role_conflicts(material)
            material["input_hash"] = fingerprint(material)
            verdict = await review_section(
                section_key=f"case{index + 1:03}",
                question=case["question"],
                prose=claim["text"],
                evidence=case["evidence"],
                review_input=material,
                runner=runner,
                trace_context=context,
            )
            actual = claim_outcome(verdict)
            results.append(
                {
                    "id": case["id"],
                    "expected": case["expected"],
                    "actual": actual,
                    "section_acceptable": verdict.acceptable if verdict else None,
                    "passed": actual == case["expected"],
                    "input_hash": material["input_hash"],
                    "verdict": verdict.to_payload() if verdict else None,
                }
            )
            (output / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))
        calls = context.llm_calls
        summary = {
            "version": REVIEW_INPUT_VERSION,
            "cases": len(results),
            "passed": sum(x["passed"] for x in results),
            "unassessed": sum(x["actual"] == "unassessed" for x in results),
            "tokens": sum((c.input_tokens or 0) + (c.output_tokens or 0) for c in calls),
            "known_cost": sum(c.cost_estimate or 0 for c in calls),
            "unpriced_calls": sum(c.cost_estimate is None for c in calls),
            "benchmark_started": False,
            "scope": "Targeted AI regression cases; not universal accuracy or human review.",
        }
        (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
        print(json.dumps(summary, ensure_ascii=False))
    finally:
        context.http_client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--budget-file", type=Path, required=True)
    parser.add_argument("--price-currency", choices=["CNY"], required=True)
    args = parser.parse_args()
    from llm_runtime.experiment_budget import ExperimentBudget

    with ExperimentBudget(args.budget_file, "calibration", currency=args.price_currency).activate():
        asyncio.run(run(args.output))
