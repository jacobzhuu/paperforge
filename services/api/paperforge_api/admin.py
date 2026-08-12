from __future__ import annotations

import argparse
import asyncio
import csv
import getpass
import hashlib
import json
import sys
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from db import (
    create_user,
    get_user_by_email,
    list_purgeable_projects,
    list_task_definitions,
    purge_project,
    task_ontology_warnings,
    task_spec_to_payload,
    upsert_task_definitions,
    validate_task_payloads,
)
from db.models.auth import AppUser
from db.models.library import DocumentFile, LiteraturePdfUpload
from db.models.paper import ExportArtifact, PaperProject, UserAsset, VisualAsset
from db.repositories.jobs import unpriced_call_count
from db.session import make_engine, make_session_factory
from sqlalchemy import func, select, update
from storage import make_object_store

from paperforge_api.auth_service import hash_password
from paperforge_api.config import get_settings
from paperforge_api.routers.projects import TASK_PROFILE_USER_BOUND_KEY
from paperforge_api.storage_migration import (
    export_storage,
    migrate_storage,
    restore_storage,
    storage_object_count,
    write_source_manifest,
)

LEGACY_USER_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")


def main() -> None:
    parser = argparse.ArgumentParser(description="PaperForge account administration")
    commands = parser.add_subparsers(dest="command", required=True)
    bootstrap = commands.add_parser(
        "bootstrap-admin", help="create/activate the first administrator"
    )
    bootstrap.add_argument("--email", required=True)
    bootstrap.add_argument("--display-name", default="PaperForge administrator")
    bootstrap.add_argument("--claim-legacy", action="store_true")
    bootstrap.add_argument("--project-map", type=Path)
    bootstrap.add_argument("--migrate-objects", action="store_true")
    bootstrap.add_argument(
        "--password-stdin",
        action="store_true",
        help="read one password line from stdin (for secret-managed automation)",
    )
    migrate = commands.add_parser("migrate-objects", help="copy legacy keys into tenant namespaces")
    migrate.add_argument("--dry-run", action="store_true")
    manifest = commands.add_parser(
        "storage-manifest", help="create a SHA-256 manifest for a filesystem object tree"
    )
    manifest.add_argument("--source-root", type=Path, required=True)
    manifest.add_argument("--manifest", type=Path, required=True)
    export = commands.add_parser(
        "export-storage", help="export the configured object store to a verified archive"
    )
    export.add_argument("--archive", type=Path, required=True)
    export.add_argument("--manifest", type=Path, required=True)
    migrate_storage_parser = commands.add_parser(
        "migrate-storage", help="copy a filesystem object tree into the configured object store"
    )
    migrate_storage_parser.add_argument("--source-root", type=Path, required=True)
    migrate_storage_parser.add_argument("--manifest", type=Path, required=True)
    migrate_storage_parser.add_argument("--dry-run", action="store_true")
    migrate_storage_parser.add_argument("--verify-only", action="store_true")
    restore = commands.add_parser(
        "restore-storage", help="restore a verified archive into the configured object store"
    )
    restore.add_argument("--archive", type=Path, required=True)
    restore.add_argument("--manifest", type=Path, required=True)
    restore.add_argument("--dry-run", action="store_true")
    restore.add_argument("--verify-only", action="store_true")
    storage_count = commands.add_parser(
        "storage-count", help="count objects in the configured object store"
    )
    storage_count.add_argument("--require-empty", action="store_true")
    purge = commands.add_parser(
        "purge-projects",
        help="permanently delete soft-deleted projects past their retention window",
    )
    purge.add_argument(
        "--days",
        type=int,
        default=30,
        help="retention window in days; only projects deleted before it are purged",
    )
    purge.add_argument("--dry-run", action="store_true")

    tasks = commands.add_parser("tasks", help="inspect and author the task ontology (R16)")
    task_commands = tasks.add_subparsers(dest="task_command", required=True)
    task_list = task_commands.add_parser("list", help="print the task ontology")
    task_list.add_argument("--domain")
    task_export = task_commands.add_parser("export", help="write the ontology to JSON")
    task_export.add_argument("--output", type=Path, required=True)
    task_upsert = task_commands.add_parser("upsert", help="load an ontology JSON file")
    task_upsert.add_argument("--file", type=Path, required=True)
    task_upsert.add_argument(
        "--dry-run",
        action="store_true",
        help="validate and print the diff without writing",
    )
    task_commands.add_parser(
        "coverage",
        help="report how many projects are bound, i.e. whether generic_only is safe yet",
    )
    task_suggest = task_commands.add_parser(
        "suggest",
        help="propose a task binding for every unbound project (dry run unless --apply)",
    )
    task_suggest.add_argument(
        "--apply",
        action="store_true",
        help="write the proposals; without it nothing is changed",
    )
    task_suggest.add_argument(
        "--include-bound",
        action="store_true",
        help="also re-propose for projects that already have a binding",
    )

    cost = commands.add_parser(
        "cost",
        help="LLM spend by role and model, and which models are still unpriced",
    )
    cost.add_argument("--project", help="restrict to one project id")
    cost.add_argument("--job", help="restrict to one generation job id")
    cost.add_argument("--role", help="restrict to one LLM role, e.g. synthesizer")
    cost.add_argument("--since", help="ISO date; only calls at or after it")

    backfill = commands.add_parser(
        "backfill-dimensions",
        help="run LLM experiment extraction over existing evidence so comparability_key works",
    )
    backfill.add_argument("--project", action="append", required=True, help="project id")
    backfill.add_argument(
        "--linked-only",
        action="store_true",
        help="only works whose evidence a sub-question links to (what synthesis reads)",
    )
    backfill.add_argument("--limit", type=int, default=20, help="cap on works per run")
    backfill.add_argument(
        "--apply",
        action="store_true",
        help="write the dimensions; without it nothing is changed",
    )
    backfill.add_argument(
        "--min-verified-rate",
        type=float,
        default=0.8,
        help="refuse to apply below this locator-verified rate (Phase 2 promotion gate)",
    )

    args = parser.parse_args()
    if args.command == "bootstrap-admin":
        if args.password_stdin:
            password = sys.stdin.readline().rstrip("\r\n")
            if not password:
                raise SystemExit("no password was received on stdin")
        else:
            password = getpass.getpass("New administrator password: ")
            confirmation = getpass.getpass("Confirm password: ")
            if password != confirmation:
                raise SystemExit("passwords do not match")
        password_hash = hash_password(password)
        asyncio.run(_bootstrap(args, password_hash))
    elif args.command == "migrate-objects":
        asyncio.run(_migrate_objects(dry_run=args.dry_run))
    elif args.command == "purge-projects":
        asyncio.run(_purge_projects(days=args.days, dry_run=args.dry_run))
    elif args.command == "tasks":
        asyncio.run(_tasks_command(args))
    elif args.command == "cost":
        asyncio.run(_cost_command(args))
    elif args.command == "backfill-dimensions":
        asyncio.run(_backfill_dimensions(args))
    elif args.command == "storage-manifest":
        entries = write_source_manifest(args.source_root, args.manifest)
        print(f"object manifest created: {len(entries)} objects")
    elif args.command == "export-storage":
        entries = export_storage(
            store=make_object_store(get_settings()),
            archive_path=args.archive,
            manifest_path=args.manifest,
        )
        print(f"object storage exported and verified: {len(entries)} objects")
    elif args.command == "migrate-storage":
        result = migrate_storage(
            source_root=args.source_root,
            manifest_path=args.manifest,
            destination=make_object_store(get_settings()),
            dry_run=args.dry_run,
            verify_only=args.verify_only,
        )
        print(_storage_result("migration", result))
    elif args.command == "restore-storage":
        result = restore_storage(
            archive_path=args.archive,
            manifest_path=args.manifest,
            destination=make_object_store(get_settings()),
            dry_run=args.dry_run,
            verify_only=args.verify_only,
        )
        print(_storage_result("restore", result))
    else:
        count = storage_object_count(make_object_store(get_settings()))
        print(count)
        if args.require_empty and count:
            raise SystemExit("configured object store is not empty")


