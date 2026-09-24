"""Replay all three polish arms against one immutable before-polish snapshot."""

import argparse
import asyncio
import json
import time
import uuid
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from statistics import median

from db import (
    create_document,
    create_job,
    create_outline,
    get_job,
    get_writing_whitelist,
    update_job,
)
from db.session import make_engine, make_session_factory
from llm_runtime.experiment_budget import ExperimentBudget
from paperforge_worker.config import WorkerSettings
from paperforge_worker.context import job_context
from paperforge_worker.orchestration.writing_graph import fingerprint
from paperforge_worker.pipelines.document import (
    WriteOutcome,
    _persist_draft,
    _polish_all,
)
from paperforge_worker.pipelines.writing import SectionDraft, WritingContext
from paperforge_worker.worker import _quality, _section_review_inputs

from evals.agent_writing.run import evaluation_url
from evals.writing_efficiency.review import compare


async def run(args):
    args.output.mkdir(parents=True, exist_ok=False)
    snapshot = json.loads(args.snapshot.read_text())
    project_id = uuid.UUID(snapshot["project_id"])
    engine = make_engine(evaluation_url())
    factory = make_session_factory(engine)
    settings = WorkerSettings(database_url=evaluation_url(), writer_polish_concurrency=2)
    if not settings.llm_config().enabled or settings.llm_default_provider == "noop":
        raise ValueError("live configured model required")
    rows = []
    try:
        for repeat in range(3):
            policies = ["legacy", "full_parallel", "selective_parallel"]
            for policy in policies[repeat:] + policies[:repeat]:
                drafts = {k: SectionDraft(**deepcopy(v)) for k, v in snapshot["drafts"].items()}
                writing_context = WritingContext(**deepcopy(snapshot["context"]))
                async with factory() as session:
                    outline = await create_outline(
                        session,
                        project_id=project_id,
                        tree=writing_context.outline,
                        status="confirmed",
                    )
                    doc = await create_document(
                        session, project_id=project_id, outline_id=outline.id
                    )
                    job = await create_job(
                        session,
                        project_id=project_id,
                        kind="write",
                        checkpoint={"writer_polish_policy": policy, "writer_polish_concurrency": 2},
                    )
                    await update_job(session, job, status="running")
                    whitelist = await get_writing_whitelist(session, project_id)
                    await session.commit()
                async with job_context(
                    project_id=project_id, job_id=job.id, settings=settings, session_factory=factory
                ) as context:
                    for key, draft in drafts.items():
                        draft.expected_body_hash = None
                        await _persist_draft(
                            context,
                            document_id=doc.id,
                            draft=draft,
                            order_no=snapshot["order_by_key"][key],
                            whitelist=whitelist,
                            language=snapshot["language"],
                        )
                    started = time.monotonic()
                    outcome = WriteOutcome()
                    await _polish_all(
                        context,
                        drafts=drafts,
                        document_id=doc.id,
                        order_by_key=snapshot["order_by_key"],
                        whitelist=whitelist,
                        language=snapshot["language"],
                        writing_context=writing_context,
                        runner=context.llm_runner(),
                        outcome=outcome,
                    )
                    elapsed = time.monotonic() - started
                    calls = list(context.llm_calls)
                    report = await _quality(context)
                    inputs = await _section_review_inputs(context, include_unlinked=True)
                    rows.append(
                        {
                            "policy": policy,
                            "repeat": repeat,
                            "snapshot_hash": fingerprint(snapshot),
                            "job_id": str(job.id),
                            "elapsed_s": elapsed,
                            "known_polish_cost": sum(c.cost_estimate or 0 for c in calls),
                            "unpriced_polish_calls": sum(c.cost_estimate is None for c in calls),
                            "polish_calls": [asdict(c) for c in calls],
                            "calls": [asdict(c) for c in context.llm_calls],
                            "quality": report.to_payload(),
                            "review_inputs": inputs,
                            "results": {
                                k: d.generation.get("polish_result") for k, d in drafts.items()
                            },
                        }
                    )
                    (args.output / "runs.json").write_text(
                        json.dumps(rows, ensure_ascii=False, default=str)
                    )
                    async with context.session() as session:
                        await update_job(
                            session, await get_job(session, job.id), status="succeeded"
                        )
        reviews = {}
        with ExperimentBudget(args.budget_file, "calibration", currency="CNY").activate():
            async with factory() as session:
                review_job = await create_job(session, project_id=project_id, kind="quality")
                await update_job(session, review_job, status="running")
                await session.commit()
            async with job_context(
                project_id=project_id,
                job_id=review_job.id,
                settings=settings,
                session_factory=factory,
            ) as context:
                for repeat in range(3):
                    baseline = next(
                        r for r in rows if r["policy"] == "legacy" and r["repeat"] == repeat
                    )
                    for policy in ("full_parallel", "selective_parallel"):
                        candidate = next(
                            r for r in rows if r["policy"] == policy and r["repeat"] == repeat
                        )
                        reviews[f"{policy}:{repeat}"] = await compare(
                            context.llm_runner(), baseline, candidate
                        )
                        (args.output / "ai-reviews.json").write_text(json.dumps(reviews, indent=2))
                async with context.session() as session:
                    await update_job(
                        session, await get_job(session, review_job.id), status="succeeded"
                    )
        result = {
            "runs": len(rows),
            "scope": "polish stage only",
            "release_eligible": False,
            "snapshot_hash": fingerprint(snapshot),
            "median_seconds": {
                p: median(r["elapsed_s"] for r in rows if r["policy"] == p)
                for p in ("legacy", "full_parallel", "selective_parallel")
            },
            "ai_review_passed": {
                p: all(reviews[f"{p}:{i}"].get("passed") is True for i in range(3))
                for p in ("full_parallel", "selective_parallel")
            },
            "human_review_complete": False,
        }
        (args.output / "summary.json").write_text(json.dumps(result, indent=2))
        return result
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--budget-file", type=Path, required=True)
    parser.add_argument("--price-currency", choices=["CNY"], required=True)
    parser.add_argument("--live", action="store_true", required=True)
    args = parser.parse_args()
    with ExperimentBudget(args.budget_file, "generation", currency="CNY").activate():
        print(json.dumps(asyncio.run(run(args))))
