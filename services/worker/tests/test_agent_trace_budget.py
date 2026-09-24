import asyncio
from copy import deepcopy

import pytest
from db import create_job, get_job, list_job_events
from llm_runtime import LLMConfig, LLMResponse
from paperforge_worker.context import JobStopped
from test_repair_scheduling import _context, _seed


class Provider:
    def generate(self, request):
        return LLMResponse(
            text="ok", model="test", provider="test", usage={"input_tokens": 2, "output_tokens": 1}
        )


async def test_budget_is_reserved_before_calls_and_inherited_on_resume(
    session_factory, monkeypatch
):
    project_id, _ = await _seed(session_factory)
    context = _context(project_id, session_factory)
    async with session_factory() as session:
        job = await create_job(session, project_id=project_id, kind="write")
        context.job_id = job.id
        await session.commit()
    context.settings.agent_max_calls = 1
    runner = context.llm_runner(config=LLMConfig(provider="openai", enabled=True))
    runner._provider = Provider()
    try:
        async with context.span("coordinator", "test") as parent:
            results = await asyncio.gather(
                *[
                    runner.agenerate(
                        "writer",
                        system_prompt="test",
                        user_prompt="test",
                        max_output_tokens=10,
                        metadata={"scheduler_job_id": "spoof", "scheduler_priority": "shadow"},
                    )
                    for _ in range(2)
                ]
            )
        assert sum(result is not None for result in results) == 1
        assert context.llm_calls[0].metadata["parent_span_id"] == parent["span_id"]
        assert context.llm_calls[0].metadata["scheduler_job_id"] == str(context.job_id)
        assert context.llm_calls[0].metadata["scheduler_priority"] == "foreground"
        with pytest.raises(JobStopped):
            await context.raise_if_stopped()
        async with session_factory() as session:
            saved = await get_job(session, job.id)
            checkpoint = deepcopy(saved.checkpoint_json)
            events = await list_job_events(session, job.id)
        assert checkpoint["agent_budget"]["calls"] == 1
        assert any(e.event_type == "agent.call_reserved" for e in events)
        context.checkpoint = checkpoint
        context._budget_exhausted = False
        assert await runner.agenerate("writer", system_prompt="x", user_prompt="y") is None
        assert context.checkpoint["agent_budget"]["calls"] == 1
        from evals.agent_writing import trace

        context.settings.database_url = session_factory.kw["bind"].url.render_as_string(
            hide_password=False
        )
        monkeypatch.setattr(trace, "WorkerSettings", lambda: context.settings)
        exported = await trace.export_trace(job.id)
        assert len(exported["calls"]) == 1
        assert exported["unreconciled_calls"] == []
        assert exported["unfinished_spans"] == []
    finally:
        context.http_client.close()


async def test_parallel_spans_keep_separate_parents(session_factory):
    project_id, _ = await _seed(session_factory)
    context = _context(project_id, session_factory)
    runner = context.llm_runner(config=LLMConfig(provider="openai", enabled=True))
    runner._provider = Provider()

    async def one(key):
        async with context.span("subagent", key, node_id=key) as span:
            await runner.agenerate("writer", system_prompt="x", user_prompt=key)
            return span

    try:
        spans = await asyncio.gather(one("a"), one("b"))
        assert {r.metadata["parent_span_id"] for r in context.llm_calls} == {
            s["span_id"] for s in spans
        }
        assert {r.metadata["node_id"] for r in context.llm_calls} == {"a", "b"}
    finally:
        context.http_client.close()


async def test_budget_pause_has_a_durable_reason(session_factory):
    from paperforge_worker.context import job_context

    project_id, _ = await _seed(session_factory)
    template = _context(project_id, session_factory)
    async with session_factory() as session:
        job = await create_job(session, project_id=project_id, kind="write")
        await session.commit()
    try:
        async with job_context(
            project_id=project_id,
            job_id=job.id,
            settings=template.settings,
            session_factory=session_factory,
        ) as context:
            context._budget_exhausted = True
            await context.raise_if_stopped()
        async with session_factory() as session:
            stored = await get_job(session, job.id)
            assert stored.status == "paused"
            assert stored.error_json == {"code": "agent_budget_exhausted"}
    finally:
        template.http_client.close()


async def test_parallel_events_do_not_lose_checkpoint_updates(session_factory):
    project_id, _ = await _seed(session_factory)
    context = _context(project_id, session_factory)
    async with session_factory() as session:
        job = await create_job(session, project_id=project_id, kind="write")
        context.job_id = job.id
        await session.commit()
    try:
        await asyncio.gather(
            *[context.emit("parallel.fixture", checkpoint={f"node_{i}": i}) for i in range(12)]
        )
        async with session_factory() as session:
            saved = await get_job(session, job.id)
            assert saved.checkpoint_json == {f"node_{i}": i for i in range(12)}
    finally:
        context.http_client.close()