async def _backfill_dimensions(args: argparse.Namespace) -> None:
    """给历史证据补上 task/dataset/split，让 comparability_key 第一次真正成立。

    这些项目的证据是在 ``EXPERIMENT_EXTRACTION_MODE=off`` 时抽的：359 条 measurement
    里 task 与 split 全为空，于是 ``comparability_key`` 退化成按证据单元加盐的唯一值，
    306 个不同的键对应 359 行——没有任何两条结果落进同一个可比簇。SYNTH 的冲突判定
    与 Phase 4 的规则 2 因此从未被真实数据检验过。

    先抽全部、再统一决定写不写：Phase 2 的推广门槛是定位核验率 ≥80%，而那是**整批**
    的性质，逐篇写入就没有门槛可言了。抽取只跑一次，门槛不额外花钱。
    """
    import httpx
    from db.models.library import EvidenceUnit
    from db.models.paper import QuestionEvidenceLink, ResearchQuestion
    from paperforge_worker.config import WorkerSettings
    from paperforge_worker.context import JobContext
    from paperforge_worker.pipelines.evidence import (
        apply_work_dimensions,
        extract_work_dimensions,
    )
    from scholar_gateway import InMemoryHttpCache

    worker_settings = WorkerSettings()
    engine = make_engine(worker_settings.database_url, application_name="paperforge-admin")
    session_factory = make_session_factory(engine)
    totals = dict.fromkeys(("works", "cells", "verified", "rejected", "written"), 0)
    skipped: dict[str, int] = {}
    calls: list[Any] = []
    try:
        for raw_project in args.project:
            project_id = uuid.UUID(raw_project)
            async with session_factory() as session:
                stmt = select(EvidenceUnit.work_id).distinct()
                if args.linked_only:
                    stmt = (
                        stmt.join(
                            QuestionEvidenceLink,
                            QuestionEvidenceLink.evidence_unit_id == EvidenceUnit.id,
                        )
                        .join(
                            ResearchQuestion,
                            ResearchQuestion.id == QuestionEvidenceLink.research_question_id,
                        )
                        .where(
                            ResearchQuestion.project_id == project_id,
                            ResearchQuestion.kind == "sub",
                        )
                    )
                else:
                    stmt = stmt.where(EvidenceUnit.project_id == project_id)
                work_ids = list((await session.scalars(stmt.limit(args.limit))).all())
            if not work_ids:
                print(f"{raw_project}: no matching works")
                continue

            context = JobContext(
                project_id=project_id,
                job_id=None,
                settings=worker_settings,
                session_factory=session_factory,
                http_client=httpx.Client(),
                scholar_cache=InMemoryHttpCache(),
            )
            context.llm_calls = calls
            extractions = []
            for work_id in work_ids:
                extracted = await extract_work_dimensions(context, work_id=work_id)
                if extracted.skipped:
                    skipped[extracted.skipped] = skipped.get(extracted.skipped, 0) + 1
                    continue
                extractions.append(extracted)
                totals["works"] += 1
                totals["cells"] += extracted.cells
                totals["verified"] += extracted.locator_verified
                totals["rejected"] += extracted.rejected

            rate = totals["verified"] / totals["cells"] if totals["cells"] else 0.0
            print(
                f"{raw_project}: {len(extractions)} work(s), {totals['cells']} cell(s), "
                f"{totals['verified']} locator-verified ({rate:.1%}), "
                f"{totals['rejected']} rejected"
            )
            if not args.apply:
                continue
            if rate < args.min_verified_rate:
                print(
                    f"  refusing to apply: {rate:.1%} is below the "
                    f"{args.min_verified_rate:.0%} promotion gate"
                )
                continue
            for extracted in extractions:
                totals["written"] += await apply_work_dimensions(context, extracted)
    finally:
        await engine.dispose()

    for reason, count in sorted(skipped.items()):
        print(f"  skipped ({reason}): {count}")
    print(_llm_spend_line(calls))
    if args.apply:
        print(f"{totals['written']} measurement(s) written")
    else:
        print("dry run: nothing was written (pass --apply)")


