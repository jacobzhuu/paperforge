"""Coordinator-owned web research. Web text never enters scholarly writing context."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from db.models.paper import GenerationJob, PaperProject, ResearchQuestion
from db.models.web_research import (
    McpDailyBudget,
    McpToolInvocation,
    WebResearchRun,
    WebResearchSource,
)
from mcp_runtime import exa
from scholar_gateway import VerificationRequest
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from paperforge_worker.context import JobStopped
from paperforge_worker.execution import ExecutionLost
from paperforge_worker.pipelines.importing import import_references

RUN_KEY = "web_research_run_id"
TERMINAL = {"completed", "partial", "failed", "disabled", "budget_exhausted"}


def _propagate_control(error):
    if isinstance(error, (JobStopped, ExecutionLost, asyncio.CancelledError)):
        raise error
    if isinstance(error, BaseExceptionGroup):
        for child in error.exceptions:
            _propagate_control(child)


async def _enabled(context):
    await context.raise_if_stopped()
    async with context.session() as session:
        project = await session.get(PaperProject, context.project_id)
        if project is None or project.deleted_at is not None:
            raise JobStopped("cancel", reason="project_unavailable")
        if not context.settings.mcp_web_enabled or not project.web_research_enabled:
            raise exa.ToolFailure("disabled")
        return project


async def _run(context, project):
    async with context.session() as session:
        run_id = context.checkpoint.get(RUN_KEY)
        stmt = select(WebResearchRun).where(WebResearchRun.project_id == project.id)
        if run_id:
            stmt = stmt.where(WebResearchRun.id == uuid.UUID(run_id))
        else:
            stmt = stmt.where(WebResearchRun.job_id == context.job_id)
        run = await session.scalar(stmt) if run_id or context.job_id else None
        if run_id and run is None:
            raise exa.ToolFailure("invalid_resume_run")
        if run is None:
            questions = list(
                await session.scalars(
                    select(ResearchQuestion)
                    .where(ResearchQuestion.project_id == project.id)
                    .order_by(ResearchQuestion.order_index, ResearchQuestion.id)
                )
            )
            topic = str((project.scope_json or {}).get("topic") or project.title)[:600]
            queries = [f"{topic} official documentation dataset project"]
            for question in questions:
                query = str(question.search_query or question.text).strip()[:600]
                if query and query not in queries:
                    queries.append(query)
            run = WebResearchRun(
                project_id=project.id,
                job_id=context.job_id,
                plan_json={
                    "version": exa.VERSION,
                    "queries": queries[:3],
                    "call_limit": context.settings.mcp_run_call_limit,
                    "timeout": context.settings.mcp_run_timeout,
                    "call_timeout": context.settings.mcp_call_timeout,
                    "pricing": "unknown",
                },
            )
            session.add(run)
            await session.flush()
        if context.job_id:
            run.job_id = context.job_id
            job = await session.scalar(
                select(GenerationJob).where(GenerationJob.id == context.job_id).with_for_update()
            )
            job.checkpoint_json = {**(job.checkpoint_json or {}), RUN_KEY: str(run.id)}
        context.checkpoint[RUN_KEY] = str(run.id)
        return run


async def reserve(context, run_id, tool, args, schema_hash, trace):
    """One short transaction serializes global/user quotas across all deployments."""
    project = await _enabled(context)
    request_hash = exa.digest([tool, args])
    async with context.session() as session:
        run = await session.scalar(
            select(WebResearchRun)
            .where(WebResearchRun.id == run_id, WebResearchRun.project_id == project.id)
            .with_for_update()
        )
        if run is None:
            raise exa.ToolFailure("invalid_run")
        previous = list(
            await session.scalars(
                select(McpToolInvocation)
                .where(
                    McpToolInvocation.run_id == run_id,
                    McpToolInvocation.request_hash == request_hash,
                )
                .order_by(McpToolInvocation.attempt)
            )
        )
        for row in previous:
            if row.status == "completed":
                return None, row.result_json
        if len(previous) >= 2:
            raise exa.ToolFailure("attempts_exhausted")
        now = datetime.now(UTC)
        if now >= run.created_at + timedelta(seconds=run.plan_json["timeout"]):
            raise exa.ToolFailure("deadline_exhausted")
        if run.calls >= min(run.plan_json["call_limit"], context.settings.mcp_run_call_limit):
            raise exa.ToolFailure("run_budget_exhausted")
        date = now.date().isoformat()
        for key, limit in [
            (f"{date}:global", context.settings.mcp_global_daily_calls),
            (f"{date}:user:{project.owner_id}", context.settings.mcp_user_daily_calls),
        ]:
            await session.execute(
                insert(McpDailyBudget)
                .values(key=key, calls=0)
                .on_conflict_do_nothing(index_elements=["key"])
            )
            counter = await session.scalar(
                select(McpDailyBudget).where(McpDailyBudget.key == key).with_for_update()
            )
            if counter.calls >= limit:
                raise exa.ToolFailure("daily_budget_exhausted")
            counter.calls += 1
        for row in previous:
            if row.status == "reserved":
                row.status = "unknown"
        run.calls += 1
        invocation = McpToolInvocation(
            run_id=run_id,
            project_id=project.id,
            owner_id=project.owner_id,
            tool=tool,
            schema_hash=schema_hash,
            request_hash=request_hash,
            attempt=len(previous) + 1,
            trace_json=trace,
        )
        session.add(invocation)
        await session.flush()
        return invocation.id, None


async def _call(context, run, client, tool, args):
    async with context.span("mcp_tool", tool, server="exa", run_id=str(run.id)) as trace:
        for attempt in range(2):
            invocation_id, cached = await reserve(
                context, run.id, tool, args, exa.digest(client.schemas[tool]), trace
            )
            if invocation_id is None:
                return cached
            await context.emit(
                "mcp.call_reserved",
                {
                    "invocation_id": str(invocation_id),
                    "tool": tool,
                    **trace,
                },
            )
            try:
                result = await client.call(tool, args)
            except (JobStopped, ExecutionLost, asyncio.CancelledError):
                raise
            except Exception as error:
                code = error.code if isinstance(error, exa.ToolFailure) else "protocol_error"
                async with context.session() as session:
                    row = await session.get(McpToolInvocation, invocation_id)
                    row.status = "unknown" if code == "tool_timeout" else "failed"
                    row.error, row.finished_at = code, datetime.now(UTC)
                await context.emit(
                    "mcp.call_failed",
                    {
                        "invocation_id": str(invocation_id),
                        "error": code,
                        **trace,
                    },
                )
                if isinstance(error, exa.ToolFailure) and error.retryable and attempt == 0:
                    await asyncio.sleep(1)
                    continue
                raise exa.ToolFailure(code) from None
            async with context.session() as session:
                row = await session.get(McpToolInvocation, invocation_id)
                row.status, row.result_json = "completed", result
                row.finished_at = datetime.now(UTC)
            await context.emit(
                "mcp.call_completed",
                {
                    "invocation_id": str(invocation_id),
                    "tool": tool,
                    **trace,
                },
            )
            return result
    raise exa.ToolFailure("attempts_exhausted")


async def _collect(context, run, client):
    async with context.session() as session:
        saved = await session.get(WebResearchRun, run.id)
        hashes = {tool: exa.digest(schema) for tool, schema in client.schemas.items()}
        if saved.plan_json.get("schemas", hashes) != hashes:
            raise exa.ToolFailure("schema_changed_on_resume")
        saved.plan_json = {**saved.plan_json, "schemas": hashes}
        saved.status = "running"
    errors = []
    for query in run.plan_json["queries"]:
        try:
            result = await _call(
                context,
                run,
                client,
                exa.SEARCH,
                exa.search_arguments(client.schemas[exa.SEARCH], query),
            )
            rows = exa.search_results(result)
        except exa.ToolFailure as error:
            if error.code in {"disabled", "daily_budget_exhausted", "deadline_exhausted"}:
                raise
            errors.append(error.code)
            continue
        async with context.session() as session:
            existing_urls = set(
                await session.scalars(
                    select(WebResearchSource.url_hash).where(WebResearchSource.run_id == run.id)
                )
            )
            for row in rows:
                url_hash = exa.digest(row["url"])
                if url_hash in existing_urls:
                    continue
                await session.execute(
                    insert(WebResearchSource)
                    .values(
                        id=uuid.uuid4(),
                        run_id=run.id,
                        project_id=context.project_id,
                        url_hash=url_hash,
                        order_no=len(existing_urls),
                        **row,
                        body="",
                        status="discovered",
                    )
                    .on_conflict_do_nothing(index_elements=["run_id", "url_hash"])
                )
                existing_urls.add(url_hash)

    async with context.session() as session:
        sources = list(
            await session.scalars(
                select(WebResearchSource)
                .where(WebResearchSource.run_id == run.id)
                .order_by(WebResearchSource.order_no, WebResearchSource.id)
            )
        )
    for source in sources[:5]:
        if source.status != "discovered":
            continue
        await _enabled(context)
        try:
            await exa.checked_url(source.url)
            payload = await _call(
                context,
                run,
                client,
                exa.FETCH,
                {
                    "urls": [source.url],
                    "maxCharacters": 6000,
                },
            )
            body = exa.fetched_text(payload, source.url)
            status = "read"
        except exa.ToolFailure as error:
            if error.code in {"disabled", "daily_budget_exhausted", "deadline_exhausted"}:
                raise
            body, status = "", "read_failed"
            errors.append(error.code)
        async with context.session() as session:
            row = await session.get(WebResearchSource, source.id)
            row.body, row.status = body, status
            row.content_hash = exa.digest(body) if body else None
            row.fetched_at = datetime.now(UTC) if body else None
        source.body = body
    return errors


async def _verify_papers(context, run):
    async with context.session() as session:
        sources = list(
            await session.scalars(
                select(WebResearchSource)
                .where(WebResearchSource.run_id == run.id)
                .order_by(WebResearchSource.order_no, WebResearchSource.id)
            )
        )
    seen = set()
    for source in sources:
        identifiers = exa.paper_identifiers(source.url, source.body + "\n" + source.snippet)
        selected = []
        for item in identifiers:
            key = exa.digest(item)
            if key not in seen and len(seen) < 10:
                seen.add(key)
                selected.append(item)
        if source.verification_json is not None:
            continue
        results = []
        for item in selected:
            await _enabled(context)
            outcome = await import_references(
                context,
                [VerificationRequest(**item, source_label="mcp_web_verified")],
                added_via="mcp_web_verified",
                status="candidate",
            )
            results.append({**item, "verified": outcome.verified, "entries": outcome.entries})
        async with context.session() as session:
            row = await session.get(WebResearchSource, source.id)
            row.verification_json = {"identifiers": results}


async def run_web_research(context) -> dict:
    try:
        project = await _enabled(context)
    except exa.ToolFailure:
        return {"status": "disabled"}
    run = await _run(context, project)
    if run.status in TERMINAL:
        return {"status": run.status, "run_id": str(run.id)}
    status, error = "completed", None
    try:
        if run.plan_json.get("version") != exa.VERSION:
            raise exa.ToolFailure("adapter_changed_on_resume")
        remaining = (
            run.created_at + timedelta(seconds=run.plan_json["timeout"]) - datetime.now(UTC)
        ).total_seconds()
        if remaining <= 0:
            raise exa.ToolFailure("deadline_exhausted")
        async with asyncio.timeout(remaining):
            async with exa.connect(
                context.settings.exa_api_key, run.plan_json["call_timeout"]
            ) as client:
                errors = await _collect(context, run, client)
        # Verification uses the existing scholarly adapters, not the MCP budget.
        # It is separately bounded to avoid a slow identifier holding a worker forever.
        async with asyncio.timeout(120):
            await _verify_papers(context, run)
        if errors:
            status, error = "partial", errors[0]
    except (JobStopped, ExecutionLost, asyncio.CancelledError):
        # Reserved calls remain ambiguous; the next fenced executor reconciles them.
        raise
    except BaseException as failure:
        _propagate_control(failure)
        while isinstance(failure, BaseExceptionGroup) and len(failure.exceptions) == 1:
            failure = failure.exceptions[0]
        if not isinstance(failure, Exception):
            raise
        error = (
            failure.code
            if isinstance(failure, exa.ToolFailure)
            else (
                "deadline_exhausted" if isinstance(failure, TimeoutError) else "connection_failed"
            )
        )
        status = "disabled" if error == "disabled" else "failed"
        if "budget" in error:
            status = "budget_exhausted"
        context.warn("web_research", error, {})
    async with context.session() as session:
        saved = await session.get(WebResearchRun, run.id)
        sources = list(
            await session.scalars(
                select(WebResearchSource.id).where(WebResearchSource.run_id == run.id)
            )
        )
        if sources and status == "failed":
            status = "partial"
        saved.status, saved.error, saved.finished_at = status, error, datetime.now(UTC)
        # Calls cancelled by the overall deadline are not eligible for free replay.
        pending = await session.scalars(
            select(McpToolInvocation).where(
                McpToolInvocation.run_id == run.id, McpToolInvocation.status == "reserved"
            )
        )
        for row in pending:
            row.status, row.error = "unknown", error or "interrupted"
        payload = {
            "status": status,
            "run_id": str(run.id),
            "sources": len(sources),
            "calls": saved.calls,
            "error": error,
            "pricing": "unknown",
        }
    await context.emit("web_research.finished", payload)
    return payload
