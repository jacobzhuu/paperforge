import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy

import pytest
from db import create_job, get_job
from db.models.library import EvidenceUnit, LibraryEntry
from db.models.paper import PaperProject
from db.models.web_research import McpDailyBudget, McpToolInvocation, WebResearchRun
from mcp_runtime import exa
from paperforge_worker.context import JobStopped
from paperforge_worker.execution import ExecutionLost
from paperforge_worker.pipelines import web_research as web
from sqlalchemy import func, select
from test_repair_scheduling import _context, _seed


async def prepare(factory):
    project_id, _ = await _seed(factory)
    context = _context(project_id, factory)
    context.settings.mcp_web_enabled = True
    async with factory() as session:
        project = await session.get(PaperProject, project_id)
        project.web_research_enabled = True
        job = await create_job(session, project_id=project_id, kind="web_research")
        context.job_id = job.id
        await session.commit()
    return context


class FakeClient:
    schemas = {exa.SEARCH: {}, exa.FETCH: {}}

    def __init__(self):
        self.calls = []
        self.stop_once = False

    async def call(self, tool, args):
        self.calls.append((tool, args))
        if tool == exa.FETCH:
            if self.stop_once:
                self.stop_once = False
                raise JobStopped("pause")
            return {
                "text": f"URL: {args['urls'][0]}\nContent: Official dataset docs",
                "structured": None,
            }
        return {
            "text": "Title: Dataset docs\nURL: https://example.org/data\nHighlights:\nData guide",
            "structured": None,
        }


def install(monkeypatch, fake):
    @asynccontextmanager
    async def connect(*args):
        yield fake

    async def checked(url):
        return exa.public_url(url)

    monkeypatch.setattr(exa, "connect", connect)
    monkeypatch.setattr(exa, "checked_url", checked)


async def test_web_never_becomes_scholarly_evidence_and_completed_run_is_reused(
    session_factory,
    monkeypatch,
):
    context = await prepare(session_factory)
    fake = FakeClient()
    install(monkeypatch, fake)
    try:
        result = await web.run_web_research(context)
        assert result["status"] == "completed" and result["sources"] == 1
        assert len(fake.calls) == 2
        await web.run_web_research(context)
        assert len(fake.calls) == 2
        async with session_factory() as session:
            assert await session.scalar(select(func.count()).select_from(EvidenceUnit)) == 0
            entries = list(
                await session.scalars(
                    select(LibraryEntry).where(LibraryEntry.project_id == context.project_id)
                )
            )
            assert len(entries) == 1 and entries[0].added_via == "search"
            calls = list(
                await session.scalars(
                    select(McpToolInvocation).where(
                        McpToolInvocation.project_id == context.project_id
                    )
                )
            )
            assert all(row.status == "completed" for row in calls)
            assert all(row.trace_json["parent_span_id"] is None for row in calls)
    finally:
        context.http_client.close()


async def test_pause_resume_preserves_search_and_budget(session_factory, monkeypatch):
    context = await prepare(session_factory)
    fake = FakeClient()
    fake.stop_once = True
    install(monkeypatch, fake)
    try:
        with pytest.raises(JobStopped):
            await web.run_web_research(context)
        async with session_factory() as session:
            old = await get_job(session, context.job_id)
            checkpoint = deepcopy(old.checkpoint_json)
            job = await create_job(
                session, project_id=context.project_id, kind="web_research", checkpoint=checkpoint
            )
            await session.commit()
            context.job_id, context.checkpoint = job.id, checkpoint
        result = await web.run_web_research(context)
        assert result["status"] == "completed" and result["calls"] == 3
        assert [tool for tool, _ in fake.calls].count(exa.SEARCH) == 1
        async with session_factory() as session:
            statuses = list(
                await session.scalars(
                    select(McpToolInvocation.status).where(
                        McpToolInvocation.project_id == context.project_id
                    )
                )
            )
            assert sorted(statuses) == ["completed", "completed", "unknown"]
    finally:
        context.http_client.close()


async def test_concurrent_reservations_use_database_global_limit(session_factory):
    contexts = [await prepare(session_factory), await prepare(session_factory)]
    try:
        # Tests share the database; clear only the dedicated test ledger counters.
        async with session_factory() as session:
            from sqlalchemy import delete

            await session.execute(delete(McpDailyBudget))
            await session.commit()
        runs = []
        for context in contexts:
            context.settings.mcp_global_daily_calls = 1
            project = await web._enabled(context)
            runs.append(await web._run(context, project))
        result = await asyncio.gather(
            *[
                web.reserve(context, run.id, exa.SEARCH, {"query": "x"}, "a" * 64, {})
                for context, run in zip(contexts, runs, strict=True)
            ],
            return_exceptions=True,
        )
        assert sum(isinstance(item, exa.ToolFailure) for item in result) == 1
        assert sum(isinstance(item, tuple) for item in result) == 1
    finally:
        for context in contexts:
            context.http_client.close()


