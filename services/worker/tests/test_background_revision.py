import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from db import (
    create_document,
    create_job,
    create_project,
    create_user,
    latest_document,
    list_sections,
    upsert_section,
)
from db.models.paper import JobDispatch
from db.models.revision import DraftRevision
from db.repositories.revisions import revision_document
from paperforge_worker.pipelines import background_revision as bg
from sqlalchemy import select


class Context:
    def __init__(self, factory, project_id, job_id):
        self.session_factory, self.project_id, self.job_id = factory, project_id, job_id
        self.settings = SimpleNamespace(redis_url="redis://localhost:6379/15")
        self.checkpoint = {}
        self.emit = AsyncMock()
        self.raise_if_stopped = AsyncMock()

    @asynccontextmanager
    async def session(self):
        async with self.session_factory() as session:
            yield session
            await session.commit()


async def seed(factory):
    from db import document_snapshot_hash

    async with factory() as session:
        owner = await create_user(
            session, email=f"{uuid.uuid4()}@example.test", password_hash="!", verified=True
        )
        project = await create_project(
            session, owner_id=owner.id, title="test", paper_type="review", language="zh"
        )
        doc = await create_document(session, project_id=project.id, outline_id=None)
        await upsert_section(
            session,
            document_id=doc.id,
            section_key="s1",
            title="Test",
            order_no=1,
            body_ir={"key": "s1", "title": "Test", "level": 1, "blocks": []},
            cite_keys=[],
        )
        job = await create_job(session, project_id=project.id, kind="full")
        job.status = "succeeded"
        snapshot = document_snapshot_hash(await list_sections(session, doc.id))
        await session.commit()
        return Context(factory, project.id, job.id), doc.id, snapshot


async def test_outbox_and_candidate_are_idempotent_and_never_current(session_factory):
    context, source, snapshot = await seed(session_factory)
    first = await bg.schedule_revision(
        context, document_id=source, snapshot=snapshot, review_style="systematic"
    )
    second = await bg.schedule_revision(
        context, document_id=source, snapshot=snapshot, review_style="systematic"
    )
    assert first == second
    async with session_factory() as session:
        record = await session.get(DraftRevision, uuid.UUID(first))
        assert (await latest_document(session, context.project_id)).id == source
        dispatch = await session.scalar(
            select(JobDispatch).where(JobDispatch.job_id == record.job_id)
        )
        assert dispatch.kwargs_json["revision_id"] == first
        assert dispatch.kwargs_json["review_style"] == "systematic"
        assert dispatch.dispatched_at is None
        with revision_document(context.project_id, record.candidate_document_id):
            assert (
                await latest_document(session, context.project_id)
            ).id == record.candidate_document_id
        assert (await latest_document(session, context.project_id)).id == source


async def test_edited_source_does_not_spawn_candidate(session_factory):
    context, source, snapshot = await seed(session_factory)
    async with context.session() as session:
        rows = await list_sections(session, source)
        rows[0].body_ir_json = {**rows[0].body_ir_json, "title": "Edited"}
    assert (
        await bg.schedule_revision(
            context, document_id=source, snapshot=snapshot, review_style="narrative"
        )
        is None
    )


async def test_adoption_conflict_preserves_user_edit(session_factory, monkeypatch):
    from fastapi import HTTPException
    from paperforge_api.routers import revisions

    context, source, snapshot = await seed(session_factory)
    key = await bg.schedule_revision(
        context, document_id=source, snapshot=snapshot, review_style="narrative"
    )
    async with context.session() as session:
        record = await session.get(DraftRevision, uuid.UUID(key))
        record.status = "ready"
        from db.models.paper import GenerationJob, PaperProject

        (await session.get(GenerationJob, record.job_id)).status = "succeeded"
        project = await session.get(PaperProject, context.project_id)
        monkeypatch.setattr(revisions, "get_authorized_project", AsyncMock(return_value=project))
        rows = await list_sections(session, source)
        rows[0].body_ir_json = {**rows[0].body_ir_json, "title": "My edit"}
    async with context.session() as session:
        with pytest.raises(HTTPException, match="409"):
            await revisions.accept(str(context.project_id), uuid.UUID(key), session)
    async with context.session() as session:
        assert (await latest_document(session, context.project_id)).id == source
        assert (await list_sections(session, source))[0].body_ir_json["title"] == "My edit"


