"""PDF jobs leave their upload retryable when a worker run is abandoned."""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from db import create_job, create_project, create_user, update_job
from db.models.library import LiteraturePdfUpload
from db.models.paper import GenerationJob
from paperforge_worker.config import WorkerSettings
from paperforge_worker.context import (
    JobStopped,
    _pdf_recovery_target,
    job_context,
)


async def _seed_pdf_job(
    session_factory,
    *,
    function: str,
    upload_status: str,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    async with session_factory() as session:
        owner = await create_user(
            session,
            email=f"{uuid.uuid4()}@example.test",
            password_hash="!test-only",
            verified=True,
        )
        project = await create_project(
            session,
            title="PDF job recovery",
            paper_type="review",
            language="en",
            owner_id=owner.id,
        )
        upload_id = uuid.uuid4()
        session.add(
            LiteraturePdfUpload(
                id=upload_id,
                project_id=project.id,
                object_key=f"tests/{upload_id}.pdf",
                filename="paper.pdf",
                mime="application/pdf",
                bytes=16,
                content_hash=upload_id.hex * 2,
                status=upload_status,
            )
        )
        job = await create_job(
            session,
            project_id=project.id,
            kind="ingest",
            checkpoint={
                "resume": {
                    "function": function,
                    "kwargs": {"upload_id": str(upload_id)},
                }
            },
        )
        await update_job(session, job, status="running")
        await session.commit()
        return project.id, upload_id, job.id


def _context_kwargs(
    project_id: uuid.UUID,
    job_id: uuid.UUID,
    session_factory,
) -> dict:
    return {
        "project_id": project_id,
        "job_id": job_id,
        "settings": WorkerSettings(llm_default_provider="noop"),
        "session_factory": session_factory,
    }


async def _upload(
    session_factory,
    upload_id: uuid.UUID,
) -> LiteraturePdfUpload:
    async with session_factory() as session:
        upload = await session.get(LiteraturePdfUpload, upload_id)
        assert upload is not None
        return upload


async def _job(
    session_factory,
    job_id: uuid.UUID,
) -> GenerationJob:
    async with session_factory() as session:
        job = await session.get(GenerationJob, job_id)
        assert job is not None
        return job


@pytest.mark.parametrize(
    ("mode", "job_status"),
    [("cancel", "cancelled"), ("pause", "paused")],
)
async def test_stopped_pdf_match_becomes_retryable(
    session_factory,
    mode: str,
    job_status: str,
) -> None:
    project_id, upload_id, job_id = await _seed_pdf_job(
        session_factory,
        function="run_pdf_match_pipeline",
        upload_status="matching",
    )

    async with job_context(**_context_kwargs(project_id, job_id, session_factory)):
        raise JobStopped(mode)

    upload = await _upload(session_factory, upload_id)
    assert upload.status == "match_failed"
    assert upload.error_json == {
        "reason": "worker_job_terminated",
        "retryable": True,
        "termination": job_status,
        "exception": None,
        "job_id": str(job_id),
    }
    assert (await _job(session_factory, job_id)).status == job_status


async def test_unexpected_parse_exception_becomes_retryable(session_factory) -> None:
    project_id, upload_id, job_id = await _seed_pdf_job(
        session_factory,
        function="run_uploaded_pdf_pipeline",
        upload_status="parsing",
    )

    with pytest.raises(RuntimeError, match="parser crashed"):
        async with job_context(**_context_kwargs(project_id, job_id, session_factory)):
            raise RuntimeError("parser crashed")

    upload = await _upload(session_factory, upload_id)
    assert upload.status == "parse_failed"
    assert upload.error_json is not None
    assert upload.error_json["retryable"] is True
    assert upload.error_json["termination"] == "failed"
    assert upload.error_json["exception"] == "RuntimeError"
    assert (await _job(session_factory, job_id)).status == "failed"


async def test_timed_out_pdf_extraction_becomes_retryable(session_factory) -> None:
    project_id, upload_id, job_id = await _seed_pdf_job(
        session_factory,
        function="run_uploaded_pdf_pipeline",
        upload_status="extracting",
    )
    entered = asyncio.Event()

    async def _pipeline() -> None:
        async with job_context(**_context_kwargs(project_id, job_id, session_factory)):
            entered.set()
            await asyncio.sleep(30)

    task = asyncio.ensure_future(_pipeline())
    await asyncio.wait_for(entered.wait(), timeout=10)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(task, timeout=0.05)

    for _ in range(50):
        upload = await _upload(session_factory, upload_id)
        if upload.status != "extracting":
            break
        await asyncio.sleep(0.02)
    assert upload.status == "parse_failed"
    assert upload.error_json is not None
    assert upload.error_json["retryable"] is True
    assert upload.error_json["exception"] == "CancelledError"


@pytest.mark.parametrize(
    ("function", "upload_status"),
    [
        ("run_pdf_match_pipeline", "needs_confirmation"),
        ("run_uploaded_pdf_pipeline", "ready"),
        ("run_uploaded_pdf_pipeline", "rejected"),
    ],
)
async def test_interrupted_pdf_job_does_not_regress_a_newer_upload_state(
    session_factory,
    function: str,
    upload_status: str,
) -> None:
    project_id, upload_id, job_id = await _seed_pdf_job(
        session_factory,
        function=function,
        upload_status=upload_status,
    )
    async with session_factory() as session:
        upload = await session.get(LiteraturePdfUpload, upload_id)
        assert upload is not None
        upload.error_json = {"reason": "keep_this_state"}
        await session.commit()

    with pytest.raises(RuntimeError):
        async with job_context(**_context_kwargs(project_id, job_id, session_factory)):
            raise RuntimeError("stale worker")

    upload = await _upload(session_factory, upload_id)
    assert upload.status == upload_status
    assert upload.error_json == {"reason": "keep_this_state"}


async def test_old_job_cleanup_does_not_fail_a_new_retry(session_factory) -> None:
    project_id, upload_id, old_job_id = await _seed_pdf_job(
        session_factory,
        function="run_uploaded_pdf_pipeline",
        upload_status="parsing",
    )
    async with session_factory() as session:
        old_job = await session.get(GenerationJob, old_job_id)
        assert old_job is not None
        newer_job = await create_job(
            session,
            project_id=project_id,
            kind="ingest",
            checkpoint={
                "resume": {
                    "function": "run_uploaded_pdf_pipeline",
                    "kwargs": {"upload_id": str(upload_id)},
                }
            },
        )
        # Deliberately keep the same timestamp: equality is ambiguous, so the
        # stale attempt must conservatively leave the active retry alone.
        newer_job.created_at = old_job.created_at
        await update_job(session, newer_job, status="running")
        await session.commit()

    with pytest.raises(RuntimeError):
        async with job_context(**_context_kwargs(project_id, old_job_id, session_factory)):
            raise RuntimeError("old attempt stopped late")

    upload = await _upload(session_factory, upload_id)
    assert upload.status == "parsing"
    assert upload.error_json is None
    assert (await _job(session_factory, old_job_id)).status == "failed"


@pytest.mark.parametrize(
    ("function", "failed_status"),
    [
        ("match_literature_pdf", "match_failed"),
        ("parse_literature_pdf", "parse_failed"),
    ],
)
def test_pdf_recovery_recognizes_resume_aliases(
    function: str,
    failed_status: str,
) -> None:
    upload_id = uuid.uuid4()
    job = SimpleNamespace(
        checkpoint_json={
            "resume": {
                "function": function,
                "kwargs": {"upload_id": str(upload_id)},
            }
        }
    )

    target = _pdf_recovery_target(job)

    assert target is not None
    assert target[0] == upload_id
    assert target[2] == failed_status
