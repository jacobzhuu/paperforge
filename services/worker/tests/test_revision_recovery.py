"""Failure-window tests use an isolated PostgreSQL database and no paid providers."""

import copy
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from db import create_job, list_sections, upsert_section
from db.models.paper import GenerationJob
from db.models.revision import DraftRevision
from db.repositories.revision_recovery import complete_section, recovery_block
from paperforge_worker.context import JobStopped
from paperforge_worker.pipelines import background_revision as bg
from paperforge_worker.pipelines import revision_recovery as recovery
from test_background_revision import seed


async def setup_revision(factory):
    context, source, snapshot = await seed(factory)
    rid = await bg.schedule_revision(
        context, document_id=source, snapshot=snapshot, review_style="narrative"
    )
    async with context.session() as session:
        row = await session.get(DraftRevision, uuid.UUID(rid))
        context.job_id = row.job_id
        candidate = row.candidate_document_id
    context.checkpoint = {"background_revision": True}
    await recovery.initialize(context, rid)
    return context, candidate


async def resumed(context):
    async with context.session() as session:
        old = await session.get(GenerationJob, context.job_id)
        old.status = "paused"
        new = await create_job(session, project_id=context.project_id, kind="revision")
        row = await session.get(DraftRevision, context.revision_id)
        row.job_id = new.id
        context.job_id = new.id
    await recovery.initialize(context, context.revision_id)


async def state(context):
    async with context.session() as session:
        return copy.deepcopy((await session.get(DraftRevision, context.revision_id)).recovery_json)


async def test_output_and_completion_commit_together_and_skip_after_missing_event(session_factory):
    context, candidate = await setup_revision(session_factory)
    await recovery.save_plan(context, {"notes": {"s1": "fix", "s2": "fix"}, "question_ids": []})
    await recovery.begin(context, "section:s1", {"input": "original"})
    async with context.session() as session:
        row = (await list_sections(session, candidate))[0]
        body = {**row.body_ir_json, "title": "Corrected"}
        await upsert_section(
            session,
            document_id=candidate,
            section_key="s1",
            title="Corrected",
            order_no=1,
            body_ir=body,
            cite_keys=[],
        )
        await complete_section(session, context, "s1", body)
    # Simulate process loss after commit but before the SSE event.
    await resumed(context)
    item = {"section_key": "s1", "review_input": {"body": body}}
    assert await recovery.section_completed(context, item)
    assert (await state(context))["operations"]["section:s1"]["attempts"] == 1
    assert (await state(context))["operations"]["section:s2"]["status"] == "pending"
    assert await recovery.begin(context, "section:s2", {"input": "second"}) is None


async def test_rollback_never_claims_completion_and_blocks_ambiguous_replay(session_factory):
    context, candidate = await setup_revision(session_factory)
    await recovery.begin(context, "section:s1", {"input": "original"})
    with pytest.raises(RuntimeError):
        async with context.session() as session:
            row = (await list_sections(session, candidate))[0]
            row.body_ir_json = {**row.body_ir_json, "title": "Uncommitted"}
            await complete_section(session, context, "s1", row.body_ir_json)
            raise RuntimeError("crash before commit")
    assert (await state(context))["operations"]["section:s1"]["status"] == "started"
    async with context.session() as session:
        assert (await list_sections(session, candidate))[0].body_ir_json["title"] == "Test"
    with pytest.raises(JobStopped):
        await resumed(context)


async def test_explicit_failure_retries_only_on_new_job_and_caps_attempts(session_factory):
    context, _ = await setup_revision(session_factory)
    await recovery.begin(context, "section:s1", {"input": "same"})
    await recovery.finish(context, "section:s1", error="no_valid_patch")
    await resumed(context)
    await recovery.begin(context, "section:s1", {"input": "same"})
    await recovery.finish(context, "section:s1", error="no_valid_patch")
    with pytest.raises(JobStopped):
        await resumed(context)
    assert (await state(context))["operations"]["section:s1"]["attempts"] == 2


async def test_completed_step_is_not_called_again_and_unstarted_step_runs(session_factory):
    context, _ = await setup_revision(session_factory)
    await recovery.save_plan(context, {"notes": {}, "question_ids": ["q"]})
    search = AsyncMock(return_value={"new_work_ids": ["work"]})
    assert await recovery.step(context, "search", search) == {"new_work_ids": ["work"]}
    await resumed(context)
    assert await recovery.step(context, "search", search) == {"new_work_ids": ["work"]}
    search.assert_awaited_once()
    screen = AsyncMock(return_value={"selected": 1})
    await recovery.step(context, "screen", screen)
    screen.assert_awaited_once()


