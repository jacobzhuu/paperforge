import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from db import latest_document, list_sections
from paperforge_worker.orchestration.dependency_contract import apply_proposals, validate_edit
from paperforge_worker.orchestration.polish_policy import triage
from paperforge_worker.orchestration.writing_graph import layers, section_graph
from paperforge_worker.pipelines.document import write_document
from paperforge_worker.pipelines.writing import SectionDraft, WritingContext
from test_repair_scheduling import _context, _recording_writer, _seed


def test_dependencies_preserve_explicit_parents_and_reject_cycles():
    tree = {
        "sections": [
            {"key": "a", "independent": True},
            {"key": "b"},
            {"key": "child", "parent_key": "a"},
            {"key": "summary", "requires_all_body": True},
        ]
    }
    result = apply_proposals(
        tree,
        [{"key": "b", "depends_on": [], "assessed": True, "reason": "separate research question"}],
    )
    graph = section_graph(result["sections"])
    assert layers(graph)[0] == ["a", "b"]
    assert "a" in graph["child"] and set(graph["summary"]) == {"a", "b", "child"}
    with pytest.raises(ValueError):
        apply_proposals(
            {"sections": [{"key": "a", "depends_on": ["b"]}, {"key": "b", "depends_on": ["a"]}]}, []
        )
    edited = deepcopy(result)
    edited["sections"][1]["title"] = "new argument"
    changed = validate_edit(edited, result)
    assert changed["dependency_contract"]["invalidated"]
    assert changed["sections"][1]["depends_on"] == ["a"]
    forged = deepcopy(result)
    forged["sections"][1]["depends_on"] = ["a"]
    with pytest.raises(ValueError):
        validate_edit(forged, result)


@pytest.mark.parametrize("decision", ["keep", "rewrite", "uncertain", "invalid", "stale", "error"])
async def test_triage_only_skips_complete_hash_bound_keep(decision):
    draft = SectionDraft(
        section_key="s1", title="test", paragraphs=[{"text": "A clear statement."}]
    )

    class Runner:
        async def agenerate_json(self, *args, **kwargs):
            if decision == "error":
                raise RuntimeError("unavailable")
            p = json.loads(kwargs["user_prompt"])
            return SimpleNamespace(
                ok=True,
                value={
                    "section": "s1",
                    "body_hash": "stale" if decision == "stale" else p["body_hash"],
                    "decision": decision,
                    "reason": "checked full section",
                },
            )

    result = await triage({"s1": draft}, WritingContext(outline={}), Runner())
    assert result["s1"]["rewrite"] is (decision != "keep")


async def test_triage_forces_duplicate_without_model():
    class Runner:
        async def agenerate_json(self, *args, **kwargs):
            raise AssertionError("must not call")

    drafts = {
        k: SectionDraft(section_key=k, title=k, paragraphs=[{"text": "duplicate " * 20}])
        for k in ["a", "b"]
    }
    result = await triage(drafts, WritingContext(outline={}), Runner())
    assert all(r["rewrite"] and r["reason"] == "duplicate_paragraph" for r in result.values())


