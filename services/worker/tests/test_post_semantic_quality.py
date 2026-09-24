"""A semantic rewrite must not inherit the previous manuscript's export clearance."""

from __future__ import annotations

import pytest
from paperforge_worker import worker
from paperforge_worker.context import JobStopped
from paperforge_worker.pipelines.quality import QualityReport
from test_semantic_convergence import _context


@pytest.mark.parametrize("profile", ["draft", "scholarly", "submission"])
async def test_rewritten_manuscript_uses_new_report_and_gate(monkeypatch, profile):
    context = _context()
    calls = []
    old = QualityReport(paper_snapshot_hash="old", readiness_status="preflight_ready")
    new = QualityReport(paper_snapshot_hash="new", readiness_status="needs_revision")

    async def snapshot(_context):
        return "doc", "new"

    async def quality(_context, **kwargs):
        calls.append(kwargs["quality_profile"])
        return new

    monkeypatch.setattr(worker, "_current_document_snapshot", snapshot)
    monkeypatch.setattr(worker, "_quality", quality)
    result = await worker._quality_after_semantics(
        context, old, quality_profile=profile, review_style="narrative"
    )
    assert result is new
    assert calls == [profile]
    assert worker.can_render_after_quality(profile, result.readiness_status) == (profile == "draft")


async def test_unchanged_manuscript_avoids_extra_model_calls(monkeypatch):
    report = QualityReport(paper_snapshot_hash="same", readiness_status="preflight_ready")

    async def snapshot(_context):
        return "doc", "same"

    async def forbidden(*args, **kwargs):
        raise AssertionError("unchanged manuscript should reuse its assessment")

    monkeypatch.setattr(worker, "_current_document_snapshot", snapshot)
    monkeypatch.setattr(worker, "_quality", forbidden)
    assert (
        await worker._quality_after_semantics(
            _context(), report, quality_profile="scholarly", review_style="narrative"
        )
        is report
    )


async def test_submission_rechecks_even_when_old_report_failed(monkeypatch):
    old = QualityReport(paper_snapshot_hash="same", readiness_status="needs_revision")
    new = QualityReport(paper_snapshot_hash="same", readiness_status="submission_ready")

    async def snapshot(_context):
        return "doc", "same"

    async def quality(_context, **kwargs):
        assert kwargs["quality_profile"] == "submission"
        return new

    monkeypatch.setattr(worker, "_current_document_snapshot", snapshot)
    monkeypatch.setattr(worker, "_quality", quality)
    assert (
        await worker._quality_after_semantics(
            _context(), old, quality_profile="submission", review_style="narrative"
        )
        is new
    )


async def test_failed_reassessment_clears_checkpoint_clearance(monkeypatch):
    context = _context()
    context.checkpoint["quality"] = {"readiness_status": "preflight_ready", "report_id": "old"}

    async def snapshot(_context):
        return "doc", "new"

    async def unavailable(*args, **kwargs):
        raise RuntimeError("verifier unavailable")

    monkeypatch.setattr(worker, "_current_document_snapshot", snapshot)
    monkeypatch.setattr(worker, "_quality", unavailable)
    result = await worker._quality_after_semantics(
        context,
        QualityReport(paper_snapshot_hash="old"),
        quality_profile="scholarly",
        review_style="narrative",
    )
    assert not worker.can_render_after_quality("scholarly", result.readiness_status)
    assert context.checkpoint["quality"] == {"readiness_status": "unassessed"}
    assert result.report_id is None


async def test_pause_propagates_through_reassessment(monkeypatch):
    async def snapshot(_context):
        return "doc", "new"

    async def stopped(*args, **kwargs):
        raise JobStopped("pause")

    monkeypatch.setattr(worker, "_current_document_snapshot", snapshot)
    monkeypatch.setattr(worker, "_quality", stopped)
    with pytest.raises(JobStopped):
        await worker._quality_after_semantics(
            _context(), None, quality_profile="scholarly", review_style="narrative"
        )