def _llm_spend_line(calls: list[Any]) -> str:
    """用 P1-4 的记账把这次运维动作的开销说清楚，包括说不清的那部分。"""
    ok = [record for record in calls if not record.error_code]
    failed = len(calls) - len(ok)
    input_tokens = sum(record.input_tokens or 0 for record in ok)
    output_tokens = sum(record.output_tokens or 0 for record in ok)
    priced = [record.cost_estimate for record in ok if record.cost_estimate is not None]
    unpriced = len(ok) - len(priced)
    amount = (
        "unpriced (set LLM_MODEL_PRICES)"
        if not priced
        else f"{'≥ ' if unpriced else ''}${sum(priced):.4f}"
    )
    reasons = Counter(record.error_code for record in calls if record.error_code)
    failures = (
        f", {failed} failed call(s) ["
        + ", ".join(f"{reason}={count}" for reason, count in reasons.most_common())
        + "]"
        if failed
        else ""
    )
    return (
        f"\nspend: {len(ok)} call(s), {input_tokens} in / {output_tokens} out, "
        f"{amount}{failures}"
    )


async def _cost_command(args: argparse.Namespace) -> None:
    """成本记账的读出口。

    面板给的是一个项目的总数；做 shadow 评估要问的是另一类问题——
    「打开某个特性之后，那个角色单独花了多少」。角色是可靠的切分维度
    （每一行都有 role），stage 元数据只覆盖不到两成的调用。

    未定价的调用单独列出并给出原因，因为把它们混进 0 元里正是这条线路
    此前形同虚设的方式。
    """
    from db.models.paper import LlmCallLog

    settings = get_settings()
    engine = make_engine(settings.database_url, application_name="paperforge-admin")
    session_factory = make_session_factory(engine)
    filters = []
    if args.project:
        filters.append(LlmCallLog.project_id == uuid.UUID(args.project))
    if args.job:
        filters.append(LlmCallLog.job_id == uuid.UUID(args.job))
    if args.role:
        filters.append(LlmCallLog.role == args.role)
    if args.since:
        filters.append(LlmCallLog.occurred_at >= datetime.fromisoformat(args.since))
    try:
        async with session_factory() as session:
            rows = (
                await session.execute(
                    select(
                        LlmCallLog.role,
                        LlmCallLog.model,
                        func.count(LlmCallLog.id),
                        func.count(LlmCallLog.error_code),
                        func.coalesce(func.sum(LlmCallLog.input_tokens), 0),
                        func.coalesce(func.sum(LlmCallLog.output_tokens), 0),
                        func.count(LlmCallLog.cost_estimate),
                        func.coalesce(func.sum(LlmCallLog.cost_estimate), 0.0),
                        unpriced_call_count(),
                        func.count(1).filter(
                            LlmCallLog.error_code.is_(None),
                            LlmCallLog.input_tokens.is_(None),
                        ),
                    )
                    .where(*filters)
                    .group_by(LlmCallLog.role, LlmCallLog.model)
                    .order_by(func.count(LlmCallLog.id).desc())
                )
            ).all()
    finally:
        await engine.dispose()

    if not rows:
        print("no LLM calls match the filter")
        return

    header = (
        f"{'role':<30} {'model':<22} {'calls':>7} {'failed':>7} "
        f"{'in_tok':>11} {'out_tok':>11} {'cost':>12} {'unpriced':>9}"
    )
    print(header)
    print("-" * len(header))
    totals = dict.fromkeys(("calls", "failed", "in", "out", "unpriced", "no_usage"), 0)
    spend = 0.0
    unpriced_models: dict[str, int] = {}
    for role, model, calls, failed, in_tok, out_tok, priced, cost, unpriced, no_usage in rows:
        print(
            f"{role:<30} {model:<22} {calls:>7} {failed:>7} "
            f"{int(in_tok):>11} {int(out_tok):>11} "
            f"{('$' + format(float(cost), '.4f')) if priced else '—':>12} {unpriced:>9}"
        )
        totals["calls"] += calls
        totals["failed"] += failed
        totals["in"] += int(in_tok)
        totals["out"] += int(out_tok)
        totals["unpriced"] += unpriced
        totals["no_usage"] += no_usage
        spend += float(cost)
        if unpriced and no_usage < unpriced:
            unpriced_models[model] = unpriced_models.get(model, 0) + (unpriced - no_usage)

    print("-" * len(header))
    bound = "" if totals["unpriced"] == 0 else "≥ "
    print(
        f"{'TOTAL':<30} {'':<22} {totals['calls']:>7} {totals['failed']:>7} "
        f"{totals['in']:>11} {totals['out']:>11} "
        f"{bound + '$' + format(spend, '.4f'):>12} {totals['unpriced']:>9}"
    )
    if totals["unpriced"]:
        print(
            f"\n{totals['unpriced']} successful call(s) could not be priced, so the total "
            "above is a lower bound, not a bill."
        )
        if totals["no_usage"]:
            print(f"  {totals['no_usage']} of them returned no token usage at all.")
        for model, count in sorted(unpriced_models.items(), key=lambda item: -item[1]):
            print(f"  {count:>6} call(s) on an unpriced model: {model}")
        print(
            "  Add them to LLM_MODEL_PRICES, e.g. "
            '{"' + (next(iter(unpriced_models), "model-name")) + '": {"input": 0.0, "output": 0.0}}'
        )
    else:
        print("\nEvery successful call is priced.")