async def test_stale_worker_and_other_project_cannot_write_operation(session_factory):
    context, _ = await setup_revision(session_factory)
    current_job = context.job_id
    context.job_id = uuid.uuid4()
    with pytest.raises(ValueError):
        await recovery.begin(context, "section:s1", {})
    context.job_id = current_job
    context.project_id = uuid.uuid4()
    with pytest.raises(ValueError):
        await recovery.begin(context, "section:s1", {})


async def test_stop_before_call_leaves_work_unstarted(session_factory):
    context, _ = await setup_revision(session_factory)
    context.raise_if_stopped.side_effect = JobStopped("cancel")
    with pytest.raises(JobStopped):
        await recovery.begin(context, "section:s1", {})
    assert not (await state(context))["operations"]


async def test_modified_completed_output_cannot_be_reused(session_factory):
    context, candidate = await setup_revision(session_factory)
    await recovery.begin(context, "section:s1", {})
    async with context.session() as session:
        row = (await list_sections(session, candidate))[0]
        await complete_section(session, context, "s1", row.body_ir_json)
        row.body_ir_json = {**row.body_ir_json, "title": "Changed"}
    with pytest.raises(JobStopped):
        await recovery.section_completed(context, {"section_key": "s1", "review_input": {}})


def test_legacy_attempts_and_exhausted_budgets_are_not_retryable():
    legacy = SimpleNamespace(recovery_json=None)
    assert recovery_block(legacy, {"revision_repair_attempts": ["s1"]}) == "legacy_attempt"
    assert recovery_block(legacy, {"revision_retrieval_attempted": True}) == "legacy_attempt"
    modern = SimpleNamespace(recovery_json={"operations": {}})
    assert recovery_block(modern, {"agent_budget": {"stop_reason": "tokens"}}) == "budget_exhausted"
    assert (
        recovery_block(
            modern, {"agent_budget": {"active_seconds": 10, "limits": {"active_seconds": 10}}}
        )
        == "budget_exhausted"
    )


async def test_activity_clock_excludes_pause_but_retains_spend(session_factory, monkeypatch):
    from test_document_persistence import _context

    seeded, _, _ = await seed(session_factory)
    context = _context(seeded.project_id, session_factory)
    context.checkpoint = {
        "background_revision": True,
        "agent_budget": {
            "started_at": "2000-01-01T00:00:00+00:00",
            "calls": 7,
            "reserved_tokens": 400,
            "active_seconds": 12,
        },
    }
    clock = [100.0]
    monkeypatch.setattr(
        "paperforge_worker.context.time", SimpleNamespace(monotonic=lambda: clock[0])
    )
    context.start_revision_clock()
    clock[0] = 105
    budget = context.revision_budget()
    assert budget["active_seconds"] == 17
    assert (budget["calls"], budget["reserved_tokens"]) == (7, 400)
    # New execution may begin days later; only persisted active time is inherited.
    context.checkpoint["agent_budget"] = budget
    context._revision_clock = None
    clock[0] = 100000
    context.start_revision_clock()
    assert context.revision_active_seconds() == 17
    context.settings.agent_max_seconds = 30
    request = SimpleNamespace(
        system_prompt="system",
        user_prompt="input",
        max_output_tokens=10,
        model="fake",
        metadata={"role": "writer"},
    )
    assert await context._reserve_call(request, {}) is not None
    assert context.checkpoint["agent_budget"]["calls"] == 8
    clock[0] += 20
    assert await context._reserve_call(request, {}) is None
    assert context.checkpoint["agent_budget"]["stop_reason"] == "deadline"
    context.http_client.close()


@pytest.mark.parametrize("block", ["unknown", "budget", "legacy"])
async def test_resume_api_rejects_blocked_work_before_creating_job(
    session_factory, monkeypatch, block
):
    from fastapi import HTTPException
    from paperforge_api.routers import projects

    context, _ = await setup_revision(session_factory)
    async with context.session() as session:
        job = await session.get(GenerationJob, context.job_id)
        job.status = "paused"
        if block == "unknown":
            row = await session.get(DraftRevision, context.revision_id)
            row.recovery_json = {"operations": {"section:s1": {"status": "started"}}}
        elif block == "budget":
            job.checkpoint_json = {**job.checkpoint_json, "agent_budget": {"stop_reason": "calls"}}
        else:
            row = await session.get(DraftRevision, context.revision_id)
            row.recovery_json = None
            job.checkpoint_json = {**job.checkpoint_json, "revision_repair_attempts": ["s1"]}
        monkeypatch.setattr(projects, "_require_project_job", AsyncMock(return_value=job))
        start = AsyncMock()
        monkeypatch.setattr(projects, "start_job", start)
        await session.flush()
        with pytest.raises(HTTPException) as error:
            await projects.resume_job(str(context.project_id), str(job.id), session, None)
        assert error.value.status_code == 409
        start.assert_not_awaited()


