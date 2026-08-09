"""Project-private literature PDF upload persistence.

The raw object remains private while metadata matching is pending.  Only after
confirmation may a caller bind it to a project-private ``DocumentFile``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models.library import (
    PDF_UPLOAD_STATUSES,
    DocumentFile,
    LiteraturePdfUpload,
    ScholarlyWork,
)


class _UnsetType:
    __slots__ = ()


_UNSET = _UnsetType()


def _validate_object_metadata(
    *,
    object_key: str,
    filename: str,
    mime: str,
    bytes_count: int,
    content_hash: str,
) -> None:
    if not object_key.strip() or object_key.startswith("shared/"):
        raise ValueError("PDF upload object_key must identify a private object")
    if not filename.strip():
        raise ValueError("PDF upload filename cannot be empty")
    if mime.split(";", 1)[0].strip().lower() != "application/pdf":
        raise ValueError("literature PDF upload must use application/pdf")
    if bytes_count <= 0:
        raise ValueError("PDF upload must contain at least one byte")
    digest = content_hash.strip().lower()
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError("PDF upload content_hash must be a SHA-256 hex digest")


async def create_pdf_upload(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    object_key: str,
    filename: str,
    mime: str,
    bytes_count: int,
    content_hash: str,
    status: str = "matching",
) -> LiteraturePdfUpload:
    if status not in PDF_UPLOAD_STATUSES:
        raise ValueError(f"unsupported PDF upload status: {status}")
    _validate_object_metadata(
        object_key=object_key,
        filename=filename,
        mime=mime,
        bytes_count=bytes_count,
        content_hash=content_hash,
    )
    row = LiteraturePdfUpload(
        project_id=project_id,
        object_key=object_key.strip(),
        filename=filename.strip(),
        mime="application/pdf",
        bytes=bytes_count,
        content_hash=content_hash.strip().lower(),
        status=status,
    )
    session.add(row)
    await session.flush()
    return row


async def get_pdf_upload(
    session: AsyncSession,
    upload_id: uuid.UUID,
    *,
    project_id: uuid.UUID,
) -> LiteraturePdfUpload | None:
    """Fetch through the project boundary; cross-project IDs are indistinguishable."""
    return await session.scalar(
        select(LiteraturePdfUpload).where(
            LiteraturePdfUpload.id == upload_id,
            LiteraturePdfUpload.project_id == project_id,
        )
    )


async def list_pdf_uploads(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    status: str | None = None,
    limit: int = 200,
) -> list[LiteraturePdfUpload]:
    if status is not None and status not in PDF_UPLOAD_STATUSES:
        raise ValueError(f"unsupported PDF upload status: {status}")
    if limit <= 0:
        raise ValueError("PDF upload list limit must be positive")
    stmt = (
        select(LiteraturePdfUpload)
        .where(LiteraturePdfUpload.project_id == project_id)
        .order_by(LiteraturePdfUpload.created_at.desc(), LiteraturePdfUpload.id.desc())
        .limit(limit)
    )
    if status is not None:
        stmt = stmt.where(LiteraturePdfUpload.status == status)
    return list((await session.scalars(stmt)).all())


async def update_pdf_upload(
    session: AsyncSession,
    upload: LiteraturePdfUpload,
    *,
    status: str | None = None,
    extracted_metadata: dict[str, Any] | None | _UnsetType = _UNSET,
    match_candidates: list[dict[str, Any]] | None | _UnsetType = _UNSET,
    matched_work_id: uuid.UUID | None | _UnsetType = _UNSET,
    document_file_id: uuid.UUID | None | _UnsetType = _UNSET,
    match_method: str | None | _UnsetType = _UNSET,
    match_confidence: float | None | _UnsetType = _UNSET,
    match_metadata: dict[str, Any] | None | _UnsetType = _UNSET,
    error: dict[str, Any] | None | _UnsetType = _UNSET,
    confirmed_at: datetime | None | _UnsetType = _UNSET,
) -> LiteraturePdfUpload:
    if status is not None and status not in PDF_UPLOAD_STATUSES:
        raise ValueError(f"unsupported PDF upload status: {status}")

    resolved_work_id = (
        upload.matched_work_id if isinstance(matched_work_id, _UnsetType) else matched_work_id
    )
    if resolved_work_id is not None:
        if not isinstance(resolved_work_id, uuid.UUID):
            raise TypeError("matched_work_id must be a UUID or None")
        if await session.get(ScholarlyWork, resolved_work_id) is None:
            raise ValueError("matched scholarly work does not exist")

    resolved_document_id = (
        upload.document_file_id if isinstance(document_file_id, _UnsetType) else document_file_id
    )
    if resolved_document_id is not None:
        if not isinstance(resolved_document_id, uuid.UUID):
            raise TypeError("document_file_id must be a UUID or None")
        document = await session.get(DocumentFile, resolved_document_id)
        if (
            document is None
            or document.access_scope != "private"
            or document.project_id != upload.project_id
        ):
            raise ValueError("PDF upload document must be private to the same project")
        if resolved_work_id is None:
            raise ValueError("PDF upload document requires a matched scholarly work")
        if document.work_id != resolved_work_id:
            raise ValueError("PDF upload document and matched work must agree")
        if document.content_hash != upload.content_hash:
            raise ValueError("PDF upload document and uploaded content must agree")

    resolved_confidence = (
        upload.match_confidence if isinstance(match_confidence, _UnsetType) else match_confidence
    )
    if resolved_confidence is not None and not 0.0 <= float(resolved_confidence) <= 1.0:
        raise ValueError("PDF upload match_confidence must be between 0 and 1")

    # Apply only after all cross-record checks pass so a rejected update does not
    # leave a dirty in-memory object that could be flushed by unrelated work.
    if status is not None:
        upload.status = status
    if not isinstance(extracted_metadata, _UnsetType):
        upload.extracted_metadata_json = extracted_metadata
    if not isinstance(match_candidates, _UnsetType):
        upload.match_candidates_json = match_candidates
    if not isinstance(matched_work_id, _UnsetType):
        upload.matched_work_id = matched_work_id
    if not isinstance(document_file_id, _UnsetType):
        upload.document_file_id = document_file_id
    if not isinstance(match_method, _UnsetType):
        upload.match_method = match_method
    if not isinstance(match_confidence, _UnsetType):
        upload.match_confidence = float(match_confidence) if match_confidence is not None else None
    if not isinstance(match_metadata, _UnsetType):
        upload.match_metadata_json = match_metadata
    if not isinstance(error, _UnsetType):
        upload.error_json = error
    if not isinstance(confirmed_at, _UnsetType):
        upload.confirmed_at = confirmed_at
    await session.flush()
    return upload


# Explicit domain names are the public API; the shorter aliases above remain
# convenient for callers already written against the initial repository draft.
create_literature_pdf_upload = create_pdf_upload
get_literature_pdf_upload = get_pdf_upload
list_literature_pdf_uploads = list_pdf_uploads
update_literature_pdf_upload = update_pdf_upload


__all__ = [
    "create_pdf_upload",
    "create_literature_pdf_upload",
    "get_pdf_upload",
    "get_literature_pdf_upload",
    "list_pdf_uploads",
    "list_literature_pdf_uploads",
    "update_pdf_upload",
    "update_literature_pdf_upload",
]