async def _tasks_command(args: argparse.Namespace) -> None:
    """任务本体的读写入口。

    在此之前，写 ``task_definition`` 的唯一途径是新增一条 Alembic 迁移——所谓
    「可配置的任务定义」其实并不成立（审计修正 C-5）。
    """
    settings = get_settings()
    engine = make_engine(settings.database_url, application_name="paperforge-admin")
    session_factory = make_session_factory(engine)
    try:
        async with session_factory() as session:
            if args.task_command == "list":
                specs = await list_task_definitions(session, domain=args.domain)
                for spec in specs:
                    vocabulary = spec.vocabulary
                    print(
                        f"{spec.domain:<16} {spec.slug:<34} "
                        f"metrics={len(spec.metrics):<3} datasets={len(spec.datasets):<3} "
                        f"models={len(vocabulary.model_families):<3} "
                        f"splits={len(vocabulary.split_strategies):<3} "
                        f"{'(empty vocabulary)' if vocabulary.empty else ''}"
                    )
                print(f"\n{len(specs)} task definition(s)")
                return

            if args.task_command == "coverage":
                await _task_coverage(session)
                return

            if args.task_command == "suggest":
                await _task_suggest(
                    session,
                    apply=args.apply,
                    include_bound=args.include_bound,
                )
                if args.apply:
                    await session.commit()
                return

            if args.task_command == "export":
                specs = await list_task_definitions(session)
                payload = [task_spec_to_payload(spec) for spec in specs]
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
                    encoding="utf-8",
                )
                print(f"exported {len(payload)} task definition(s) to {args.output}")
                return

            raw = json.loads(args.file.read_text(encoding="utf-8"))
            errors = validate_task_payloads(raw)
            if errors:
                for error in errors:
                    print(f"  {error}", file=sys.stderr)
                raise SystemExit(f"{len(errors)} validation error(s); nothing was written")
            for warning in task_ontology_warnings(raw):
                print(f"  warning: {warning}", file=sys.stderr)
            current = await list_task_definitions(session)
            before = {spec.slug: task_spec_to_payload(spec) for spec in current}
            incoming = {str(item["slug"]): item for item in raw}
            added = sorted(set(incoming) - set(before))
            changed = sorted(
                slug
                for slug in set(incoming) & set(before)
                if _normalized_task(incoming[slug]) != _normalized_task(before[slug])
            )
            # upsert 不删任何行：本体是共享的，一份局部文件不应该悄悄抹掉别人的任务。
            untouched = sorted(set(before) - set(incoming))
            for slug in added:
                print(f"  + {slug}")
            for slug in changed:
                print(f"  ~ {slug}")
            if untouched:
                print(f"  (left alone, not in file: {', '.join(untouched)})")
            if args.dry_run:
                print(f"dry run: {len(added)} to add, {len(changed)} to update; nothing written")
                return
            result = await upsert_task_definitions(session, raw)
            await session.commit()
            print(
                f"task ontology written: {result['created']} created, "
                f"{result['updated']} updated"
            )
    finally:
        await engine.dispose()