async def test_fast_delivery_orders_render_before_background(session_factory, monkeypatch):
    from paperforge_worker import worker as w

    context, source, snapshot = await seed(session_factory)
    events = []

    async def stage(_context, name, action):
        return await action()

    async def quality(_context, **kw):
        assert kw["deterministic_only"] is True
        events.append("checks")
        return SimpleNamespace(report_id=None, to_payload=lambda: {})

    async def export(_context, **kw):
        assert kw["paper_snapshot_hash"] == snapshot
        events.append("pdf")
        return SimpleNamespace(
            to_payload=lambda: {"formats": {"pdf": "private-key"}, "compile_ok": True}
        )

    async def schedule(*args, **kw):
        events.append("background")
        return "revision"

    monkeypatch.setattr(w, "_run_stage", stage)
    monkeypatch.setattr(w, "_quality", quality)
    monkeypatch.setattr(w, "export_document", export)
    monkeypatch.setattr(w, "_object_store", lambda _: None)
    monkeypatch.setattr(w, "_finish", AsyncMock())
    monkeypatch.setattr(bg, "schedule_revision", schedule)
    await bg.deliver_fast_draft(context, review_style="systematic")
    assert events == ["checks", "pdf", "background"]
    w._finish.assert_awaited_once_with(context, delivered=True)


async def test_background_failure_does_not_hide_delivered_pdf(session_factory, monkeypatch):
    from paperforge_worker import worker as w

    context, source, snapshot = await seed(session_factory)
    # A completed first render must remain successful even if outbox creation needs retry.
    monkeypatch.setattr(
        w,
        "_run_stage",
        AsyncMock(
            side_effect=[
                SimpleNamespace(report_id=None, to_payload=lambda: {}),
                SimpleNamespace(to_payload=lambda: {"formats": {"pdf": "file"}}),
            ]
        ),
    )
    monkeypatch.setattr(
        bg, "schedule_revision", AsyncMock(side_effect=RuntimeError("database unavailable"))
    )
    monkeypatch.setattr(w, "_finish", AsyncMock())
    await bg.deliver_fast_draft(context, review_style="narrative")
    w._finish.assert_awaited_once_with(context, delivered=True)


async def test_adoption_creates_new_version_and_preserves_source(session_factory, monkeypatch):
    from db.models.paper import GenerationJob, PaperProject
    from paperforge_api.routers import revisions

    context, source, snapshot = await seed(session_factory)
    key = await bg.schedule_revision(
        context, document_id=source, snapshot=snapshot, review_style="narrative"
    )
    async with context.session() as session:
        record = await session.get(DraftRevision, uuid.UUID(key))
        record.status = "needs_review"
        (await session.get(GenerationJob, record.job_id)).status = "succeeded"
        project = await session.get(PaperProject, context.project_id)
        monkeypatch.setattr(revisions, "get_authorized_project", AsyncMock(return_value=project))
        candidate = record.candidate_document_id
        rows = await list_sections(session, candidate)
        rows[0].body_ir_json = {**rows[0].body_ir_json, "title": "Corrected"}
        from db import create_quality_report, document_snapshot_hash
        from db.models.paper import ExportArtifact, PaperDocument

        candidate_doc = await session.get(PaperDocument, candidate)
        corrected_hash = document_snapshot_hash(rows)
        report = await create_quality_report(
            session,
            project_id=context.project_id,
            document_id=candidate,
            document_version=candidate_doc.version,
            paper_snapshot_hash=corrected_hash,
            quality_profile="draft",
            review_style="narrative",
            readiness_status="draft",
            blockers=[],
            warnings=[{"code": "still_needs_review"}],
            scores={},
            metrics={},
        )
        session.add(
            ExportArtifact(
                project_id=context.project_id,
                document_version=candidate_doc.version,
                format="pdf",
                object_key="candidate.pdf",
                quality_report_id=report.id,
                paper_snapshot_hash=corrected_hash,
            )
        )
    async with context.session() as session:
        result = await revisions.accept(str(context.project_id), uuid.UUID(key), session)
        assert result["document_id"] not in {str(source), str(candidate)}
    async with context.session() as session:
        current = await latest_document(session, context.project_id)
        assert str(current.id) == result["document_id"]
        assert (await list_sections(session, current.id))[0].body_ir_json["title"] == "Corrected"
        assert (await list_sections(session, source))[0].body_ir_json["title"] == "Test"
        from db import latest_quality_report, list_export_artifacts

        report = await latest_quality_report(session, context.project_id)
        assert report.document_id == current.id
        assert report.warnings_json == [{"code": "still_needs_review"}]
        artifacts = await list_export_artifacts(session, context.project_id)
        assert len(artifacts) == 1
        assert artifacts[0].document_version == current.version
        assert artifacts[0].object_key == "candidate.pdf"
        assert artifacts[0].quality_report_id == report.id