async def test_disabled_never_connects_and_mid_run_disable_stops_fetch(
    session_factory,
    monkeypatch,
):
    context = await prepare(session_factory)
    fake = FakeClient()
    install(monkeypatch, fake)
    try:
        context.settings.mcp_web_enabled = False
        assert (await web.run_web_research(context))["status"] == "disabled"
        assert fake.calls == []
        context.settings.mcp_web_enabled = True
        original = fake.call

        async def disable(tool, args):
            result = await original(tool, args)
            async with session_factory() as session:
                project = await session.get(PaperProject, context.project_id)
                project.web_research_enabled = False
                await session.commit()
            return result

        fake.call = disable
        assert (await web.run_web_research(context))["status"] == "disabled"
        assert len(fake.calls) == 1
    finally:
        context.http_client.close()


@pytest.mark.parametrize("error", [JobStopped("pause"), ExecutionLost("lease")])
async def test_protocol_exception_group_cannot_swallow_control(
    session_factory,
    monkeypatch,
    error,
):
    context = await prepare(session_factory)

    @asynccontextmanager
    async def connect(*args):
        raise BaseExceptionGroup("SDK task group", [error])
        yield

    monkeypatch.setattr(exa, "connect", connect)
    try:
        with pytest.raises(type(error)):
            await web.run_web_research(context)
    finally:
        context.http_client.close()


async def test_schema_drift_is_rejected_on_resume(session_factory, monkeypatch):
    context = await prepare(session_factory)
    run = await web._run(context, await web._enabled(context))
    async with session_factory() as session:
        row = await session.get(WebResearchRun, run.id)
        row.plan_json = {**row.plan_json, "schemas": {"changed": "schema"}}
        await session.commit()
    fake = FakeClient()
    install(monkeypatch, fake)
    try:
        result = await web.run_web_research(context)
        assert result["error"] == "schema_changed_on_resume"
        assert not fake.calls
    finally:
        context.http_client.close()


async def test_run_ownership_and_user_quota(session_factory):
    a, b = await prepare(session_factory), await prepare(session_factory)
    try:
        run_a = await web._run(a, await web._enabled(a))
        run_b = await web._run(b, await web._enabled(b))
        with pytest.raises(exa.ToolFailure, match="invalid_run"):
            await web.reserve(b, run_a.id, exa.SEARCH, {}, "0" * 64, {})
        a.settings.mcp_user_daily_calls = 1
        await web.reserve(a, run_a.id, exa.SEARCH, {"query": "first"}, "0" * 64, {})
        with pytest.raises(exa.ToolFailure, match="daily_budget_exhausted"):
            await web.reserve(a, run_a.id, exa.SEARCH, {"query": "second"}, "0" * 64, {})
        # Another user still has their own allocation.
        await web.reserve(b, run_b.id, exa.SEARCH, {"query": "first"}, "0" * 64, {})
    finally:
        a.http_client.close()
        b.http_client.close()


async def test_verified_import_does_not_override_user_selection(session_factory, monkeypatch):
    from types import SimpleNamespace

    from paperforge_worker.pipelines import importing
    from scholar_gateway import VerificationRequest
    from test_repair_scheduling import _Candidate

    class VerifiedCandidate(_Candidate):
        provider_name = "crossref"
        provider_record_id = "verified"

    context = await prepare(session_factory)
    try:
        async with session_factory() as session:
            entry = await session.scalar(
                select(LibraryEntry).where(LibraryEntry.project_id == context.project_id)
            )
            entry.status, entry.user_pinned = "excluded", True
            entry.rank_reason_json = {"user": "preserve this decision"}
            await session.commit()
        monkeypatch.setattr(
            importing,
            "verify_reference",
            lambda *args, **kwargs: SimpleNamespace(
                verified=True,
                candidate=VerifiedCandidate(),
                matched_by="doi",
                confidence=1,
            ),
        )
        result = await importing.import_references(
            context,
            [VerificationRequest(doi="10.1000/example")],
            added_via="mcp_web_verified",
            status="candidate",
        )
        assert result.verified == 1
        async with session_factory() as session:
            entry = await session.scalar(
                select(LibraryEntry).where(LibraryEntry.project_id == context.project_id)
            )
            assert entry.status == "excluded" and entry.user_pinned
            assert entry.rank_reason_json == {"user": "preserve this decision"}
    finally:
        context.http_client.close()
