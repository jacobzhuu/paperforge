"""Project-private literature PDF matching and lifecycle helpers."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from dataclasses import dataclass, field
from typing import Any

from db import resolve_canonical_work, upsert_work
from db.models.library import (
    PDF_UPLOAD_STATUSES,
    DocumentFile,
    LiteraturePdfUpload,
    ScholarlyWork,
    WorkAuthor,
)
from ingest import PdfBibliographicMetadata, extract_pdf_bibliographic_metadata
from scholar_gateway import (
    VerificationRequest,
    normalize_doi,
    normalized_title_hash,
    verify_reference,
)
from sqlalchemy import select
from storage import make_object_store

from paperforge_worker.context import JobContext

PDF_VISIBILITY_TIMEOUT_SECONDS = 2.0
PDF_VISIBILITY_POLL_SECONDS = 0.1


@dataclass
class PdfMatchOutcome:
    upload_id: str
    status: str = "matching"
    matched_work_id: str | None = None
    match_method: str | None = None
    match_confidence: float | None = None
    extracted_metadata: dict[str, Any] | None = None
    candidates: list[dict[str, Any]] = field(default_factory=list)
    failure_reason: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "upload_id": self.upload_id,
            "status": self.status,
            "matched_work_id": self.matched_work_id,
            "match_method": self.match_method,
            "match_confidence": self.match_confidence,
            "extracted_metadata": self.extracted_metadata,
            "candidates": self.candidates,
            "failure_reason": self.failure_reason,
        }


@dataclass(frozen=True)
class UploadedPdfTarget:
    upload_id: uuid.UUID
    document_file_id: uuid.UUID
    work_id: uuid.UUID
    already_ready: bool = False


async def wait_for_pdf_upload_visibility(
    context: JobContext,
    upload_id: Any,
) -> None:
    """Wait until the API transaction that enqueued the match job is visible."""
    await _load_upload(context, _as_uuid(upload_id))


async def match_uploaded_pdf(
    context: JobContext,
    upload_id: Any,
) -> PdfMatchOutcome:
    """Extract untrusted clues, prefer a local exact match, then verify remotely."""
    upload_uuid = _as_uuid(upload_id)
    upload = await _load_upload(context, upload_uuid)
    outcome = PdfMatchOutcome(upload_id=str(upload_uuid), status=upload.status)
    if upload.status == "needs_confirmation" and upload.matched_work_id is not None:
        return _outcome_from_upload(upload)
    if upload.status not in {"matching", "match_failed"}:
        raise ValueError(f"pdf_upload_not_matchable:{upload.status}")

    await set_pdf_upload_status(context, upload_uuid, "matching", error=None)
    try:
        content = await asyncio.to_thread(
            make_object_store(context.settings).get,
            upload.object_key,
        )
        integrity_error = _integrity_error(
            content,
            expected_bytes=upload.bytes,
            expected_hash=upload.content_hash,
        )
        if integrity_error:
            raise ValueError(integrity_error)
        metadata = await asyncio.to_thread(
            extract_pdf_bibliographic_metadata,
            content,
        )
        metadata_payload = metadata.to_payload()
        await _store_extracted_metadata(context, upload_uuid, metadata_payload)

        local_work, local_method, local_confidence, local_candidates = await _find_local_match(
            context, metadata
        )
        if local_work is not None:
            return await _persist_match(
                context,
                upload_uuid,
                work=local_work,
                metadata=metadata_payload,
                method=local_method or "local_exact",
                confidence=local_confidence or 1.0,
                candidates=local_candidates,
                match_metadata={"source": "local_library"},
            )

        request = VerificationRequest(
            doi=metadata.doi,
            title=metadata.title,
            authors=metadata.authors,
            publication_year=metadata.publication_year,
            source_label="pdf_upload",
        )
        result = await asyncio.to_thread(
            verify_reference,
            request,
            client=context.http_client,
            config=context.settings.provider_config("crossref"),
            cache=context.scholar_cache,
        )
        if not result.verified or result.candidate is None:
            reason = result.failure_reason or "verification_failed"
            await _persist_match_failure(
                context,
                upload_uuid,
                reason=reason,
                metadata=metadata_payload,
                candidates=local_candidates,
                diagnostics=result.diagnostics,
            )
            outcome.status = "match_failed"
            outcome.extracted_metadata = metadata_payload
            outcome.candidates = local_candidates
            outcome.failure_reason = reason
            return outcome

        async with context.session() as session:
            work, _created = await upsert_work(session, result.candidate)
            work = await resolve_canonical_work(session, work)
        candidate_payload = _work_payload(
            work,
            method=result.matched_by or "provider_verified",
            confidence=result.confidence or 1.0,
        )
        return await _persist_match(
            context,
            upload_uuid,
            work=work,
            metadata=metadata_payload,
            method=result.matched_by or "provider_verified",
            confidence=result.confidence or 1.0,
            candidates=[*local_candidates, candidate_payload],
            match_metadata={
                "source": "provider_verification",
                "provider": result.candidate.provider_name,
                "provider_record_id": result.candidate.provider_record_id,
                "diagnostics": result.diagnostics,
            },
        )
    except Exception as error:  # noqa: BLE001 - persist a user-visible match failure
        reason = str(error)[:500] or type(error).__name__
        await _persist_match_failure(
            context,
            upload_uuid,
            reason=reason,
            metadata=outcome.extracted_metadata,
            candidates=outcome.candidates,
            diagnostics={"error_type": type(error).__name__},
        )
        raise


async def prepare_uploaded_pdf(
    context: JobContext,
    upload_id: Any,
) -> UploadedPdfTarget:
    """Resolve and authorize the confirmed upload and its bound private document."""
    upload_uuid = _as_uuid(upload_id)
    deadline = asyncio.get_running_loop().time() + PDF_VISIBILITY_TIMEOUT_SECONDS
    last_error = "pdf_upload_not_found"
    while True:
        async with context.session() as session:
            upload = await session.scalar(
                select(LiteraturePdfUpload).where(
                    LiteraturePdfUpload.id == upload_uuid,
                    LiteraturePdfUpload.project_id == context.project_id,
                )
            )
            if upload is None:
                last_error = "pdf_upload_not_found"
            elif upload.status == "needs_confirmation":
                # start_job is currently called before the confirming API
                # transaction commits, so a fast worker can see the previous
                # committed state for a few milliseconds.
                last_error = "pdf_upload_not_confirmed:needs_confirmation"
            elif upload.status == "ready":
                if upload.document_file_id is None or upload.matched_work_id is None:
                    last_error = "ready_pdf_upload_missing_binding"
                else:
                    return UploadedPdfTarget(
                        upload_id=upload.id,
                        document_file_id=upload.document_file_id,
                        work_id=upload.matched_work_id,
                        already_ready=True,
                    )
            elif upload.status not in {"parsing", "extracting", "parse_failed"}:
                raise ValueError(f"pdf_upload_not_confirmed:{upload.status}")
            elif upload.document_file_id is None or upload.matched_work_id is None:
                last_error = "pdf_upload_missing_binding"
            else:
                document = await session.scalar(
                    select(DocumentFile).where(
                        DocumentFile.id == upload.document_file_id,
                        DocumentFile.project_id == context.project_id,
                        DocumentFile.access_scope == "private",
                        DocumentFile.work_id == upload.matched_work_id,
                    )
                )
                if document is not None:
                    return UploadedPdfTarget(
                        upload_id=upload.id,
                        document_file_id=document.id,
                        work_id=document.work_id,
                    )
                last_error = "pdf_document_not_found_or_not_authorized"
        if not await _wait_for_visibility_poll(deadline):
            raise ValueError(last_error)


async def set_pdf_upload_status(
    context: JobContext,
    upload_id: Any,
    status: str,
    *,
    error: dict[str, Any] | None = None,
) -> None:
    if status not in PDF_UPLOAD_STATUSES:
        raise ValueError(f"unsupported_pdf_upload_status:{status}")
    async with context.session() as session:
        upload = await session.scalar(
            select(LiteraturePdfUpload)
            .where(
                LiteraturePdfUpload.id == _as_uuid(upload_id),
                LiteraturePdfUpload.project_id == context.project_id,
            )
            .with_for_update()
        )
        if upload is None:
            raise ValueError("pdf_upload_not_found")
        if upload.status == "rejected" and status != "rejected":
            raise ValueError("pdf_upload_rejected")
        if upload.status == "ready" and status != "ready":
            # Concurrent duplicate jobs may finish out of order.  Once the
            # private PDF is usable, a stale parser cannot move it backwards.
            return
        upload.status = status
        upload.error_json = error


async def _load_upload(
    context: JobContext,
    upload_id: uuid.UUID,
) -> LiteraturePdfUpload:
    deadline = asyncio.get_running_loop().time() + PDF_VISIBILITY_TIMEOUT_SECONDS
    while True:
        async with context.session() as session:
            upload = await session.scalar(
                select(LiteraturePdfUpload).where(
                    LiteraturePdfUpload.id == upload_id,
                    LiteraturePdfUpload.project_id == context.project_id,
                )
            )
            if upload is not None:
                # Materialize all values before the committing session expires.
                _ = (
                    upload.status,
                    upload.matched_work_id,
                    upload.object_key,
                    upload.bytes,
                    upload.content_hash,
                )
                return upload
        if not await _wait_for_visibility_poll(deadline):
            raise ValueError("pdf_upload_not_found")


async def _wait_for_visibility_poll(deadline: float) -> bool:
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        return False
    await asyncio.sleep(min(PDF_VISIBILITY_POLL_SECONDS, remaining))
    return True


async def _store_extracted_metadata(
    context: JobContext,
    upload_id: uuid.UUID,
    metadata: dict[str, Any],
) -> None:
    async with context.session() as session:
        upload = await session.scalar(
            select(LiteraturePdfUpload)
            .where(
                LiteraturePdfUpload.id == upload_id,
                LiteraturePdfUpload.project_id == context.project_id,
            )
            .with_for_update()
        )
        if upload is None:
            raise ValueError("pdf_upload_not_found")
        if upload.status == "rejected":
            raise ValueError("pdf_upload_rejected")
        if upload.status not in {"matching", "match_failed"}:
            return
        upload.extracted_metadata_json = metadata


async def _find_local_match(
    context: JobContext,
    metadata: PdfBibliographicMetadata,
) -> tuple[ScholarlyWork | None, str | None, float | None, list[dict[str, Any]]]:
    async with context.session() as session:
        doi = normalize_doi(metadata.doi)
        if doi:
            work = await session.scalar(
                select(ScholarlyWork)
                .where(ScholarlyWork.doi == doi)
                .order_by(ScholarlyWork.created_at, ScholarlyWork.id)
                .limit(1)
            )
            if work is not None:
                work = await resolve_canonical_work(session, work)
                return (
                    work,
                    "local_doi_exact",
                    1.0,
                    [_work_payload(work, method="local_doi_exact", confidence=1.0)],
                )
            # A strong identifier must be verified as such.  Do not silently
            # bind by title when a PDF supplied a conflicting DOI.
            return None, None, None, []

        title_hash = normalized_title_hash(metadata.title)
        if not title_hash:
            return None, None, None, []
        works = list(
            (
                await session.scalars(
                    select(ScholarlyWork)
                    .where(ScholarlyWork.normalized_title_hash == title_hash)
                    .order_by(ScholarlyWork.created_at, ScholarlyWork.id)
                )
            ).all()
        )
        candidates = [
            _work_payload(work, method="local_title_exact", confidence=0.95) for work in works
        ]
        if not works:
            return None, None, None, candidates
        chosen = await _choose_title_candidate(session, works, metadata)
        if chosen is None:
            return None, None, None, candidates
        chosen = await resolve_canonical_work(session, chosen)
        confidence = _title_match_confidence(chosen, metadata)
        return chosen, "local_title_exact", confidence, candidates


async def _choose_title_candidate(
    session: Any,
    works: list[ScholarlyWork],
    metadata: PdfBibliographicMetadata,
) -> ScholarlyWork | None:
    if len(works) == 1:
        work = works[0]
        if (
            metadata.publication_year
            and work.publication_year
            and abs(metadata.publication_year - work.publication_year) > 1
        ):
            return None
        return work

    wanted_surnames = {_surname(name) for name in metadata.authors if _surname(name)}
    ranked: list[tuple[int, ScholarlyWork]] = []
    for work in works:
        score = 0
        if metadata.publication_year and work.publication_year:
            if abs(metadata.publication_year - work.publication_year) > 1:
                continue
            score += 1
        if wanted_surnames:
            authors = list(
                (
                    await session.scalars(select(WorkAuthor).where(WorkAuthor.work_id == work.id))
                ).all()
            )
            have = {_surname(author.author_name) for author in authors}
            if have and not (wanted_surnames & have):
                continue
            if wanted_surnames & have:
                score += 2
        ranked.append((score, work))
    if not ranked:
        return None
    ranked.sort(key=lambda item: item[0], reverse=True)
    if len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
        return None
    return ranked[0][1]


async def _persist_match(
    context: JobContext,
    upload_id: uuid.UUID,
    *,
    work: ScholarlyWork,
    metadata: dict[str, Any],
    method: str,
    confidence: float,
    candidates: list[dict[str, Any]],
    match_metadata: dict[str, Any],
) -> PdfMatchOutcome:
    async with context.session() as session:
        upload = await session.scalar(
            select(LiteraturePdfUpload)
            .where(
                LiteraturePdfUpload.id == upload_id,
                LiteraturePdfUpload.project_id == context.project_id,
            )
            .with_for_update()
        )
        if upload is None:
            raise ValueError("pdf_upload_not_found")
        if upload.status == "rejected":
            raise ValueError("pdf_upload_rejected")
        if upload.status == "needs_confirmation" and upload.matched_work_id is not None:
            return _outcome_from_upload(upload)
        if upload.status not in {"matching", "match_failed"}:
            # Another match worker may have finished first and the user may
            # already have confirmed it.  A stale worker must never regress a
            # parsing/ready upload back to confirmation.
            return _outcome_from_upload(upload)
        upload.status = "needs_confirmation"
        upload.extracted_metadata_json = metadata
        upload.match_candidates_json = candidates
        upload.matched_work_id = work.id
        upload.match_method = method
        upload.match_confidence = confidence
        upload.match_metadata_json = match_metadata
        upload.error_json = None
    return PdfMatchOutcome(
        upload_id=str(upload_id),
        status="needs_confirmation",
        matched_work_id=str(work.id),
        match_method=method,
        match_confidence=confidence,
        extracted_metadata=metadata,
        candidates=candidates,
    )


async def _persist_match_failure(
    context: JobContext,
    upload_id: uuid.UUID,
    *,
    reason: str,
    metadata: dict[str, Any] | None,
    candidates: list[dict[str, Any]],
    diagnostics: dict[str, Any],
) -> None:
    async with context.session() as session:
        upload = await session.scalar(
            select(LiteraturePdfUpload)
            .where(
                LiteraturePdfUpload.id == upload_id,
                LiteraturePdfUpload.project_id == context.project_id,
            )
            .with_for_update()
        )
        if upload is None or upload.status == "rejected":
            return
        # A duplicate/stale worker must not regress a match already shown to or
        # confirmed by the user.
        if upload.status not in {"matching", "match_failed"}:
            return
        upload.status = "match_failed"
        if metadata is not None:
            upload.extracted_metadata_json = metadata
        if candidates:
            upload.match_candidates_json = candidates
        upload.error_json = {
            "reason": str(reason)[:500],
            "diagnostics": diagnostics,
        }


def _outcome_from_upload(upload: LiteraturePdfUpload) -> PdfMatchOutcome:
    return PdfMatchOutcome(
        upload_id=str(upload.id),
        status=upload.status,
        matched_work_id=str(upload.matched_work_id) if upload.matched_work_id else None,
        match_method=upload.match_method,
        match_confidence=upload.match_confidence,
        extracted_metadata=dict(upload.extracted_metadata_json or {}),
        candidates=list(upload.match_candidates_json or []),
        failure_reason=(upload.error_json or {}).get("reason"),
    )


def _work_payload(
    work: ScholarlyWork,
    *,
    method: str,
    confidence: float,
) -> dict[str, Any]:
    return {
        "work_id": str(work.id),
        "title": work.canonical_title,
        "doi": work.doi,
        "publication_year": work.publication_year,
        "method": method,
        "confidence": round(float(confidence), 4),
    }


def _title_match_confidence(
    work: ScholarlyWork,
    metadata: PdfBibliographicMetadata,
) -> float:
    if (
        metadata.publication_year
        and work.publication_year
        and abs(metadata.publication_year - work.publication_year) <= 1
    ):
        return 0.98
    return 0.95


def _integrity_error(
    content: bytes,
    *,
    expected_bytes: int | None,
    expected_hash: str | None,
) -> str | None:
    if expected_bytes is not None and len(content) != expected_bytes:
        return "pdf_size_mismatch"
    if expected_hash:
        digest = hashlib.sha256(content).hexdigest()
        if digest.casefold() != expected_hash.casefold():
            return "pdf_hash_mismatch"
    return None


def _surname(name: str) -> str:
    text = (name or "").strip()
    if not text:
        return ""
    value = text.split(",", 1)[0] if "," in text else text.split()[-1]
    return "".join(character for character in value.casefold() if character.isalpha())


def _as_uuid(value: Any) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