async def test_revision_listing_does_not_call_running_work_blocked(session_factory, monkeypatch):
    from db.models.paper import PaperProject
    from paperforge_api.routers import revisions

    context, _ = await setup_revision(session_factory)
    await recovery.begin(context, "section:s1", {})
    async with context.session() as session:
        project = await session.get(PaperProject, context.project_id)
        job = await session.get(GenerationJob, context.job_id)
        job.status = "running"
        monkeypatch.setattr(revisions, "get_authorized_project", AsyncMock(return_value=project))
        result = (await revisions.revisions(str(context.project_id), session))["data"][0]
        assert result["resume_block_reason"] is None
        job.status = "paused"
        result = (await revisions.revisions(str(context.project_id), session))["data"][0]
        assert not result["can_resume"]
        assert result["resume_block_code"] == "unknown_outcome"


async def test_dependent_targets_survive_completed_parent_on_resume(session_factory):
    context, _ = await setup_revision(session_factory)
    assert await recovery.repair_targets(context, {"s1", "conclusion"}) == {"s1", "conclusion"}
    await resumed(context)
    assert await recovery.repair_targets(context, set()) == {"s1", "conclusion"}


async def test_duplicate_execution_does_not_claim_candidate_twice(session_factory):
    context, _ = await setup_revision(session_factory)
    assert await recovery.initialize(context, context.revision_id) is None


async def test_writer_persistence_marks_ledger_in_the_same_transaction(
    session_factory, monkeypatch
):
    from db.repositories.revisions import revision_document
    from paperforge_worker.orchestration.writing_graph import fingerprint
    from paperforge_worker.pipelines.document import _persist_draft
    from paperforge_worker.pipelines.writing import SectionDraft

    context, candidate = await setup_revision(session_factory)
    await recovery.begin(context, "section:s1", {})
    async with context.session() as session:
        original = (await list_sections(session, candidate))[0].body_ir_json
    draft = SectionDraft(section_key="s1", title="Corrected")
    draft.paragraphs = [{"text": "A revised paragraph.", "cite_keys": []}]
    draft.generator = "llm:test"
    draft.expected_body_hash = fingerprint(original)
    with revision_document(context.project_id, candidate):
        await _persist_draft(
            context, document_id=candidate, draft=draft, order_no=1, whitelist={}, language="en"
        )
    async with context.session() as session:
        body = (await list_sections(session, candidate))[0].body_ir_json
    op = (await state(context))["operations"]["section:s1"]
    assert op["status"] == "completed"
    from db.repositories.revision_recovery import digest

    assert op["output_hash"] == digest(body)


async def test_real_repair_resumes_failed_section_without_rewriting_saved_sibling(
    session_factory, monkeypatch
):
    from db import document_snapshot_hash, latest_document
    from db.repositories.revisions import revision_document
    from paperforge_worker.pipelines.document import repair_document_sections
    from test_repair_scheduling import _prepare, _recording_writer

    context, cite_key = await _prepare(session_factory, monkeypatch)
    try:
        async with context.session() as session:
            source = await latest_document(session, context.project_id)
            snapshot = document_snapshot_hash(await list_sections(session, source.id))
            job = await create_job(session, project_id=context.project_id, kind="full")
            job.status = "succeeded"
            context.job_id = job.id
        rid = await bg.schedule_revision(
            context, document_id=source.id, snapshot=snapshot, review_style="narrative"
        )
        async with context.session() as session:
            row = await session.get(DraftRevision, uuid.UUID(rid))
            candidate = row.candidate_document_id
            context.job_id = row.job_id
        await recovery.initialize(context, rid)
        notes = {"s1": "repair first", "s4": "repair fourth"}
        await recovery.save_plan(context, {"notes": notes, "question_ids": []})
        monkeypatch.setattr(
            "paperforge_worker.pipelines.semantic_review.review_section",
            AsyncMock(return_value=None),
        )
        records = []
        writer = _recording_writer(cite_key, records)
        failing = [True]

        async def partial_writer(**kwargs):
            draft = await writer(**kwargs)
            if draft.section_key == "s4" and failing[0]:
                draft.generator = "deterministic_fallback"
            return draft

        monkeypatch.setattr("paperforge_worker.pipelines.document.write_section", partial_writer)
        with revision_document(context.project_id, candidate):
            await repair_document_sections(
                context,
                section_keys=set(notes),
                language="en",
                paper_type="review",
                notes=notes,
                concurrency=2,
            )
            with pytest.raises(JobStopped):
                await recovery.ensure_settled(context)
            assert (await state(context))["operations"]["section:s1"]["status"] == "completed"
            assert (await state(context))["operations"]["section:s4"]["status"] == "failed"
            s1_calls = sum(r["key"] == "s1" for r in records)
            await resumed(context)
            failing[0] = False
            await repair_document_sections(
                context,
                section_keys=set(notes),
                language="en",
                paper_type="review",
                notes=notes,
                concurrency=2,
            )
            await recovery.ensure_settled(context)
            assert sum(r["key"] == "s1" for r in records) == s1_calls
            assert (await state(context))["operations"]["section:s4"]["attempts"] == 2
        async with context.session() as session:
            assert (await latest_document(session, context.project_id)).id == source.id
    finally:
        context.http_client.close()