@pytest.mark.parametrize(
    "new_readiness,expected_export",
    [
        ("needs_revision", False),
        ("preflight_ready", True),
    ],
)
async def test_full_pipeline_export_uses_post_semantic_quality(
    monkeypatch,
    new_readiness,
    expected_export,
):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    context = _context()
    context.settings = SimpleNamespace(writer_repair_concurrency=1)
    exported = []
    old = QualityReport(paper_snapshot_hash="old", readiness_status="preflight_ready")
    new = QualityReport(paper_snapshot_hash="new", readiness_status=new_readiness)

    @asynccontextmanager
    async def session():
        yield None

    @asynccontextmanager
    async def job_context(**kwargs):
        yield context

    async def project(*args):
        return SimpleNamespace(
            language="en", title="Test", publication_title=None, paper_type="original"
        )

    async def library(*args, **kwargs):
        return {}

    async def stage(ctx, name, callback, **kwargs):
        if name == "outline":
            return SimpleNamespace(outline_id="outline", to_payload=lambda: {})
        if name == "write":
            return SimpleNamespace(section_count=1, document_id="doc", to_payload=lambda: {})
        if name == "quality":
            return old
        if name == "render":
            return await callback()
        return None

    async def semantics(*args, **kwargs):
        return {"rounds": [{"routes": {"s1": "rewrite"}}]}

    async def snapshot(*args):
        return "doc", "new"

    async def assess(*args, **kwargs):
        return new

    async def export(*args, **kwargs):
        exported.append(kwargs)
        return SimpleNamespace(to_payload=lambda: {})

    async def finish(*args, **kwargs):
        pass

    context.session = session
    monkeypatch.setattr(worker, "job_context", job_context)
    monkeypatch.setattr(worker, "get_project", project)
    monkeypatch.setattr(worker, "run_library_pipeline", library)
    monkeypatch.setattr(worker, "_run_stage", stage)
    monkeypatch.setattr(worker, "_converge_section_semantics", semantics)
    monkeypatch.setattr(worker, "_current_document_snapshot", snapshot)
    monkeypatch.setattr(worker, "_quality", assess)
    monkeypatch.setattr(worker, "export_document", export)
    monkeypatch.setattr(worker, "_object_store", lambda _: None)
    monkeypatch.setattr(worker, "_finish", finish)
    monkeypatch.setattr(worker, "_finish_incomplete", finish)
    monkeypatch.setattr(worker, "_finish_needs_input", finish)
    result = await worker.run_full_pipeline(
        {"settings": context.settings}, str(context.project_id), quality_profile="scholarly"
    )
    assert bool(exported) == expected_export
    assert result["quality"]["paper_snapshot_hash"] == "new"
    if exported:
        assert exported[0]["paper_snapshot_hash"] == "new"


async def test_identical_rewrite_republishes_stale_report_without_model_calls(
    session_factory,
    monkeypatch,
):
    from db import create_document, get_section, upsert_section
    from db.models.paper import QualityReportRecord
    from test_document_persistence import _context as db_context
    from test_document_persistence import _seed

    project_id, _ = await _seed(session_factory)
    context = db_context(project_id, session_factory)
    body = {"blocks": [{"type": "paragraph", "text": "Evidence-supported text."}]}
    async with context.session() as session:
        document = await create_document(session, project_id=project_id, outline_id=None)
        await upsert_section(
            session,
            document_id=document.id,
            section_key="s1",
            title="Findings",
            order_no=0,
            body_ir=body,
            cite_keys=[],
        )
    _, snapshot = await worker._current_document_snapshot(context)
    report = QualityReport(paper_snapshot_hash=snapshot, readiness_status="preflight_ready")
    await worker._publish_quality_report(
        context,
        report,
        document_id=document.id,
        document_version=document.version,
        quality_profile="scholarly",
        review_style="narrative",
    )
    original_id = report.report_id
    async with context.session() as session:
        row = await get_section(session, document_id=document.id, section_key="s1")
        await upsert_section(
            session,
            document_id=document.id,
            section_key="s1",
            title=row.title,
            order_no=0,
            body_ir=body,
            cite_keys=[],
        )

    async def forbidden(*args, **kwargs):
        raise AssertionError("identical rewrite must not spend another verification call")

    monkeypatch.setattr(worker, "_quality", forbidden)
    result = await worker._quality_after_semantics(
        context, report, quality_profile="scholarly", review_style="narrative"
    )
    assert result.report_id != original_id
    import uuid

    async with context.session() as session:
        record = await session.get(QualityReportRecord, uuid.UUID(result.report_id))
        assert not record.stale
        assert record.paper_snapshot_hash == snapshot
