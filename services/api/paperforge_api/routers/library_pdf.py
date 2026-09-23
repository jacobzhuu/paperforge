"""Private scholarly-PDF upload, match confirmation, and download routes."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import quote

from arq.connections import ArqRedis
from db import (
    assign_bibtex_key,
    get_work_authors,
    reference_metadata_payload,
    set_entry_status,
    upsert_entry,
)
from db.models.library import DocumentFile, LiteraturePdfUpload, ScholarlyWork
from db.models.paper import GenerationJob
from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile, status
from paper_ir import ReferenceMetadata, make_bibtex_key
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from storage import make_object_store

from paperforge_api.config import get_settings
from paperforge_api.deps import authorize_project_request, get_queue, get_session
from paperforge_api.deps import get_authorized_project as _require_project
from paperforge_api.jobs import ensure_project_job_slot, require_queue
from paperforge_api.schemas import (
    ConfirmLiteraturePdfRequest,
    JobResponse,
    LiteraturePdfUploadResponse,
    LiteraturePdfUploadStartedResponse,
    PdfExtractedMetadataResponse,
    ScholarlyWorkResponse,
)

router = APIRouter(
    prefix="/api/v1",
    tags=["library-pdf"],
    dependencies=[Depends(authorize_project_request)],
)

SessionDep = Annotated[AsyncSession, Depends(get_session, scope="function")]
QueueDep = Annotated[ArqRedis | None, Depends(get_queue)]

MAX_LITERATURE_PDF_BYTES = 64 * 1024 * 1024


@router.post(
    "/projects/{project_id}/library/pdf-uploads",
    response_model=LiteraturePdfUploadStartedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def upload_literature_pdf(
    project_id: str,
    session: SessionDep,
    queue: QueueDep,
    file: Annotated[UploadFile, File()],
) -> LiteraturePdfUploadStartedResponse:
    """Persist a PDF privately, then asynchronously identify its scholarly work."""
    project = await _require_project(session, project_id)
    await require_queue(queue)
    await ensure_project_job_slot(session, project.id, queue)
    content = await file.read(MAX_LITERATURE_PDF_BYTES + 1)
    if not content:
        raise HTTPException(status_code=422, detail="uploaded PDF is empty")
    if len(content) > MAX_LITERATURE_PDF_BYTES:
        raise HTTPException(status_code=413, detail="PDF exceeds 64 MiB limit")
    if not content.startswith(b"%PDF-"):
        raise HTTPException(status_code=422, detail="file is not a PDF")

    upload_id = uuid.uuid4()
    filename = _safe_name(file.filename or "paper.pdf")
    if not filename.lower().endswith(".pdf"):
        filename = f"{filename}.pdf"
    object_key = f"users/{project.owner_id}/projects/{project.id}/literature/{upload_id}-{filename}"
    store = await asyncio.to_thread(make_object_store, get_settings())
    try:
        # Object stores cannot participate in the database transaction. Keep
        # the write in the guarded region so even an ambiguous remote failure
        # gets a best-effort compensating delete.
        await asyncio.to_thread(
            store.put,
            object_key,
            content,
            content_type="application/pdf",
        )
        upload = LiteraturePdfUpload(
            id=upload_id,
            project_id=project.id,
            filename=filename,
            object_key=object_key,
            mime="application/pdf",
            bytes=len(content),
            content_hash=hashlib.sha256(content).hexdigest(),
            status="matching",
        )
        session.add(upload)
        await session.flush()
        job = await _commit_and_enqueue_pdf_job(
            session,
            queue,
            project_id=project.id,
            function="run_pdf_match_pipeline",
            upload=upload,
            enqueue_failure_status="match_failed",
            upload_id=str(upload.id),
        )
    except HTTPException:
        # Queue failures happen after the upload/job transaction is committed;
        # the helper has already put the upload into a retryable failure state.
        raise
    except Exception:
        # The database transaction will roll back; remove the object that is not
        # part of that transaction so a failed enqueue cannot orphan private data.
        try:
            await asyncio.to_thread(store.delete, object_key)
        except Exception:  # noqa: BLE001 - preserve the original failure
            pass
        raise
    return LiteraturePdfUploadStartedResponse(
        upload=await _upload_response(session, upload),
        job=_job_response(job),
    )


@router.get(
    "/projects/{project_id}/library/pdf-uploads",
    response_model=list[LiteraturePdfUploadResponse],
)
async def get_literature_pdf_uploads(
    project_id: str,
    session: SessionDep,
    include_completed: bool = Query(default=False),
) -> list[LiteraturePdfUploadResponse]:
    project = await _require_project(session, project_id)
    statement = (
        select(LiteraturePdfUpload)
        .where(LiteraturePdfUpload.project_id == project.id)
        .order_by(LiteraturePdfUpload.created_at.desc(), LiteraturePdfUpload.id.desc())
        .limit(500)
    )
    if not include_completed:
        # The workbench is an action queue. Terminal history must not push an
        # older confirmation/failure out of the bounded response.
        statement = statement.where(LiteraturePdfUpload.status.not_in({"ready", "rejected"}))
    uploads = list((await session.scalars(statement)).all())
    return [await _upload_response(session, upload) for upload in uploads]


@router.post(
    "/projects/{project_id}/library/pdf-uploads/{upload_id}/confirm",
    response_model=LiteraturePdfUploadStartedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def confirm_literature_pdf(
    project_id: str,
    upload_id: str,
    request: ConfirmLiteraturePdfRequest,
    session: SessionDep,
    queue: QueueDep,
) -> LiteraturePdfUploadStartedResponse:
    """Confirm the proposed match, bind the private document, and parse it."""
    project = await _require_project(session, project_id)
    await require_queue(queue)
    await _lock_project_library(session, project.id)
    upload = await _require_upload(session, project.id, upload_id, for_update=True)
    if upload.status != "needs_confirmation" or upload.matched_work_id is None:
        raise HTTPException(status_code=409, detail="PDF upload has no match awaiting confirmation")
    work = await session.get(ScholarlyWork, upload.matched_work_id)
    if work is None:
        raise HTTPException(status_code=409, detail="matched scholarly work no longer exists")
    if work.is_retracted:
        raise HTTPException(
            status_code=409,
            detail="a retracted work cannot enter the writing library",
        )

    entry, _created = await upsert_entry(
        session,
        project_id=project.id,
        work_id=work.id,
        added_via="pdf_upload",
        status="selected",
        rank_reason={
            "method": "user_pdf_confirmed",
            "matched_by": upload.match_method,
            "confidence": upload.match_confidence,
        },
        verified=True,
    )
    # Confirmation is an explicit admission action. It must also revive an
    # existing candidate/excluded row instead of binding a PDF to an entry that
    # remains outside the writing whitelist.
    await set_entry_status(session, entry, "selected")
    # Omitting the role means “keep the project's existing judgment”. This
    # prevents attaching a fuller source from silently demoting a core paper.
    if request.literature_role is not None:
        entry.literature_role = request.literature_role
        entry.user_pinned = request.literature_role == "core"
    payload = await reference_metadata_payload(session, work)
    await assign_bibtex_key(
        session,
        entry,
        make_bibtex_key,
        reference=ReferenceMetadata(**payload),
    )

    document = await session.scalar(
        select(DocumentFile).where(
            DocumentFile.project_id == project.id,
            DocumentFile.work_id == work.id,
            DocumentFile.content_hash == upload.content_hash,
        )
    )
    if document is None:
        document = DocumentFile(
            project_id=project.id,
            access_scope="private",
            work_id=work.id,
            kind="uploaded_pdf",
            object_key=upload.object_key,
            mime="application/pdf",
            bytes=upload.bytes,
            content_hash=upload.content_hash,
        )
        session.add(document)
        await session.flush()
    else:
        # The same bytes may be uploaded again to repair a previously missing
        # object. Make the freshly confirmed private copy authoritative; older
        # upload rows still retain their own private object keys for download.
        document.object_key = upload.object_key
        document.kind = "uploaded_pdf"
        document.mime = "application/pdf"
        document.bytes = upload.bytes
    upload.document_file_id = document.id
    upload.status = "parsing"
    upload.confirmed_at = datetime.now(UTC)
    upload.error_json = None
    await session.flush()

    job = await _commit_and_enqueue_pdf_job(
        session,
        queue,
        project_id=project.id,
        function="run_uploaded_pdf_pipeline",
        upload=upload,
        enqueue_failure_status="parse_failed",
        upload_id=str(upload.id),
    )
    return LiteraturePdfUploadStartedResponse(
        upload=await _upload_response(session, upload),
        job=_job_response(job),
    )


@router.delete(
    "/projects/{project_id}/library/pdf-uploads/{upload_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def reject_literature_pdf(
    project_id: str,
    upload_id: str,
    session: SessionDep,
) -> Response:
    """Reject an unconfirmed match and erase its private object."""
    project = await _require_project(session, project_id)
    upload = await _require_upload(session, project.id, upload_id, for_update=True)
    if upload.document_file_id is not None or upload.status in {"parsing", "extracting", "ready"}:
        raise HTTPException(status_code=409, detail="a confirmed PDF cannot be rejected")
    # First make the payload inaccessible through the API, then delete it.
    # If the object-store call fails, a repeated DELETE/admin purge can safely
    # retry; the opposite order could leave an apparently usable DB row whose
    # only copy has already gone.
    upload.status = "rejected"
    upload.error_json = None
    await session.commit()
    store = await asyncio.to_thread(make_object_store, get_settings())
    await asyncio.to_thread(store.delete, upload.object_key)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/projects/{project_id}/library/pdf-uploads/{upload_id}/retry",
    response_model=LiteraturePdfUploadStartedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def retry_literature_pdf(
    project_id: str,
    upload_id: str,
    session: SessionDep,
    queue: QueueDep,
) -> LiteraturePdfUploadStartedResponse:
    """Retry a failed match or reparse an already-confirmed private PDF."""
    project = await _require_project(session, project_id)
    await require_queue(queue)
    upload = await _require_upload(session, project.id, upload_id, for_update=True)
    if upload.status == "match_failed":
        upload.status = "matching"
        function = "run_pdf_match_pipeline"
    elif upload.status == "parse_failed":
        if upload.document_file_id is None or upload.matched_work_id is None:
            raise HTTPException(status_code=409, detail="confirmed PDF binding is incomplete")
        upload.status = "parsing"
        function = "run_uploaded_pdf_pipeline"
    else:
        raise HTTPException(status_code=409, detail="PDF upload is not retryable")
    upload.error_json = None
    await session.flush()
    job = await _commit_and_enqueue_pdf_job(
        session,
        queue,
        project_id=project.id,
        function=function,
        upload=upload,
        enqueue_failure_status=(
            "match_failed" if function == "run_pdf_match_pipeline" else "parse_failed"
        ),
        upload_id=str(upload.id),
    )
    return LiteraturePdfUploadStartedResponse(
        upload=await _upload_response(session, upload),
        job=_job_response(job),
    )


@router.get("/projects/{project_id}/library/pdf-uploads/{upload_id}/download")
async def download_literature_pdf(
    project_id: str,
    upload_id: str,
    session: SessionDep,
) -> Response:
    project = await _require_project(session, project_id)
    upload = await _require_upload(session, project.id, upload_id)
    if upload.status == "rejected":
        raise HTTPException(status_code=410, detail="uploaded PDF was rejected")
    try:
        store = await asyncio.to_thread(make_object_store, get_settings())
        data = await asyncio.to_thread(store.get, upload.object_key)
    except (FileNotFoundError, ValueError) as error:
        raise HTTPException(status_code=410, detail="uploaded PDF payload is gone") from error
    except Exception as error:
        if getattr(error, "code", None) in {"NoSuchKey", "NoSuchObject"}:
            raise HTTPException(
                status_code=410,
                detail="uploaded PDF payload is gone",
            ) from error
        raise HTTPException(
            status_code=503,
            detail="private PDF storage is temporarily unavailable",
        ) from error
    return Response(
        content=data,
        media_type="application/pdf",
        headers={
            "Content-Disposition": (
                f"attachment; filename*=UTF-8''{quote(upload.filename, safe='')}"
            ),
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


async def _require_upload(
    session: AsyncSession,
    project_id: uuid.UUID,
    upload_id: str,
    *,
    for_update: bool = False,
) -> LiteraturePdfUpload:
    try:
        parsed_id = uuid.UUID(upload_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail="PDF upload not found") from error
    statement = select(LiteraturePdfUpload).where(
        LiteraturePdfUpload.id == parsed_id,
        LiteraturePdfUpload.project_id == project_id,
    )
    if for_update:
        statement = statement.with_for_update()
    upload = await session.scalar(statement)
    if upload is None:
        raise HTTPException(status_code=404, detail="PDF upload not found")
    return upload


async def _upload_response(
    session: AsyncSession,
    upload: LiteraturePdfUpload,
) -> LiteraturePdfUploadResponse:
    # ``updated_at`` is maintained by a server-side ON UPDATE expression and
    # SQLAlchemy expires it after flush. Refresh explicitly in async code so
    # response serialization never attempts implicit I/O (MissingGreenlet).
    await session.refresh(upload)
    matched_work: ScholarlyWorkResponse | None = None
    if upload.matched_work_id is not None:
        work = await session.get(ScholarlyWork, upload.matched_work_id)
        if work is not None:
            authors = await get_work_authors(session, work.id)
            matched_work = ScholarlyWorkResponse(
                id=str(work.id),
                canonical_title=work.canonical_title,
                authors=[item["author_name"] for item in authors],
                publication_year=work.publication_year,
                venue_name=work.venue_name,
                doi=work.doi,
                arxiv_id=work.arxiv_id,
                oa_status=work.oa_status,
                is_retracted=work.is_retracted,
                abstract=work.abstract,
                citation_count=work.citation_count,
            )
    extracted = upload.extracted_metadata_json
    return LiteraturePdfUploadResponse(
        id=str(upload.id),
        filename=upload.filename,
        status=upload.status,
        extracted_metadata=(
            PdfExtractedMetadataResponse(
                doi=extracted.get("doi"),
                title=extracted.get("title"),
                authors=list(extracted.get("authors") or []),
                publication_year=extracted.get("publication_year"),
            )
            if isinstance(extracted, dict)
            else None
        ),
        matched_work=matched_work,
        match_method=upload.match_method,
        match_confidence=upload.match_confidence,
        document_file_id=str(upload.document_file_id) if upload.document_file_id else None,
        error=upload.error_json,
        created_at=upload.created_at,
        updated_at=upload.updated_at,
    )


def _job_response(job: GenerationJob) -> JobResponse:
    return JobResponse(
        id=str(job.id),
        project_id=str(job.project_id),
        kind=job.kind,
        status=job.status,
        stage=job.stage,
        progress=job.progress,
        checkpoint=job.checkpoint_json,
        error=job.error_json,
        created_at=job.created_at,
        finished_at=job.finished_at,
    )


async def _commit_and_enqueue_pdf_job(
    session: AsyncSession,
    queue: ArqRedis | None,
    *,
    project_id: uuid.UUID,
    function: str,
    upload: LiteraturePdfUpload,
    enqueue_failure_status: str,
    upload_id: str,
) -> GenerationJob:
    """Commit upload state and its durable dispatch intent together.

    Queue transport failure retains queued work for the background dispatcher.
    The signature remains compatible with upload callers; no private object is
    deleted merely because Redis is temporarily unavailable.
    """
    from paperforge_api.dispatch import dispatch_after_commit
    from paperforge_api.jobs import start_job

    job = await start_job(
        session,
        queue,
        project_id=project_id,
        kind="ingest",
        function=function,
        upload_id=upload_id,
    )
    await session.commit()
    await dispatch_after_commit(session)
    return job


async def _lock_project_library(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> None:
    """Serialize admissions that allocate project-unique rows and cite keys."""
    lock_key = int.from_bytes(project_id.bytes[:8], byteorder="big", signed=True)
    await session.execute(select(func.pg_advisory_xact_lock(lock_key)))


def _safe_name(name: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in name)
    return cleaned.strip("-")[:200] or "paper.pdf"
