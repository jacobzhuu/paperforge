from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime

import pytest
from db import (
    create_literature_pdf_upload,
    create_project,
    create_user,
    get_literature_pdf_upload,
    list_literature_pdf_uploads,
    update_literature_pdf_upload,
)
from db.models.library import DocumentFile, ScholarlyWork
from sqlalchemy.exc import IntegrityError

pytestmark = pytest.mark.asyncio


async def _project(session, label: str):
    owner = await create_user(
        session,
        email=f"{label}-{uuid.uuid4()}@example.test",
        password_hash="!test-only",
        verified=True,
    )
    return await create_project(
        session,
        title=f"{label} project",
        paper_type="review",
        owner_id=owner.id,
    )


async def _work(session, title: str = "Private PDF study") -> ScholarlyWork:
    row = ScholarlyWork(canonical_title=title)
    session.add(row)
    await session.flush()
    return row


async def test_pdf_upload_repository_is_project_scoped_and_tracks_confirmation(session):
    project = await _project(session, "owner")
    other_project = await _project(session, "other")
    digest = hashlib.sha256(b"%PDF-private").hexdigest()
    upload = await create_literature_pdf_upload(
        session,
        project_id=project.id,
        object_key=f"users/{project.owner_id}/projects/{project.id}/literature/paper.pdf",
        filename="paper.pdf",
        mime="application/pdf",
        bytes_count=12,
        content_hash=digest,
    )

    assert upload.status == "matching"
    assert (
        await get_literature_pdf_upload(
            session,
            upload.id,
            project_id=other_project.id,
        )
        is None
    )
    assert await list_literature_pdf_uploads(session, project.id) == [upload]

    work = await _work(session)
    await update_literature_pdf_upload(
        session,
        upload,
        status="needs_confirmation",
        extracted_metadata={
            "title": work.canonical_title,
            "doi": "10.1000/private",
        },
        match_candidates=[{"work_id": str(work.id), "confidence": 1.0}],
        matched_work_id=work.id,
        match_method="doi_crossref",
        match_confidence=1.0,
        match_metadata={"provider": "crossref"},
        error=None,
    )
    assert upload.extracted_metadata_json["doi"] == "10.1000/private"
    assert upload.matched_work_id == work.id

    document = DocumentFile(
        project_id=project.id,
        access_scope="private",
        work_id=work.id,
        kind="uploaded_pdf",
        object_key=upload.object_key,
        mime=upload.mime,
        bytes=upload.bytes,
        content_hash=upload.content_hash,
    )
    session.add(document)
    await session.flush()
    confirmed_at = datetime.now(UTC)
    await update_literature_pdf_upload(
        session,
        upload,
        status="parsing",
        document_file_id=document.id,
        confirmed_at=confirmed_at,
    )

    assert upload.document_file_id == document.id
    assert upload.confirmed_at == confirmed_at
    assert await list_literature_pdf_uploads(
        session,
        project.id,
        status="parsing",
    ) == [upload]


async def test_pdf_upload_repository_rejects_invalid_metadata_and_cross_project_document(
    session,
):
    project = await _project(session, "owner")
    other_project = await _project(session, "other")
    with pytest.raises(ValueError, match="private object"):
        await create_literature_pdf_upload(
            session,
            project_id=project.id,
            object_key="shared/oa/not-private.pdf",
            filename="paper.pdf",
            mime="application/pdf",
            bytes_count=10,
            content_hash="a" * 64,
        )

    upload = await create_literature_pdf_upload(
        session,
        project_id=project.id,
        object_key=f"users/{project.owner_id}/projects/{project.id}/literature/paper.pdf",
        filename="paper.pdf",
        mime="application/pdf",
        bytes_count=10,
        content_hash="b" * 64,
    )
    work = await _work(session)
    foreign_document = DocumentFile(
        project_id=other_project.id,
        access_scope="private",
        work_id=work.id,
        kind="uploaded_pdf",
        object_key="users/foreign/projects/foreign/literature/paper.pdf",
        mime="application/pdf",
        bytes=10,
        content_hash="b" * 64,
    )
    session.add(foreign_document)
    await session.flush()

    with pytest.raises(ValueError, match="same project"):
        await update_literature_pdf_upload(
            session,
            upload,
            matched_work_id=work.id,
            document_file_id=foreign_document.id,
        )
    with pytest.raises(ValueError, match="unsupported PDF upload status"):
        await update_literature_pdf_upload(session, upload, status="confirmed")
    with pytest.raises(ValueError, match="between 0 and 1"):
        await update_literature_pdf_upload(session, upload, match_confidence=1.1)
    assert upload.status == "matching"
    assert upload.matched_work_id is None
    assert upload.document_file_id is None
    with pytest.raises(ValueError, match="unsupported PDF upload status"):
        await list_literature_pdf_uploads(session, project.id, status="uploaded")


async def test_pdf_upload_can_be_retried_with_the_same_project_content(session):
    project = await _project(session, "retry")
    common = {
        "project_id": project.id,
        "filename": "paper.pdf",
        "mime": "application/pdf",
        "bytes_count": 10,
        "content_hash": "e" * 64,
    }
    rejected = await create_literature_pdf_upload(
        session,
        object_key=f"users/{project.owner_id}/projects/{project.id}/literature/rejected.pdf",
        status="rejected",
        **common,
    )
    retried = await create_literature_pdf_upload(
        session,
        object_key=f"users/{project.owner_id}/projects/{project.id}/literature/retry.pdf",
        **common,
    )

    assert retried.id != rejected.id
    uploads = await list_literature_pdf_uploads(session, project.id)
    assert {upload.id for upload in uploads} == {rejected.id, retried.id}


async def test_document_file_partial_uniqueness_isolated_by_access_scope_and_project(
    session,
):
    first_project = await _project(session, "first")
    second_project = await _project(session, "second")
    work = await _work(session)
    common = {
        "work_id": work.id,
        "kind": "uploaded_pdf",
        "mime": "application/pdf",
        "bytes": 10,
        "content_hash": "c" * 64,
    }
    session.add_all(
        [
            DocumentFile(
                project_id=None,
                access_scope="shared",
                object_key="shared/oa/works/work/fulltext.bin",
                **common,
            ),
            DocumentFile(
                project_id=first_project.id,
                access_scope="private",
                object_key="users/first/projects/first/literature/paper.pdf",
                **common,
            ),
            DocumentFile(
                project_id=second_project.id,
                access_scope="private",
                object_key="users/second/projects/second/literature/paper.pdf",
                **common,
            ),
        ]
    )
    await session.flush()


async def test_document_file_rejects_duplicate_private_content_in_one_project(session):
    project = await _project(session, "owner")
    work = await _work(session)
    common = {
        "project_id": project.id,
        "access_scope": "private",
        "work_id": work.id,
        "kind": "uploaded_pdf",
        "mime": "application/pdf",
        "bytes": 10,
        "content_hash": "d" * 64,
    }
    session.add(
        DocumentFile(
            object_key="users/owner/projects/project/literature/first.pdf",
            **common,
        )
    )
    await session.flush()
    session.add(
        DocumentFile(
            object_key="users/owner/projects/project/literature/second.pdf",
            **common,
        )
    )
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()
