"""Live paired A/B/C runs on a restored *_eval database; never the public project.

    PAPERFORGE_EVAL_DATABASE_URL=.../paperforge_eval uv run python -m \
        evals.agent_writing.run --protocol frames-v2 --project-id UUID --live \
        --budget-file /private/budget.sqlite --price-currency CNY --output runs.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
import uuid
from dataclasses import asdict
from pathlib import Path

from db import (
    create_job,
    get_job,
    get_project,
    get_writing_whitelist,
    grounded_asset_payloads,
    latest_outline,
    update_job,
)
from db.models.paper import ClaimEntailmentCache
from db.session import make_engine, make_session_factory
from paperforge_worker.config import WorkerSettings
from paperforge_worker.context import job_context
from paperforge_worker.orchestration.writing_graph import fingerprint
from paperforge_worker.pipelines.document import _card_context, build_markdown, write_document
from paperforge_worker.pipelines.review_inputs import REVIEW_INPUT_VERSION
from paperforge_worker.pipelines.semantic_review import review_section
from paperforge_worker.worker import _quality, _section_review_inputs
from sqlalchemy import delete, insert, select
from sqlalchemy.engine import make_url

from evals.agent_writing.frames_protocol import CONDITIONS, CONFIGURATIONS, PROTOCOL, analyze


def evaluation_url() -> str:
    url = os.environ.get("PAPERFORGE_EVAL_DATABASE_URL", "")
    if not url or not (make_url(url).database or "").endswith("_eval"):
        raise ValueError("PAPERFORGE_EVAL_DATABASE_URL must name an isolated *_eval database")
    return url


async def run(
    args,
    *,
    settings=None,
    conditions=CONDITIONS,
    configurations=CONFIGURATIONS,
    protocol=PROTOCOL,
    configure=None,
    prepare_outline=None,
    capture_inputs=False,
) -> list[dict]:
    settings = settings or WorkerSettings(database_url=evaluation_url())
    if not settings.llm_config().enabled or settings.llm_default_provider == "noop":
        raise ValueError("live evaluation requires a configured model")
    if args.output.exists():
        raise ValueError("output already exists; historical experiments are immutable")
    engine = make_engine(settings.database_url)
    factory = make_session_factory(engine)
    results = []
    try:
        async with factory() as session:
            project = await get_project(session, args.project_id)
            outline = await latest_outline(session, args.project_id)
            if project is None or outline is None:
                raise ValueError("restore the representative project and outline into the eval DB")
            source = {
                "outline": outline.tree_json,
                "cards": await _card_context(session, project.id),
                "assets": await grounded_asset_payloads(session, project.id),
                "whitelist": await get_writing_whitelist(session, project.id),
            }
            cached = list(
                (
                    await session.scalars(
                        select(ClaimEntailmentCache).where(
                            ClaimEntailmentCache.project_id == project.id
                        )
                    )
                ).all()
            )
            warm_rows = (
                [{c.name: getattr(row, c.name) for c in row.__table__.columns} for row in cached]
                if args.cache_mode == "warm"
                else []
            )
            publication = (project.publication_title, project.keywords_json)
        model = settings.llm_config()
        # Configuration identity deliberately excludes credentials/base URL.
        model_hash = fingerprint(
            {
                "roles": settings.llm_role_models,
                "thinking": settings.llm_role_thinking,
                "retry": settings.llm_role_retry,
                "provider": model.provider,
                "resolved_models": {
                    role: model.model_for_role(role)
                    for role in ("writer", "verifier", "planner", "synthesizer", "section_reviewer")
                },
            }
        )
        for repeat in range(args.repeats):
            # Rotate condition order to reduce drift/time-of-day bias.
            order = conditions[repeat % len(conditions) :] + conditions[: repeat % len(conditions)]
            for condition in order:
                mode, frame_width = configurations[condition]
                settings.writer_execution_mode = mode
                settings.writer_concurrency = 2
                settings.writer_frame_concurrency = frame_width
                if configure is not None:
                    configure(settings, condition)
                current_outline = outline
                if prepare_outline is not None:
                    current_outline = await prepare_outline(factory, outline, condition)
                async with factory() as session:
                    run_project = await get_project(session, project.id)
                    run_project.publication_title, run_project.keywords_json = publication
                    await session.execute(
                        delete(ClaimEntailmentCache).where(
                            ClaimEntailmentCache.project_id == project.id
                        )
                    )
                    if warm_rows:
                        await session.execute(insert(ClaimEntailmentCache), warm_rows)
                    job = await create_job(session, project_id=project.id, kind="write")
                    await update_job(session, job, status="running")
                    await session.commit()
                async with job_context(
                    project_id=project.id, job_id=job.id, settings=settings, session_factory=factory
                ) as context:
                    started = time.monotonic()
                    outcome = await write_document(
                        context,
                        language=project.language,
                        paper_type=project.paper_type,
                        outline_id=current_outline.id,
                        title=project.title,
                    )
                    writing_seconds = time.monotonic() - started
                    report = await _quality(context)
                    verdicts = []
                    review_inputs = await _section_review_inputs(context)
                    for item in review_inputs:
                        verdict = await review_section(
                            section_key=item["section_key"],
                            question=item["question"],
                            prose=item["prose"],
                            evidence=item["evidence"],
                            review_input=item["review_input"],
                            trace_context=context,
                            runner=context.llm_runner(),
                            language=project.language,
                        )
                        verdicts.append(verdict.to_payload() if verdict else {"unassessed": True})
                    calls = list(context.llm_calls)
                    tokens_known = all(
                        c.input_tokens is not None and c.output_tokens is not None for c in calls
                    )
                    row = {
                        "fixture_hash": fingerprint(source),
                        "model_config_hash": model_hash,
                        "repeat": repeat,
                        "condition": condition,
                        "protocol": protocol,
                        "polish_policy": settings.writer_polish_policy,
                        "outline_hash": fingerprint(current_outline.tree_json),
                        "writer_execution_mode": mode,
                        "body_concurrency": 2,
                        "frame_concurrency": frame_width,
                        "evaluator_version": REVIEW_INPUT_VERSION,
                        "cache_mode": args.cache_mode,
                        "job_id": str(job.id),
                        "document_id": outcome.document_id,
                        "writing_seconds": writing_seconds,
                        "total_seconds": time.monotonic() - started,
                        "complete": outcome.complete,
                        "tokens": sum(c.input_tokens + c.output_tokens for c in calls)
                        if tokens_known
                        else None,
                        "known_cost": sum(c.cost_estimate or 0 for c in calls),
                        "unpriced_calls": sum(c.cost_estimate is None for c in calls),
                        "quality": report.to_payload(),
                        "hard_violations": sorted(
                            str(b.get("code", b.get("rule", b)))
                            for b in report.blockers
                            for _ in range(max(1, int(b.get("count", 1))))
                        ),
                        "semantic_verdicts": verdicts,
                        "calls": [asdict(c) for c in calls],
                        "expert_verified": False,
                    }
                    if capture_inputs:
                        row["review_inputs"] = await _section_review_inputs(
                            context, include_unlinked=True
                        )
                    results.append(row)
                    manuscript = await build_markdown(context)
                    args.output.parent.mkdir(parents=True, exist_ok=True)
                    args.output.with_name(f"{job.id}.md").write_text(manuscript)
                    args.output.write_text(
                        json.dumps(results, ensure_ascii=False, indent=2, default=str)
                    )
                    await context.raise_if_stopped()
                    async with context.session() as session:
                        run_job = await get_job(session, job.id)
                        await update_job(
                            session,
                            run_job,
                            status="succeeded" if outcome.complete else "failed",
                            progress=1.0,
                        )
        return results
    finally:
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--protocol", choices=[PROTOCOL], required=True)
    parser.add_argument("--project-id", type=uuid.UUID, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--cache-mode", choices=("cold", "warm"), default="cold")
    parser.add_argument("--budget-file", type=Path, required=True)
    parser.add_argument("--price-currency", choices=["CNY"], required=True)
    parser.add_argument("--live", action="store_true", help="explicitly enable paid model calls")
    args = parser.parse_args()
    if not args.live or args.repeats < 3:
        parser.error("--live and at least 3 repeats are required")
    from llm_runtime.experiment_budget import ExperimentBudget

    with ExperimentBudget(args.budget_file, "generation", currency=args.price_currency).activate():
        print(json.dumps(analyze(asyncio.run(run(args))), indent=2))


if __name__ == "__main__":
    main()