async def _task_suggest(session: Any, *, apply: bool, include_bound: bool) -> None:
    """为每个项目提出任务绑定；默认只打印，不写库。

    与 QDECOMP 的自动推断有两点不同，正是这两点让它能把覆盖率真正推到 100%：

    * **判据用题名 + 主题 + 研究问题**，而不是只用子问题。29 个项目里有 23 个从未跑过
      QDECOMP，一条研究问题都没有，自动推断对它们完全无从下手。
    * **匹配到领域就绑定该领域的全部任务**，而不是单个最佳任务。这些几乎都是综述，
      一篇综述会横跨该领域的多个任务，指标白名单取并集才对。

    匹配不到任何领域时提议 ``generic.scholarly``——那是一个**明确的判断**（"本项目
    没有适用的专用本体"），不是"没绑定"。两者在库里可区分，后者会继续吃回退。
    """
    from db import list_project_task_bindings, list_task_definitions, replace_project_task_profile
    from db.repositories.tasks import GENERIC_TASK_SLUG, infer_task_id

    tasks = await list_task_definitions(session)
    by_slug = {spec.slug: spec for spec in tasks}
    if GENERIC_TASK_SLUG not in by_slug:
        raise SystemExit(
            f"{GENERIC_TASK_SLUG} is missing; run `tasks upsert` with the seed file first"
        )
    by_domain: dict[str, list[str]] = {}
    for spec in tasks:
        by_domain.setdefault(spec.domain, []).append(spec.slug)
    # 通用任务不参与领域推断：它是兜底，不该和真实领域竞争线索。
    inferable = [spec for spec in tasks if spec.slug != GENERIC_TASK_SLUG]
    exclusions_by_domain: dict[str, list[str]] = {}
    for spec in tasks:
        exclusions_by_domain.setdefault(spec.domain, []).extend(spec.exclusion_cues)

    projects = list(
        (
            await session.scalars(
                select(PaperProject)
                .where(PaperProject.deleted_at.is_(None))
                .order_by(PaperProject.created_at)
            )
        ).all()
    )

    changed = 0
    generic = 0
    print(f"{'project':<38} {'topic':<34} {'->':<3} proposal")
    print("-" * 110)
    for project in projects:
        current = [row.task_id for row in await list_project_task_bindings(session, project.id)]
        if current and not include_bound:
            continue
        evidence = await _project_domain_evidence(session, project)
        slug = infer_task_id(evidence, inferable)
        domain = by_slug[slug].domain if slug else None
        # `infer_task_id` 只看纳入线索。排除线索在这里生效：例如"根际微生物次级代谢
        # 产物与植物抗病机制"会命中 bgc 的"次级代谢"，但它是生物学机制论文，不是
        # 计算 BGC 预测——绑到 bgc（AUROC/MCC、MIBiG）就是自信地绑错。
        excluded_by = (
            next(
                (
                    cue
                    for cue in exclusions_by_domain.get(domain, [])
                    if cue.casefold() in evidence.casefold()
                ),
                None,
            )
            if domain
            else None
        )
        if domain is None or excluded_by:
            proposal = [GENERIC_TASK_SLUG]
            generic += 1
        else:
            proposal = sorted(by_domain[domain])
        topic = str((project.scope_json or {}).get("topic") or project.title)[:32]
        label = (
            proposal[0]
            if len(proposal) == 1
            else f"{by_slug[proposal[0]].domain} ({len(proposal)} tasks)"
        )
        if excluded_by:
            label = f"{label}  [excluded from {domain}: {excluded_by!r}]"
        marker = " " if current == proposal else "*"
        print(f"{marker}{str(project.id)[:36]:<37} {topic:<34} ->  {label}")
        if current != proposal:
            changed += 1
            if apply:
                await replace_project_task_profile(
                    session,
                    project_id=project.id,
                    task_ids=proposal,
                )

    print("-" * 110)
    print(f"{len(projects)} project(s) considered; {changed} would change; {generic} -> generic")
    if not apply:
        print("dry run — nothing was written. Re-run with --apply to persist.")