async def test_background_delivery_stays_on_candidate_and_reports_remaining_problems(
    session_factory, monkeypatch
):
    from paperforge_worker import worker as w
    from paperforge_worker.config import WorkerSettings
    from paperforge_worker.pipelines.quality import QualityReport

    context, source, snapshot = await seed(session_factory)
    key = await bg.schedule_revision(
        context, document_id=source, snapshot=snapshot, review_style="narrative"
    )
    async with context.session() as session:
        record = await session.get(DraftRevision, uuid.UUID(key))
        context.job_id = record.job_id
        candidate = record.candidate_document_id
    context.settings = WorkerSettings(_env_file=None)
    context.checkpoint = {"background_revision": True}

    @asynccontextmanager
    async def job_context(**kwargs):
        yield context

    async def stage(_context, name, action):
        return await action()

    async def export(_context, **kwargs):
        async with context.session() as session:
            assert (await latest_document(session, context.project_id)).id == candidate
        return SimpleNamespace(to_payload=lambda: {"formats": {"pdf": "candidate.pdf"}})

    report = QualityReport(
        document_version=2,
        paper_snapshot_hash=snapshot,
        warnings=[{"code": "numeric_locator_missing"}],
    )
    monkeypatch.setattr(bg, "job_context", job_context)
    monkeypatch.setattr(bg, "_review", AsyncMock(return_value=([], {}, ["s1"])))
    monkeypatch.setattr(w, "_mark_running", AsyncMock())
    monkeypatch.setattr(w, "_finish", AsyncMock())
    monkeypatch.setattr(w, "_quality", AsyncMock(return_value=report))
    monkeypatch.setattr(w, "_publish_quality_report", AsyncMock())
    monkeypatch.setattr(w, "_run_stage", stage)
    monkeypatch.setattr(w, "export_document", export)
    monkeypatch.setattr(w, "_object_store", lambda _: None)
    result = await bg.run_background_revision(
        {"settings": context.settings},
        str(context.project_id),
        str(context.job_id),
        revision_id=key,
    )
    assert result["status"] == "needs_review"
    assert result["review"]["unassessed"] == ["s1"]
    async with context.session() as session:
        assert (await latest_document(session, context.project_id)).id == source
        assert (await session.get(DraftRevision, uuid.UUID(key))).status == "needs_review"


async def test_first_draft_quality_never_calls_a_model(session_factory, monkeypatch):
    from paperforge_worker import worker as w
    from test_document_persistence import _context

    seeded, source, _ = await seed(session_factory)
    context = _context(seeded.project_id, session_factory)
    # Even when a verifier is configured, deterministic delivery cannot invoke it.
    runner = SimpleNamespace(enabled=True, agenerate=AsyncMock(side_effect=AssertionError("LLM")))
    monkeypatch.setattr(type(context), "llm_runner", lambda self: runner)
    checker = AsyncMock(side_effect=AssertionError("semantic check before first PDF"))
    monkeypatch.setattr(w, "soft_check_citations", checker)
    report = await w._quality(context, quality_profile="draft", deterministic_only=True)
    assert report.depth_metrics["review_coverage"]["status"] == "pending"
    checker.assert_not_awaited()
    runner.agenerate.assert_not_awaited()
    context.http_client.close()


async def test_candidate_api_shows_live_progress_until_final_report(session_factory, monkeypatch):
    from db.models.paper import GenerationJob, PaperProject
    from paperforge_api.routers import revisions

    context, source, snapshot = await seed(session_factory)
    key = await bg.schedule_revision(
        context, document_id=source, snapshot=snapshot, review_style="narrative"
    )
    async with context.session() as session:
        record = await session.get(DraftRevision, uuid.UUID(key))
        record.status = "running"
        job = await session.get(GenerationJob, record.job_id)
        job.checkpoint_json = {
            "manuscript_review_progress": {"claim_count": 20, "checked_count": 8}
        }
        project = await session.get(PaperProject, context.project_id)
        monkeypatch.setattr(revisions, "get_authorized_project", AsyncMock(return_value=project))
        assert (await revisions.revisions(str(context.project_id), session))["data"][0]["review"][
            "checked_count"
        ] == 8
        record.status = "needs_review"
        record.review_json = {"claim_count": 20, "checked_count": 20}
        assert (await revisions.revisions(str(context.project_id), session))["data"][0]["review"][
            "checked_count"
        ] == 20
