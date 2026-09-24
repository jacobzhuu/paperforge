from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest
from db import latest_document, latest_outline, list_sections
from paperforge_worker.context import JobStopped
from paperforge_worker.orchestration.writing_graph import (
    descendants,
    findings_snapshot,
    layers,
    section_graph,
)
from paperforge_worker.pipelines.document import write_document
from test_repair_scheduling import _context, _recording_writer, _seed


def test_graph_contracts_and_frame_barrier():
    graph = section_graph(
        [
            {"key": "abstract", "kind": "frame"},
            {"key": "a", "independent": True},
            {"key": "b", "independent": True},
            {"key": "compare", "depends_on": ["a", "b"]},
        ]
    )
    assert layers(graph) == [["a", "b"], ["compare"], ["abstract"]]
    assert descendants(graph, {"a"}) == {"a", "compare", "abstract"}
    assert layers(section_graph([{"key": "a"}, {"key": "b"}])) == [["a"], ["b"]]


@pytest.mark.parametrize(
    "sections",
    [
        [{"key": "a", "depends_on": ["missing"]}],
        [{"key": "a", "depends_on": ["b"]}, {"key": "b", "depends_on": ["a"]}],
        [{"key": "a"}, {"key": "a"}],
    ],
)
def test_graph_rejects_unsafe_contracts(sections):
    with pytest.raises(ValueError):
        section_graph(sections)


def test_frame_snapshot_keeps_first_and_last_findings():
    sections = [{"key": "abstract", "kind": "frame"}, {"key": "a"}, {"key": "z"}]
    snapshot = findings_snapshot(sections, {"a": "first finding", "z": "last finding"})
    assert "first finding" in snapshot and "last finding" in snapshot
    assert "abstract" not in snapshot


async def prepare(session_factory, monkeypatch, mode):
    project_id, cite_key = await _seed(session_factory)
    async with session_factory() as session:
        outline = await latest_outline(session, project_id)
        tree = deepcopy(outline.tree_json)
        for section in tree["sections"]:
            if section.get("kind") != "frame":
                section["independent"] = True
        outline.tree_json = tree
        await session.commit()
    context = _context(project_id, session_factory)
    context.settings.writer_execution_mode = mode
    calls, inflight = [], {}
    writer = _recording_writer(
        cite_key, [], inflight=inflight, new_terms={"s1": {"attack": "poisoning"}}
    )

    async def record(**kwargs):
        key = kwargs["section"]["key"]
        calls.append(
            {
                "key": key,
                "summary": kwargs["context"].section_summary(key),
                "terms": dict(kwargs["context"].glossary),
            }
        )
        return await writer(**kwargs)

    monkeypatch.setattr("paperforge_worker.pipelines.document.write_section", record)
    return context, calls, inflight


async def test_dag_serial_parallel_inputs_match_and_memory_resumes(session_factory, monkeypatch):
    seen = {}
    for mode in ("dag_serial", "dag_parallel"):
        context, calls, inflight = await prepare(session_factory, monkeypatch, mode)
        try:
            await write_document(context, coherence=False)
            seen[mode] = deepcopy(calls)
            assert inflight["peak"] == (2 if mode == "dag_parallel" else 1)
            async with session_factory() as session:
                doc = await latest_document(session, context.project_id)
                rows = {r.section_key: r for r in await list_sections(session, doc.id)}
                assert rows["s1"].generation_json["terms"] == {"attack": "poisoning"}
                assert doc.writing_state_json["nodes"]["s1"]["status"] == "completed"
            calls.clear()
            await write_document(context, coherence=False)
            assert calls == [], "completed nodes and frames must survive resume without calls"
        finally:
            context.http_client.close()
    # Request completion/interleaving differs; each node's ordered attempts must not.
    for key in {item["key"] for item in seen["dag_serial"]}:
        assert [c for c in seen["dag_serial"] if c["key"] == key] == [
            c for c in seen["dag_parallel"] if c["key"] == key
        ]
    abstract = next(c for c in seen["dag_parallel"] if c["key"] == "abstract")
    assert "[s1]" in abstract["summary"] and "[s5]" in abstract["summary"]


async def test_manual_edit_cannot_be_overwritten_on_resume(session_factory, monkeypatch):
    context, calls, _ = await prepare(session_factory, monkeypatch, "dag_parallel")
    try:
        await write_document(context, coherence=False)
        async with session_factory() as session:
            doc = await latest_document(session, context.project_id)
            row = next(r for r in await list_sections(session, doc.id) if r.section_key == "s1")
            edited = {**row.body_ir_json, "title": "Human edited"}
            row.body_ir_json = edited
            row.status = "edited"
            await session.commit()
        calls.clear()
        with pytest.raises(JobStopped):
            await write_document(context, coherence=False)
        assert not calls
        async with session_factory() as session:
            rows = await list_sections(session, doc.id)
            assert next(r for r in rows if r.section_key == "s1").body_ir_json == edited
    finally:
        context.http_client.close()


