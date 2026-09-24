"""Explicit live semantic replay of frozen manuscripts; never executes the writer."""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
from paperforge_worker.config import WorkerSettings
from paperforge_worker.orchestration.writing_graph import fingerprint

from evals.evaluator_trust.manuscript_review import build_manuscript_input, review_manuscript
from evals.evaluator_trust.run import ReviewContext


async def run(source: Path, output: Path) -> None:
    import hashlib

    for name, expected in json.loads((source / "review/input-sha256.json").read_text()).items():
        assert hashlib.sha256((source / name).read_bytes()).hexdigest() == expected, name
    snapshot = json.loads((source / "review/db-snapshot.json").read_text())
    runs = json.loads((source / "cold.json").read_text())
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
    results = []
    try:
        for run in runs:
            rows = [
                SimpleNamespace(**r)
                for r in snapshot["sections"]
                if r["document_id"] == run["document_id"]
            ]
            material = build_manuscript_input(
                rows,
                {e["id"]: e for e in snapshot["evidence"]},
                snapshot=fingerprint([r.body_ir_json for r in rows]),
            )
            result = await review_manuscript(material, runner=runner, context=context)
            results.append({"job_id": run["job_id"], "review": result})
            (output / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))
        calls = context.llm_calls
        summary = {
            "manuscripts": len(results),
            "assessed": sum(r["review"]["status"] == "assessed" for r in results),
            "tokens": sum((c.input_tokens or 0) + (c.output_tokens or 0) for c in calls),
            "known_cost": sum(c.cost_estimate or 0 for c in calls),
            "unpriced_calls": sum(c.cost_estimate is None for c in calls),
            "benchmark_started": False,
        }
        (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
        print(json.dumps(summary))
    finally:
        context.http_client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--budget-file", type=Path, required=True)
    parser.add_argument("--price-currency", choices=["CNY"], required=True)
    args = parser.parse_args()
    from llm_runtime.experiment_budget import ExperimentBudget

    with ExperimentBudget(args.budget_file, "calibration", currency=args.price_currency).activate():
        asyncio.run(run(args.source, args.output))
