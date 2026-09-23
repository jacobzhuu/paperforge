"""Re-review immutable paired runs using complete batched evidence inputs."""

import argparse
import asyncio
import json
import uuid
from pathlib import Path

from db import create_job, get_job, update_job
from db.session import make_engine, make_session_factory
from llm_runtime.experiment_budget import ExperimentBudget
from paperforge_worker.config import WorkerSettings
from paperforge_worker.context import job_context
from paperforge_worker.orchestration.writing_graph import fingerprint

from evals.agent_writing.run import evaluation_url
from evals.writing_efficiency.report import analyze
from evals.writing_efficiency.review import VERSION, compare


async def run(args):
    rows = json.loads(args.runs.read_text())
    args.output.mkdir(parents=True, exist_ok=False)
    settings = WorkerSettings(database_url=evaluation_url())
    if not settings.llm_config().enabled or settings.llm_default_provider == "noop":
        raise ValueError("live configured model required")
    engine = make_engine(evaluation_url())
    factory = make_session_factory(engine)
    reviews = {}
    try:
        async with factory() as session:
            job = await create_job(session, project_id=args.project_id, kind="quality")
            await update_job(session, job, status="running")
            await session.commit()
        with ExperimentBudget(args.budget_file, "calibration", currency="CNY").activate():
            async with job_context(
                project_id=args.project_id,
                job_id=job.id,
                settings=settings,
                session_factory=factory,
            ) as context:
                for repeat in range(3):
                    pair = [
                        next(r for r in rows if r["repeat"] == repeat and r["condition"] == arm)
                        for arm in ("baseline", "optimized")
                    ]
                    reviews[str(repeat)] = await compare(context.llm_runner(), *pair)
                    (args.output / "ai-reviews.json").write_text(json.dumps(reviews, indent=2))
                async with context.session() as session:
                    await update_job(session, await get_job(session, job.id), status="succeeded")
        result = {
            **analyze(rows, reviews),
            "review_version": VERSION,
            "source_runs_hash": fingerprint(rows),
            "review_job_id": str(job.id),
        }
        (args.output / "summary.json").write_text(json.dumps(result, indent=2))
        return result
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-id", type=uuid.UUID, required=True)
    parser.add_argument("--runs", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--budget-file", type=Path, required=True)
    parser.add_argument("--price-currency", choices=["CNY"], required=True)
    parser.add_argument("--live", action="store_true", required=True)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args)), indent=2))
