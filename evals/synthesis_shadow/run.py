"""跑一次 Phase 4 影子评估。

    python -m evals.synthesis_shadow.run --project <uuid> [--project <uuid> ...]
    python -m evals.synthesis_shadow.run --all --limit 5 --persist --output report.json

走的是**生产同一条路径**：同一个 ``load_synthesis_bundles``、同一个提示词、同一套
``build_synthesis`` 准入规则。这里唯一多做的事是把每一条被丢弃的条目按原因记下来
——生产上那些原因只汇总成一个计数，评估要的恰恰是分布。

默认不写库。加 ``--persist`` 才写 ``question_synthesis`` 行（供人工核对），
它是纯增量的，删掉即可回滚。任何情况下都不产出正文、不改 ``answer_status``。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

import httpx
from db import list_project_task_specs, list_projects, upsert_question_synthesis
from db.session import make_engine, make_session_factory
from llm_runtime import LLMCallRecord
from paperforge_worker.config import WorkerSettings
from paperforge_worker.context import JobContext
from paperforge_worker.pipelines.synthesis import load_synthesis_bundles
from paperforge_worker.pipelines.synthesis_llm import (
    CORE_DIMENSIONS,
    bundle_fingerprint,
    eligible_unit_count,
    should_synthesize,
    synthesize_bundle,
)
from scholar_gateway import InMemoryHttpCache

from evals.synthesis_shadow.report import BundleObservation, ShadowReport, render


async def evaluate(
    *,
    project_ids: list[uuid.UUID],
    settings: WorkerSettings,
    persist: bool,
    max_questions: int,
) -> ShadowReport:
    engine = make_engine(settings.database_url, application_name="paperforge-synthesis-shadow")
    session_factory = make_session_factory(engine)
    report = ShadowReport()
    calls: list[LLMCallRecord] = []
    try:
        for project_id in project_ids:
            context = JobContext(
                project_id=project_id,
                job_id=None,
                settings=settings,
                session_factory=session_factory,
                http_client=httpx.Client(),
                scholar_cache=InMemoryHttpCache(),
            )
            context.llm_calls = calls
            await _evaluate_project(
                context,
                report=report,
                persist=persist,
                max_questions=max_questions,
            )
    finally:
        await engine.dispose()
    _tally_cost(report, calls=calls, settings=settings)
    return report


async def _evaluate_project(
    context: JobContext,
    *,
    report: ShadowReport,
    persist: bool,
    max_questions: int,
) -> None:
    outcome = await load_synthesis_bundles(context)
    async with context.session() as session:
        specs = await list_project_task_specs(
            session,
            context.project_id,
            fallback=context.settings.task_profile_fallback,
        )
    dimensions = frozenset(
        {str(value).casefold() for spec in specs for value in spec.dimensions}
    ) | frozenset(CORE_DIMENSIONS)
    runner = context.llm_runner()
    budget = max_questions

    for bundle in outcome.bundles:
        observation = _observe(bundle, project_id=str(context.project_id))
        report.observations.append(observation)
        if not should_synthesize(bundle):
            observation.skip_reason = (
                "insufficient_evidence"
                if bundle["answer_status"] == "insufficient_evidence"
                else "below_evidence_floor"
            )
            continue
        if budget <= 0:
            observation.skip_reason = "budget_exhausted"
            continue
        if not runner.enabled:
            # noop provider：管线自检用，不该被算成一次"没留下东西"的调用。
            observation.skip_reason = "runner_disabled"
            continue
        budget -= 1
        observation.called = True
        result = await synthesize_bundle(
            bundle=bundle,
            runner=runner,
            allowed_dimensions=dimensions,
            language="zh",
        )
        if result is None:
            continue
        observation.parsed = True
        observation.claim_kept = bool(result.claim)
        observation.claim = result.claim
        observation.entries = [
            {"kind": entry.kind, **entry.to_payload()}
            for group in (result.agreement, result.conditional, result.conflict, result.gap)
            for entry in group
        ]
        observation.accepted = {
            "agreement": len(result.agreement),
            "conditional": len(result.conditional),
            "conflict": len(result.conflict),
            "gap": len(result.gap),
        }
        for item in result.rejected:
            observation.rejected[str(item.get("reason") or "unknown")] += 1
            observation.rejections.append(dict(item))
        if persist:
            async with context.session() as session:
                await upsert_question_synthesis(
                    session,
                    project_id=context.project_id,
                    research_question_id=uuid.UUID(bundle["question_id"]),
                    bundle_hash=bundle_fingerprint(bundle),
                    generator=result.generator,
                    claim=result.claim,
                    agreement=[entry.to_payload() for entry in result.agreement],
                    conditional=[entry.to_payload() for entry in result.conditional],
                    conflict=[entry.to_payload() for entry in result.conflict],
                    gap=[entry.to_payload() for entry in result.gap],
                )


def _observe(bundle: dict[str, Any], *, project_id: str) -> BundleObservation:
    evidence = bundle.get("evidence") or []
    return BundleObservation(
        project_id=project_id,
        question_id=str(bundle.get("question_id")),
        question=str(bundle.get("question") or "")[:120],
        answer_status=str(bundle.get("answer_status") or ""),
        evidence_units=len(evidence),
        fulltext_units=eligible_unit_count(bundle),
        distinct_works=len({str(row.get("work_id")) for row in evidence if row.get("work_id")}),
        comparison_clusters=len(bundle.get("comparison_clusters") or []),
    )


def _tally_cost(
    report: ShadowReport, *, calls: list[LLMCallRecord], settings: WorkerSettings
) -> None:
    priced: list[float] = []
    for record in calls:
        if record.error_code:
            report.failed_calls += 1
            report.failure_reasons[record.error_code] += 1
            continue
        report.input_tokens += record.input_tokens or 0
        report.output_tokens += record.output_tokens or 0
        report.model = report.model or record.model
        if record.cost_estimate is None:
            report.unpriced_calls += 1
        else:
            priced.append(record.cost_estimate)
    report.cost = sum(priced) if priced else None


def _resolve_projects(args: argparse.Namespace, settings: WorkerSettings) -> list[uuid.UUID]:
    if args.project:
        return [uuid.UUID(value) for value in args.project]

    async def _list() -> list[uuid.UUID]:
        engine = make_engine(settings.database_url, application_name="paperforge-synthesis-shadow")
        session_factory = make_session_factory(engine)
        try:
            async with session_factory() as session:
                projects = await list_projects(session)
                return [project.id for project in projects][: args.limit]
        finally:
            await engine.dispose()

    return asyncio.run(_list())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", action="append", help="project id; repeatable")
    parser.add_argument("--all", action="store_true", help="evaluate every project")
    parser.add_argument("--limit", type=int, default=5, help="cap for --all")
    parser.add_argument(
        "--max-questions",
        type=int,
        default=8,
        help="per-project call cap, mirroring SYNTHESIS_LLM_MAX_QUESTIONS",
    )
    parser.add_argument(
        "--persist",
        action="store_true",
        help="write question_synthesis rows for manual review (additive; delete to undo)",
    )
    parser.add_argument("--output", type=Path, help="write the JSON payload here")
    args = parser.parse_args()
    if not args.project and not args.all:
        raise SystemExit("pass --project <uuid> or --all")

    settings = WorkerSettings()
    # 影子评估不受生产开关约束：它自己直接调用综合，不经过 enrich_with_synthesis。
    project_ids = _resolve_projects(args, settings)
    if not project_ids:
        raise SystemExit("no projects matched")
    report = asyncio.run(
        evaluate(
            project_ids=project_ids,
            settings=settings,
            persist=args.persist,
            max_questions=args.max_questions,
        )
    )
    payload = report.to_payload()
    payload["projects"] = [str(value) for value in project_ids]
    payload["persisted"] = args.persist
    print(render(payload))
    if args.output:
        args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nJSON written to {args.output}")


if __name__ == "__main__":
    main()