async def test_pause_drains_admitted_nodes_and_resume_reuses_them(session_factory, monkeypatch):
    context, calls, _ = await prepare(session_factory, monkeypatch, "dag_parallel")

    async def stopped():
        return "pause" if len(calls) >= 2 else None

    monkeypatch.setattr(context, "stop_requested", stopped)
    try:
        with pytest.raises(JobStopped):
            await write_document(context, coherence=False)
        async with session_factory() as session:
            doc = await latest_document(session, context.project_id)
            completed = {
                k for k, v in doc.writing_state_json["nodes"].items() if v["status"] == "completed"
            }
        assert completed
        assert {c["key"] for c in calls} == completed

        async def running():
            return None

        monkeypatch.setattr(context, "stop_requested", running)
        calls.clear()
        await write_document(context, coherence=False)
        assert not completed.intersection(c["key"] for c in calls)
    finally:
        context.http_client.close()


async def test_evidence_changes_during_call_are_not_committed(session_factory, monkeypatch):
    context, _, _ = await prepare(session_factory, monkeypatch, "dag_parallel")
    from paperforge_worker.pipelines import document as pipeline

    original = pipeline.write_section
    changed = False

    async def change_outline(**kwargs):
        nonlocal changed
        if not changed:
            changed = True
            async with session_factory() as session:
                outline = await latest_outline(session, context.project_id)
                outline.tree_json = {**outline.tree_json, "topic": "changed while writing"}
                await session.commit()
        await asyncio.sleep(0)
        return await original(**kwargs)

    monkeypatch.setattr(pipeline, "write_section", change_outline)
    try:
        with pytest.raises(JobStopped):
            await write_document(context, coherence=False)
        async with session_factory() as session:
            doc = await latest_document(session, context.project_id)
            assert not await list_sections(session, doc.id)
    finally:
        context.http_client.close()


async def test_final_evaluator_rejects_frames_bound_to_old_body(session_factory, monkeypatch):
    from paperforge_worker.worker import _quality

    context, _, _ = await prepare(session_factory, monkeypatch, "dag_serial")
    try:
        await write_document(context, coherence=False)
        report = await _quality(context, quality_profile="scholarly")
        assert not any(item["code"] == "frame_snapshot_stale" for item in report.blockers)
        async with session_factory() as session:
            doc = await latest_document(session, context.project_id)
            row = next(r for r in await list_sections(session, doc.id) if r.section_key == "s1")
            row.body_ir_json = {**row.body_ir_json, "title": "Revised finding"}
            await session.commit()
        report = await _quality(context, quality_profile="scholarly")
        assert any(item["code"] == "frame_snapshot_stale" for item in report.blockers)
    finally:
        context.http_client.close()


async def test_interrupted_rewrite_with_previous_body_is_not_misclassified_as_manual_edit(
    session_factory,
    monkeypatch,
):
    context, calls, _ = await prepare(session_factory, monkeypatch, "dag_serial")
    try:
        await write_document(context, coherence=False)
        async with session_factory() as session:
            doc = await latest_document(session, context.project_id)
            state = deepcopy(doc.writing_state_json)
            node = state["nodes"]["s1"]
            node["status"] = "started"
            node["base_output_hash"] = node.pop("output_hash")
            doc.writing_state_json = state
            await session.commit()
        calls.clear()
        await write_document(context, coherence=False)
        assert any(c["key"] == "s1" for c in calls)
        assert not any(c["key"] == "s2" for c in calls)
    finally:
        context.http_client.close()


async def test_frame_parallel_inputs_overlap_and_resume(session_factory, monkeypatch):
    histories = []
    for width in (1, 2):
        context, calls, inflight = await prepare(session_factory, monkeypatch, "dag_parallel")
        context.settings.writer_frame_concurrency = width
        from paperforge_worker.pipelines import document as pipeline

        original = pipeline.write_section
        active, peak = 0, 0
        starts, finishes = [], []

        async def frame_writer(*, original=original, starts=starts, finishes=finishes, **kwargs):
            nonlocal active, peak
            frame = kwargs["section"].get("kind") == "frame"
            if frame:
                active += 1
                peak = max(peak, active)
                starts.append(kwargs["section"]["key"])
            try:
                return await original(**kwargs)
            finally:
                if frame:
                    active -= 1
                    finishes.append(kwargs["section"]["key"])

        monkeypatch.setattr(pipeline, "write_section", frame_writer)
        try:
            await write_document(context, coherence=False)
            assert peak == width
            histories.append(
                [c for c in calls if c["key"] in {"abstract", "introduction", "conclusion"}]
            )
            calls.clear()
            await write_document(context, coherence=False)
            assert calls == []
            async with session_factory() as session:
                doc = await latest_document(session, context.project_id)
                rows = await list_sections(session, doc.id)
                for row in rows:
                    if row.section_key in starts:
                        assert row.generation_json["body_snapshot"]
                        assert row.generation_json["input_hash"]
        finally:
            context.http_client.close()
    assert histories[0] == histories[1]


