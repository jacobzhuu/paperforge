"""Sequential, budgeted paired writing experiments in a restored *_eval database."""

import argparse
import asyncio
import json
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path

from db import create_job, get_outline, update_job
from db.session import make_engine, make_session_factory
from llm_runtime.experiment_budget import ExperimentBudget
from paperforge_worker.config import WorkerSettings
from paperforge_worker.context import job_context
from paperforge_worker.orchestration.writing_graph import fingerprint
from paperforge_worker.pipelines import document
from paperforge_worker.worker import run_dependency_rebuild_pipeline

from evals.agent_writing.run import evaluation_url
from evals.agent_writing.run import run as writing_run
from evals.writing_efficiency.report import PROTOCOL, analyze
from evals.writing_efficiency.review import compare


async def run(args):
    args.output.mkdir(parents=True, exist_ok=False)
    settings = WorkerSettings(database_url=evaluation_url())
    if not settings.llm_config().enabled or settings.llm_default_provider == "noop":
        raise ValueError("live experiment requires a configured model")
    settings.writer_concurrency = 2
    settings.writer_polish_concurrency = 2
    factory_engine = make_engine(evaluation_url())
    factory = make_session_factory(factory_engine)
    graph = None
    original_polish = document._polish_all

    async def capture(context, **kwargs):
        # Save the exact before-polish workload for later ablations; never publish this file.
        snapshot = {
            "project_id": str(context.project_id),
            "job_id": str(context.job_id),
            "context": asdict(kwargs["writing_context"]),
            "drafts": {k: asdict(v) for k, v in kwargs["drafts"].items()},
            "order_by_key": kwargs["order_by_key"],
            "language": kwargs["language"],
            "policy": context.checkpoint.get("writer_polish_policy", "legacy"),
        }
        target = args.output / f"{context.job_id}.pre-polish.json"
        if not target.exists():
            target.write_text(json.dumps(snapshot, ensure_ascii=False, default=str))
        return await original_polish(context, **kwargs)

    async def prepare(factory, source, condition):
        nonlocal graph
        if condition == "baseline":
            return source
        if graph is None:
            async with factory() as session:
                job = await create_job(session, project_id=source.project_id, kind="outline")
                await session.commit()
            result = await run_dependency_rebuild_pipeline(
                {"settings": settings, "session_factory": factory},
                str(source.project_id),
                str(job.id),
                str(source.id),
                fingerprint(source.tree_json),
            )
            async with factory() as session:
                import uuid

                graph = await get_outline(session, uuid.UUID(result["outline_id"]))
                graph.status = "confirmed"
                await session.commit()
            (args.output / "dependency-plan.json").write_text(
                json.dumps(graph.tree_json, ensure_ascii=False, default=str, indent=2)
            )
        return graph

    def configure(settings, condition):
        settings.writer_polish_policy = (
            "legacy" if condition == "baseline" else "selective_parallel"
        )

    writer_args = deepcopy(args)
    writer_args.output = args.output / "runs.json"
    writer_args.cache_mode = "cold"
    writer_args.repeats = 3
    document._polish_all = capture
    try:
        with ExperimentBudget(args.budget_file, args.phase, currency="CNY").activate():
            rows = await writing_run(
                writer_args,
                settings=settings,
                conditions=("baseline", "optimized"),
                configurations={"baseline": ("dag_parallel", 1), "optimized": ("dag_parallel", 1)},
                protocol=PROTOCOL,
                configure=configure,
                prepare_outline=prepare,
                capture_inputs=True,
            )
        reviews = {}
        with ExperimentBudget(args.budget_file, "calibration", currency="CNY").activate():
            async with factory() as session:
                job = await create_job(session, project_id=args.project_id, kind="quality")
                await session.commit()
            async with job_context(
                project_id=args.project_id,
                job_id=job.id,
                settings=settings,
                session_factory=factory,
            ) as context:
                for repeat in range(3):
                    before = next(
                        r for r in rows if r["condition"] == "baseline" and r["repeat"] == repeat
                    )
                    after = next(
                        r for r in rows if r["condition"] == "optimized" and r["repeat"] == repeat
                    )
                    reviews[str(repeat)] = await compare(context.llm_runner(), before, after)
                    (args.output / "ai-reviews.json").write_text(json.dumps(reviews, indent=2))
                async with context.session() as session:
                    from db import get_job

                    await update_job(session, await get_job(session, job.id), status="succeeded")
        report = analyze(rows, reviews)
        (args.output / "summary.json").write_text(json.dumps(report, indent=2))
        return report
    finally:
        document._polish_all = original_polish
        await factory_engine.dispose()


if __name__ == "__main__":
    import uuid

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-id", type=uuid.UUID, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--budget-file", type=Path, required=True)
    parser.add_argument("--price-currency", choices=["CNY"], required=True)
    parser.add_argument("--phase", choices=["generation", "retest"], default="generation")
    parser.add_argument("--live", action="store_true", required=True)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args)), indent=2))