async def test_supplement_resume_keeps_query_groups_and_skips_completed_steps(
    session_factory, monkeypatch
):
    from paperforge_worker import worker as w
    from paperforge_worker.pipelines import fulltext
    from paperforge_worker.pipelines import revision_retrieval as retrieval

    context, _ = await setup_revision(session_factory)
    question_id, work_id = uuid.uuid4(), uuid.uuid4()
    context.settings.search_limit_per_provider = 20
    context.settings.fulltext_max_works = 40
    await recovery.save_plan(context, {"notes": {}, "question_ids": [str(question_id)]})
    question = SimpleNamespace(id=question_id, text="Q", search_query="Q", term_aliases_json=[])
    monkeypatch.setattr(
        retrieval, "get_project", AsyncMock(return_value=SimpleNamespace(scope_json={}))
    )
    monkeypatch.setattr(retrieval, "list_research_questions", AsyncMock(return_value=[question]))
    monkeypatch.setattr(
        retrieval, "list_entries", AsyncMock(return_value=[(None, SimpleNamespace(id=work_id))])
    )
    monkeypatch.setattr(retrieval, "get_cards", AsyncMock(return_value={}))
    monkeypatch.setattr(retrieval, "list_evidence_units", AsyncMock(return_value=[]))
    monkeypatch.setattr(w, "_question_linked_work_ids", AsyncMock(return_value={}))
    monkeypatch.setattr(
        w, "_classify_evidence_gaps", lambda *a, **kw: [(question, "needs_second_source")]
    )
    monkeypatch.setattr(
        w, "_gap_repair_queries", lambda qs, kind: ["alias A", "alias B"] if qs else []
    )
    search = AsyncMock(return_value=SimpleNamespace(to_payload=lambda: {"selected_count": 1}))
    screen = AsyncMock(return_value=SimpleNamespace(to_payload=lambda: {"selected": 1}))
    acquire = AsyncMock(return_value=(SimpleNamespace(to_payload=lambda: {"acquired": 1}), {}))
    monkeypatch.setattr(w, "run_search", search)
    monkeypatch.setattr(w, "screen_eligibility", screen)
    monkeypatch.setattr(w, "_assign_keys", AsyncMock())
    monkeypatch.setattr(w, "acquire_fulltexts", acquire)
    monkeypatch.setattr(
        fulltext,
        "load_persisted_fulltext_sources",
        AsyncMock(return_value={str(work_id): SimpleNamespace(text="stored fulltext")}),
    )
    stages = []
    for name in (
        "generate_cards",
        "extract_evidence_units",
        "build_question_evidence_matrix",
        "synthesize_questions",
    ):
        action = AsyncMock(return_value=SimpleNamespace(to_payload=lambda: {}))
        stages.append(action)
        monkeypatch.setattr(w, name, action)
    stop = [True]

    async def interrupt_before_processing(ctx, key, action):
        if key == "processing" and stop[0]:
            raise JobStopped("pause")
        return await recovery.step(ctx, key, action)

    monkeypatch.setattr(retrieval, "step", interrupt_before_processing)
    with pytest.raises(JobStopped):
        await retrieval.supplement(context, language="en", question_ids={question_id})
    await resumed(context)
    stop[0] = False
    await retrieval.supplement(context, language="en", question_ids={question_id})
    search.assert_awaited_once()
    assert search.call_args.kwargs["query_texts"] == ["alias A", "alias B"]
    assert (
        len(search.call_args.kwargs["providers"]) * len(search.call_args.kwargs["query_texts"])
        <= 12
    )
    screen.assert_awaited_once()
    acquire.assert_awaited_once()
    for stage in stages:
        stage.assert_awaited_once()


async def test_changed_sources_block_resume_before_writer(session_factory):
    context, _ = await setup_revision(session_factory)
    await recovery.guard_sources(context, {"evidence": ["original"]})
    await resumed(context)
    with pytest.raises(JobStopped):
        await recovery.guard_sources(context, {"evidence": ["edited"]})
    assert (await state(context))["blocked"] == "inputs_changed"