async def _project_domain_evidence(session: Any, project: Any) -> str:
    """推断领域时读哪些文本。题名与主题优先，研究问题作为补充。"""
    from db.models.paper import ResearchQuestion

    questions = list(
        (
            await session.scalars(
                select(ResearchQuestion.text).where(ResearchQuestion.project_id == project.id)
            )
        ).all()
    )
    scope = project.scope_json or {}
    parts = [
        str(project.title or ""),
        str(scope.get("topic") or ""),
        str(scope.get("research_question") or ""),
        *(str(text) for text in questions),
    ]
    return " \n".join(part for part in parts if part)


async def _task_coverage(session: Any) -> None:
    """报告任务绑定覆盖率——这是能否安全切到 generic_only 的唯一判据。

    未绑定的项目当前继承**全部**领域的白名单。切换回退开关会把它们改成通用任务，
    也就是缩小匹配面；覆盖率越高，这个切换影响的项目越少。
    """
    from db.models.paper import ProjectTaskProfile

    total = int(
        await session.scalar(
            select(func.count()).select_from(PaperProject).where(PaperProject.deleted_at.is_(None))
        )
        or 0
    )
    bound_rows = list(
        (
            await session.execute(
                select(ProjectTaskProfile.project_id, func.count())
                .join(PaperProject, PaperProject.id == ProjectTaskProfile.project_id)
                .where(PaperProject.deleted_at.is_(None))
                .group_by(ProjectTaskProfile.project_id)
            )
        ).all()
    )
    bound = len(bound_rows)
    user_bound = int(
        await session.scalar(
            select(func.count())
            .select_from(PaperProject)
            .where(
                PaperProject.deleted_at.is_(None),
                PaperProject.scope_json[TASK_PROFILE_USER_BOUND_KEY].astext == "true",
            )
        )
        or 0
    )
    unbound = total - bound
    coverage = (bound / total) if total else 1.0

    print(f"projects (not deleted) : {total}")
    print(f"  bound                : {bound}  ({coverage:.0%})")
    print(f"    of which user-bound: {user_bound}")
    print(f"  unbound              : {unbound}")
    print()
    if unbound:
        print(
            f"{unbound} project(s) currently inherit EVERY domain's metric and dataset "
            "whitelist via TASK_PROFILE_FALLBACK=all_tasks."
        )
        print(
            "Bind them (UI: project scope page, or PUT /api/v1/projects/{id}/tasks) "
            "before switching the fallback to generic_only."
        )
    else:
        print("Every project is bound; TASK_PROFILE_FALLBACK no longer affects any project.")