def test_frame_dependencies_and_custom_barriers():
    from paperforge_worker.pipelines.document import _frame_waves

    assert _frame_waves(
        [
            {"key": "body"},
            {"key": "abstract", "kind": "frame"},
            {"key": "conclusion", "kind": "frame", "depends_on": ["abstract", "body"]},
        ]
    ) == [["abstract"], ["conclusion"]]
    assert _frame_waves(
        [
            {"key": "abstract", "kind": "frame"},
            {"key": "custom", "kind": "frame"},
        ]
    ) == [["abstract"], ["custom"]]
    with pytest.raises(ValueError):
        _frame_waves([{"key": "abstract", "depends_on": ["missing"]}])
    with pytest.raises(ValueError):
        _frame_waves(
            [
                {"key": "abstract", "depends_on": ["conclusion"]},
                {"key": "conclusion", "depends_on": ["abstract"]},
            ]
        )


async def test_parallel_frames_drain_on_stop_and_resume_only_missing(session_factory, monkeypatch):
    from paperforge_worker.pipelines import document as pipeline

    context, calls, _ = await prepare(session_factory, monkeypatch, "dag_parallel")
    context.settings.writer_frame_concurrency = 2
    original_write = pipeline.write_section
    original_emit = context.emit
    stop = False
    paid = set()
    committed = []

    async def write(**kwargs):
        key = kwargs["section"]["key"]
        if key in {"abstract", "introduction", "conclusion"}:
            paid.add(key)
            # Deliberately finish out of order.
            await asyncio.sleep(0.03 if key == "abstract" else 0.001)
        return await original_write(**kwargs)

    async def emit(name, payload=None, **kwargs):
        nonlocal stop
        if name == "frame.committed":
            committed.append(payload["section"])
            stop = True
        return await original_emit(name, payload, **kwargs)

    async def check():
        if stop:
            raise JobStopped("pause")

    monkeypatch.setattr(pipeline, "write_section", write)
    monkeypatch.setattr(context, "emit", emit)
    monkeypatch.setattr(context, "raise_if_stopped", check)
    try:
        with pytest.raises(JobStopped):
            await write_document(context, coherence=False)
        assert set(committed) == paid
        assert committed == [k for k in ("abstract", "introduction", "conclusion") if k in paid]
        calls.clear()
        stop = False
        monkeypatch.setattr(context, "emit", original_emit)
        await write_document(context, coherence=False)
        assert all(c["key"] not in paid for c in calls)
    finally:
        context.http_client.close()


async def test_frame_rejects_changed_body_without_overwriting_editor(session_factory, monkeypatch):
    from db.models.paper import PaperSection
    from paperforge_worker.pipelines import document as pipeline
    from sqlalchemy import select

    context, _, _ = await prepare(session_factory, monkeypatch, "dag_parallel")
    context.settings.writer_frame_concurrency = 2
    original = pipeline.write_section
    edited = False
    changed = None

    async def edit_body(**kwargs):
        nonlocal edited, changed
        if kwargs["section"]["key"] == "abstract" and not edited:
            edited = True
            async with session_factory() as session:
                doc = await latest_document(session, context.project_id)
                row = await session.scalar(
                    select(PaperSection).where(
                        PaperSection.document_id == doc.id, PaperSection.section_key == "s1"
                    )
                )
                changed = deepcopy(row.body_ir_json)
                changed["blocks"][0]["runs"][0]["v"] = "Human edited the accepted body."
                row.body_ir_json = changed
                await session.commit()
        return await original(**kwargs)

    monkeypatch.setattr(pipeline, "write_section", edit_body)
    try:
        with pytest.raises(JobStopped, match="writing_inputs_changed"):
            await write_document(context, coherence=False)
        async with session_factory() as session:
            doc = await latest_document(session, context.project_id)
            rows = {r.section_key: r for r in await list_sections(session, doc.id)}
            assert rows["s1"].body_ir_json == changed
            assert "abstract" not in rows
    finally:
        context.http_client.close()


async def test_four_slot_ceiling_preserves_frozen_writer_inputs(session_factory, monkeypatch):
    histories = []
    for width in (1, 4):
        context, calls, inflight = await prepare(session_factory, monkeypatch, "dag_parallel")
        context.settings.writer_concurrency = width
        async with session_factory() as session:
            outline = await latest_outline(session, context.project_id)
            assert len([s for s in outline.tree_json["sections"] if s.get("kind") != "frame"]) >= 4
        try:
            await write_document(context, coherence=False)
            assert inflight["peak"] == width
            histories.append(sorted(calls, key=lambda call: call["key"]))
        finally:
            context.http_client.close()
    assert histories[0] == histories[1]