async def test_parallel_polish_freezes_inputs_and_resumes_without_rewrite(
    session_factory, monkeypatch
):
    project_id, cite_key = await _seed(session_factory)
    calls = []
    monkeypatch.setattr(
        "paperforge_worker.pipelines.document.write_section", _recording_writer(cite_key, calls)
    )
    context = _context(project_id, session_factory)
    context.settings.writer_polish_policy = "full_parallel"
    active = peak = 0
    inputs = []

    async def polish(*, draft, context, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        inputs.append(deepcopy(context.rolling_summaries))
        await asyncio.sleep(0.015 if draft.section_key == "s1" else 0.001)
        draft.generation["polish_result"] = {"accepted": True, "changed": False, "reason": None}
        active -= 1
        return draft

    monkeypatch.setattr("paperforge_worker.pipelines.parallel_polish.coherence_pass", polish)
    try:
        result = await write_document(context, coherence=True)
        assert result.complete and peak == 2 and len(inputs) == 6
        assert all(x == inputs[0] for x in inputs)
        async with context.session() as session:
            document = await latest_document(session, project_id)
            ledger = document.writing_state_json["polish"]
            assert len(ledger["results"]) == 6
            assert list(ledger["results"]) == [f"s{i}" for i in range(1, 7)]
            assert len(await list_sections(session, document.id)) == 9
        await write_document(context, coherence=True)
        assert len(inputs) == 6
    finally:
        context.http_client.close()


async def test_parallel_polish_stops_on_external_edit(session_factory, monkeypatch):
    from paperforge_worker.context import JobStopped
    from paperforge_worker.pipelines import parallel_polish

    project_id, cite_key = await _seed(session_factory)
    monkeypatch.setattr(
        "paperforge_worker.pipelines.document.write_section", _recording_writer(cite_key, [])
    )
    context = _context(project_id, session_factory)
    context.settings.writer_polish_policy = "full_parallel"
    edited = False

    async def polish(*, draft, **kwargs):
        nonlocal edited
        if not edited:
            edited = True
            async with context.session() as session:
                document = await latest_document(session, project_id)
                rows = await list_sections(session, document.id)
                target = next(row for row in rows if row.section_key == "s6")
                target.body_ir_json = {**target.body_ir_json, "title": "USER EDIT"}
        draft.generation["polish_result"] = {"accepted": True, "changed": False, "reason": None}
        return draft

    monkeypatch.setattr(parallel_polish, "coherence_pass", polish)
    try:
        with pytest.raises(JobStopped):
            await write_document(context, coherence=True)
        async with context.session() as session:
            document = await latest_document(session, project_id)
            rows = await list_sections(session, document.id)
            assert (
                next(r for r in rows if r.section_key == "s6").body_ir_json["title"] == "USER EDIT"
            )
            assert document.writing_state_json["polish"]["results"] == {}
    finally:
        context.http_client.close()


async def test_selective_polish_persists_skip_and_reuses_decision(session_factory, monkeypatch):
    from paperforge_worker.pipelines import parallel_polish

    project_id, cite_key = await _seed(session_factory)
    monkeypatch.setattr(
        "paperforge_worker.pipelines.document.write_section", _recording_writer(cite_key, [])
    )
    context = _context(project_id, session_factory)
    context.settings.writer_polish_policy = "selective_parallel"
    triage_calls = []

    async def decide(drafts, *args, **kwargs):
        triage_calls.append(1)
        return {key: {"rewrite": False, "reason": "clear"} for key in drafts}

    async def forbidden(**kwargs):
        raise AssertionError("keep decisions must not invoke rewriting")

    monkeypatch.setattr(parallel_polish, "triage", decide)
    monkeypatch.setattr(parallel_polish, "coherence_pass", forbidden)
    try:
        await write_document(context, coherence=True)
        await write_document(context, coherence=True)
        assert triage_calls == [1]
        async with context.session() as session:
            doc = await latest_document(session, project_id)
            assert all(
                not v["rewrite"] for v in doc.writing_state_json["polish"]["results"].values()
            )
    finally:
        context.http_client.close()


async def test_dependency_rebuild_retry_reuses_version_after_completion_failure(
    session_factory, monkeypatch
):
    from db import create_job, latest_outline
    from paperforge_worker import worker
    from paperforge_worker.config import WorkerSettings
    from paperforge_worker.orchestration import dependency_contract
    from paperforge_worker.orchestration.writing_graph import fingerprint

    project_id, _ = await _seed(session_factory)
    async with session_factory() as session:
        source = await latest_outline(session, project_id)
        job = await create_job(session, project_id=project_id, kind="outline")
        await session.commit()
    calls = []

    async def propose(tree, runner):
        calls.append(1)
        return apply_proposals(tree, [])

    finish = worker._finish

    async def fail_after_commit(*args, **kwargs):
        raise RuntimeError("completion event unavailable")

    monkeypatch.setattr(dependency_contract, "propose", propose)
    monkeypatch.setattr(worker, "_finish", fail_after_commit)
    args = (
        {
            "session_factory": session_factory,
            "settings": WorkerSettings(llm_default_provider="noop"),
        },
        str(project_id),
        str(job.id),
        str(source.id),
        fingerprint(source.tree_json),
    )
    with pytest.raises(RuntimeError, match="completion event"):
        await worker.run_dependency_rebuild_pipeline(*args)
    async with session_factory() as session:
        created = await latest_outline(session, project_id)
        assert created.id != source.id
    monkeypatch.setattr(worker, "_finish", finish)
    result = await worker.run_dependency_rebuild_pipeline(*args)
    assert result["outline_id"] == str(created.id) and calls == [1]
    async with session_factory() as session:
        assert (await latest_outline(session, project_id)).id == created.id
        # User-facing resume uses a new queue job with the inherited intent.
        from db import get_job

        prior = await get_job(session, job.id)
        resumed = await create_job(
            session, project_id=project_id, kind="outline", checkpoint=prior.checkpoint_json
        )
        await session.commit()
    resumed_args = (args[0], args[1], str(resumed.id), *args[3:])
    result = await worker.run_dependency_rebuild_pipeline(*resumed_args)
    assert result["outline_id"] == str(created.id) and calls == [1]


async def test_all_section_review_includes_unlinked_body_and_frames(session_factory, monkeypatch):
    from paperforge_worker.worker import _section_review_inputs

    project_id, cite_key = await _seed(session_factory)
    monkeypatch.setattr(
        "paperforge_worker.pipelines.document.write_section", _recording_writer(cite_key, [])
    )
    context = _context(project_id, session_factory)
    try:
        await write_document(context, coherence=False)
        assert await _section_review_inputs(context) == []
        all_sections = await _section_review_inputs(context, include_unlinked=True)
        assert len(all_sections) == 9
        assert {"abstract", "introduction", "conclusion"} <= {
            r["section_key"] for r in all_sections
        }
    finally:
        context.http_client.close()


async def test_triage_is_bounded_and_overlaps_requests():
    active = peak = 0

    class Runner:
        async def agenerate_json(self, *args, **kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1
            p = json.loads(kwargs["user_prompt"])
            return SimpleNamespace(
                ok=True,
                value={
                    "section": p["section"],
                    "body_hash": p["body_hash"],
                    "decision": "keep",
                    "reason": "checked",
                },
            )

    drafts = {
        str(i): SectionDraft(section_key=str(i), title=str(i), paragraphs=[{"text": str(i)}])
        for i in range(5)
    }
    decisions = await triage(drafts, WritingContext(outline={}), Runner(), concurrency=2)
    assert peak == 2 and list(decisions) == list(drafts)
    assert all(not d["rewrite"] for d in decisions.values())


async def test_parallel_polish_pause_drains_paid_candidates_and_resumes_pending_only(
    session_factory, monkeypatch
):
    from paperforge_worker.context import JobStopped
    from paperforge_worker.pipelines import parallel_polish

    project_id, cite_key = await _seed(session_factory)
    body_calls = []
    monkeypatch.setattr(
        "paperforge_worker.pipelines.document.write_section",
        _recording_writer(cite_key, body_calls),
    )
    context = _context(project_id, session_factory)
    context.settings.writer_execution_mode = "dag_parallel"
    context.settings.writer_polish_policy = "full_parallel"
    stopped = False
    calls = []

    async def check():
        if stopped:
            raise JobStopped("pause", reason="test_pause")

    async def polish(*, draft, **kwargs):
        nonlocal stopped
        calls.append(draft.section_key)
        await asyncio.sleep(0.01)
        stopped = True
        draft.paragraphs[0]["text"] += " This wording was reviewed."
        draft.generation["polish_result"] = {"accepted": True, "changed": True, "reason": None}
        return draft

    monkeypatch.setattr(context, "raise_if_stopped", check)
    monkeypatch.setattr(parallel_polish, "coherence_pass", polish)
    try:
        with pytest.raises(JobStopped):
            await write_document(context, coherence=True)
        completed = list(calls)
        body_calls_before_resume = len(body_calls)
        assert 1 <= len(completed) <= 2
        async with context.session() as session:
            doc = await latest_document(session, project_id)
            assert set(doc.writing_state_json["polish"]["results"]) == set(completed)
        stopped = False

        async def running():
            return None

        monkeypatch.setattr(context, "raise_if_stopped", running)
        assert (await write_document(context, coherence=True)).complete
        assert len(calls) == 6 and len(set(calls)) == 6
        assert len([r for r in body_calls if r["key"].startswith("s")]) == body_calls_before_resume
    finally:
        context.http_client.close()


def test_duplicate_dependency_proposals_retain_sequential_barrier():
    result = apply_proposals(
        {"sections": [{"key": "a"}, {"key": "b"}]},
        [
            {"key": "b", "depends_on": ["a"], "assessed": True, "reason": "uses A"},
            {"key": "b", "depends_on": [], "assessed": True, "reason": "independent"},
        ],
    )
    assert result["sections"][1]["depends_on"] == ["a"]
    assert result["sections"][1]["dependency_source"] == "fallback"