def _normalized_task(payload: dict[str, Any]) -> str:
    """按稳定形状比较，好让 diff 不受键顺序和缺省字段影响。"""
    return json.dumps(
        {
            "domain": payload.get("domain"),
            "labels": payload.get("labels") or {},
            "metrics": payload.get("metrics") or [],
            "datasets": payload.get("datasets") or [],
            "dimensions": payload.get("dimensions") or [],
            "inclusion_cues": payload.get("inclusion_cues") or [],
            "exclusion_cues": payload.get("exclusion_cues") or [],
            "vocabulary": payload.get("vocabulary") or {},
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _storage_result(operation: str, result: dict[str, int]) -> str:
    return (
        f"object storage {operation}: copied={result['copied']}; "
        f"already valid={result['skipped']}; verified={result['verified']}"
    )


async def _bootstrap(args: argparse.Namespace, password_hash: str) -> None:
    settings = get_settings()
    engine = make_engine(settings.database_url)
    factory = make_session_factory(engine)
    try:
        async with factory() as session:
            placeholder = await session.get(AppUser, LEGACY_USER_ID)
            user = await get_user_by_email(session, args.email)
            if user is None and placeholder is not None and placeholder.status == "disabled":
                user = placeholder
                user.email = args.email.strip().lower()
                user.display_name = args.display_name.strip() or None
                user.password_hash = password_hash
                user.status = "active"
                from datetime import UTC, datetime

                user.email_verified_at = datetime.now(UTC)
            elif user is None:
                user = await create_user(
                    session,
                    email=args.email,
                    password_hash=password_hash,
                    display_name=args.display_name,
                    verified=True,
                )
            else:
                from datetime import UTC, datetime

                user.password_hash = password_hash
                user.display_name = args.display_name.strip() or user.display_name
                user.status = "active"
                user.email_verified_at = user.email_verified_at or datetime.now(UTC)

            claimed = 0
            if args.claim_legacy and user.id != LEGACY_USER_ID:
                result = await session.execute(
                    update(PaperProject)
                    .where(PaperProject.owner_id == LEGACY_USER_ID)
                    .values(owner_id=user.id)
                )
                claimed += int(result.rowcount or 0)
            if args.project_map:
                claimed += await _apply_project_map(session, args.project_map)
            await session.commit()
            print(f"administrator ready: {user.email}; projects assigned: {claimed}")
        if args.migrate_objects:
            await _migrate_objects(dry_run=False)
    finally:
        await engine.dispose()


async def _apply_project_map(session: Any, path: Path) -> int:
    assigned = 0
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            project_id = uuid.UUID((row.get("project_id") or "").strip())
            email = (row.get("email") or "").strip()
            user = await get_user_by_email(session, email)
            if user is None or user.status != "active":
                raise ValueError(f"active user not found for project mapping: {email}")
            result = await session.execute(
                update(PaperProject).where(PaperProject.id == project_id).values(owner_id=user.id)
            )
            if not result.rowcount:
                raise ValueError(f"project not found for mapping: {project_id}")
            assigned += 1
    return assigned


async def _migrate_objects(*, dry_run: bool) -> None:
    settings = get_settings()
    engine = make_engine(settings.database_url)
    factory = make_session_factory(engine)
    store = make_object_store(settings)
    copied = 0
    skipped = 0
    try:
        async with factory() as session:
            assets = (
                await session.execute(
                    select(UserAsset, PaperProject).join(
                        PaperProject, PaperProject.id == UserAsset.project_id
                    )
                )
            ).all()
            for asset, project in assets:
                changed, key = _private_key(asset.object_key, project)
                if changed:
                    copied += await _copy_verified(store, asset.object_key, key, dry_run=dry_run)
                    if not dry_run:
                        asset.object_key = key
                else:
                    skipped += 1

            exports = (
                await session.execute(
                    select(ExportArtifact, PaperProject).join(
                        PaperProject, PaperProject.id == ExportArtifact.project_id
                    )
                )
            ).all()
            for artifact, project in exports:
                for field in ("object_key", "compile_log_key"):
                    old_key = getattr(artifact, field)
                    changed, key = _private_key(old_key, project)
                    if changed:
                        copied += await _copy_verified(store, old_key, key, dry_run=dry_run)
                        if not dry_run:
                            setattr(artifact, field, key)
                    else:
                        skipped += 1

            visuals = (
                await session.execute(
                    select(VisualAsset, PaperProject).join(
                        PaperProject, PaperProject.id == VisualAsset.project_id
                    )
                )
            ).all()
            for visual, project in visuals:
                renditions = dict(visual.renditions_json or {})
                changed_payload = False
                for item in renditions.values():
                    if not isinstance(item, dict):
                        continue
                    changed, key = _private_key(item.get("object_key"), project)
                    if changed:
                        copied += await _copy_verified(
                            store, item["object_key"], key, dry_run=dry_run
                        )
                        item["object_key"] = key
                        changed_payload = True
                if changed_payload and not dry_run:
                    visual.renditions_json = renditions

            documents = list((await session.scalars(select(DocumentFile))).all())
            for document in documents:
                old_key = document.object_key
                if old_key.startswith("works/"):
                    key = f"shared/oa/{old_key}"
                    copied += await _copy_verified(store, old_key, key, dry_run=dry_run)
                    if not dry_run:
                        document.object_key = key
                else:
                    skipped += 1
            if not dry_run:
                await session.commit()
        verb = "would copy" if dry_run else "copied and switched"
        print(f"object migration: {verb} {copied}; already migrated/empty: {skipped}")
        print("legacy objects were retained; remove them only after the seven-day safety window")
    finally:
        await engine.dispose()


def _private_key(old_key: str | None, project: PaperProject) -> tuple[bool, str | None]:
    if not old_key or old_key.startswith("users/"):
        return False, old_key
    legacy_prefix = f"projects/{project.id}/"
    if not old_key.startswith(legacy_prefix):
        return False, old_key
    return True, f"users/{project.owner_id}/{old_key}"


async def _copy_verified(store: Any, old_key: str, new_key: str, *, dry_run: bool) -> int:
    if dry_run:
        store.get(old_key)  # A dry run still verifies that every source exists.
        return 1
    data = store.get(old_key)
    digest = hashlib.sha256(data).digest()
    store.put(new_key, data)
    if hashlib.sha256(store.get(new_key)).digest() != digest:
        raise RuntimeError(f"object verification failed: {new_key}")
    return 1


async def _project_object_keys(session: Any, project_id: uuid.UUID) -> list[str]:
    """一个项目独占的对象键。

    必须逐条从库里枚举而不是按前缀删：`ObjectStore` 协议只有单键 `delete(key)`，
    没有 list/前缀删除。反过来说这也更安全——`shared/oa/works/...` 下的 OA 全文
    是**跨项目共享**的，按前缀扫会把别的项目还在用的全文一起删掉。
    """
    keys: list[str] = []
    assets = await session.scalars(select(UserAsset).where(UserAsset.project_id == project_id))
    keys.extend(asset.object_key for asset in assets)

    exports = await session.scalars(
        select(ExportArtifact).where(ExportArtifact.project_id == project_id)
    )
    for artifact in exports:
        keys.extend([artifact.object_key, artifact.compile_log_key])

    visuals = await session.scalars(select(VisualAsset).where(VisualAsset.project_id == project_id))
    for visual in visuals:
        for item in (visual.renditions_json or {}).values():
            if isinstance(item, dict):
                keys.append(item.get("object_key"))

    documents = await session.scalars(
        select(DocumentFile).where(DocumentFile.project_id == project_id)
    )
    keys.extend(document.object_key for document in documents)
    uploads = await session.scalars(
        select(LiteraturePdfUpload).where(LiteraturePdfUpload.project_id == project_id)
    )
    keys.extend(upload.object_key for upload in uploads)

    # 去重并保序；`shared/` 一律不碰。
    return list(dict.fromkeys(key for key in keys if key and not str(key).startswith("shared/")))


async def _purge_projects(*, days: int, dry_run: bool) -> None:
    """真删保留期已过的软删除项目，并回收它们独占的对象。

    顺序是**先对象后行**：反过来一旦行删成功、对象删失败，object_key 就再也查不出来，
    存储里会留下永远没人认领的垃圾。
    """
    settings = get_settings()
    engine = make_engine(settings.database_url)
    factory = make_session_factory(engine)
    store = make_object_store(settings)
    cutoff = datetime.now(UTC) - timedelta(days=days)
    purged = deleted_objects = missing_objects = 0
    try:
        async with factory() as session:
            projects = await list_purgeable_projects(session, before=cutoff)
            for project in projects:
                keys = await _project_object_keys(session, project.id)
                for key in keys:
                    if dry_run:
                        deleted_objects += 1
                        continue
                    try:
                        store.delete(key)
                        deleted_objects += 1
                    except Exception:  # noqa: BLE001 - 对象早已不在不该挡住回收
                        missing_objects += 1
                if not dry_run:
                    await purge_project(session, project)
                purged += 1
            if not dry_run:
                await session.commit()
    finally:
        await engine.dispose()
    prefix = "[dry-run] " if dry_run else ""
    print(
        f"{prefix}purged {purged} project(s) deleted before {cutoff.isoformat()}: "
        f"{deleted_objects} object(s) removed, {missing_objects} already gone"
    )


if __name__ == "__main__":
    main()
