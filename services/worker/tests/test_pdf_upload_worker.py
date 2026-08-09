"""Worker contracts for private scholarly-PDF ingestion."""

from __future__ import annotations

import asyncio
import hashlib
import io
import uuid
import zlib
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from db import (
    create_document,
    create_project,
    create_user,
    list_evidence_units,
    replace_citation_usage,
    upsert_card,
    upsert_entry,
    upsert_section,
)
from db.models.library import (
    DocumentChunk,
    DocumentFile,
    DocumentParse,
    EvidenceUnit,
    LibraryEntry,
    LiteratureCard,
    LiteraturePdfUpload,
    ScholarlyWork,
)
from paperforge_worker.config import WorkerSettings
from paperforge_worker.context import JobContext
from paperforge_worker.pipelines.cards import (
    _find_card_cache,
    card_source_hash,
    generate_cards,
)
from paperforge_worker.pipelines.evidence import extract_evidence_units
from paperforge_worker.pipelines.fulltext import (
    PARSER_VERSION,
    load_persisted_fulltext_sources,
)
from paperforge_worker.pipelines.pdf_upload import (
    _load_upload,
    _persist_match_failure,
    match_uploaded_pdf,
    prepare_uploaded_pdf,
)
from paperforge_worker.worker import (
    _quality,
    _unused_core_literature,
    run_uploaded_pdf_pipeline,
)
from pypdf import PdfWriter
from scholar_gateway import InMemoryHttpCache, normalized_title_hash
from sqlalchemy import select
from storage import make_object_store


async def _seed_projects(session_factory):
    async with session_factory() as session:
        owner = await create_user(
            session,
            email=f"{uuid.uuid4()}@example.test",
            password_hash="!test-only",
            verified=True,
        )
        project_a = await create_project(
            session,
            title="Project A",
            paper_type="review",
            owner_id=owner.id,
        )
        project_b = await create_project(
            session,
            title="Project B",
            paper_type="review",
            owner_id=owner.id,
        )
        work = ScholarlyWork(
            canonical_title="Private PDF Evidence",
            normalized_title_hash=normalized_title_hash("Private PDF Evidence"),
            doi="10.5555/private-pdf",
            is_retracted=False,
        )
        session.add(work)
        await session.flush()
        await session.commit()
        return owner.id, project_a.id, project_b.id, work.id


def _context(project_id, session_factory, tmp_path) -> JobContext:
    return JobContext(
        project_id=project_id,
        job_id=None,
        settings=WorkerSettings(
            _env_file=None,
            storage_backend="filesystem",
            storage_fs_root=str(tmp_path / "objects"),
            llm_default_provider="noop",
        ),
        session_factory=session_factory,
        http_client=httpx.Client(),
        scholar_cache=InMemoryHttpCache(),
    )


def _minimal_pdf(lines: list[str]) -> bytes:
    text_ops = "\n".join(f"({line}) Tj" for line in lines)
    stream = f"BT\n/F1 12 Tf\n{text_ops}\nET".encode()
    compressed = zlib.compress(stream)
    return (
        b"%PDF-1.4\n"
        b"1 0 obj\n<< /Type /Catalog >>\nendobj\n"
        b"2 0 obj\n<< /Length "
        + str(len(compressed)).encode()
        + b" /Filter /FlateDecode >>\nstream\n"
        + compressed
        + b"\nendstream\nendobj\n"
        b"trailer\n<< /Root 1 0 R >>\n%%EOF\n"
    )


async def test_fulltext_loader_enforces_project_acl_and_prefers_private_source(
    session_factory,
    tmp_path,
) -> None:
    _owner_id, project_a, project_b, work_id = await _seed_projects(session_factory)
    async with session_factory() as session:
        shared = DocumentFile(
            project_id=None,
            work_id=work_id,
            access_scope="shared",
            kind="oa_pdf",
            object_key="shared.pdf",
            mime="application/pdf",
            bytes=10,
            content_hash="a" * 64,
        )
        private = DocumentFile(
            project_id=project_a,
            work_id=work_id,
            access_scope="private",
            kind="uploaded_pdf",
            object_key="private.pdf",
            mime="application/pdf",
            bytes=10,
            content_hash="b" * 64,
        )
        session.add_all([shared, private])
        await session.flush()
        session.add_all(
            [
                DocumentParse(
                    document_file_id=shared.id,
                    parser_version=PARSER_VERSION,
                    status="parsed",
                    extracted_text="public full text",
                ),
                DocumentParse(
                    document_file_id=private.id,
                    parser_version=PARSER_VERSION,
                    status="parsed",
                    extracted_text="PROJECT A SECRET",
                ),
            ]
        )
        await session.commit()

    source_a = (
        await load_persisted_fulltext_sources(
            _context(project_a, session_factory, tmp_path),
            [work_id],
        )
    )[str(work_id)]
    source_b = (
        await load_persisted_fulltext_sources(
            _context(project_b, session_factory, tmp_path),
            [work_id],
        )
    )[str(work_id)]
    assert source_a.text == "PROJECT A SECRET"
    assert source_a.is_private
    assert source_b.text == "public full text"
    assert not source_b.is_private


async def test_persisted_chunks_restore_page_and_section_markers(
    session_factory,
    tmp_path,
) -> None:
    _owner_id, project_a, _project_b, work_id = await _seed_projects(session_factory)
    passage = (
        "Results show accuracy improved from 71 percent to 84 percent "
        "on the held-out evaluation dataset."
    )
    async with session_factory() as session:
        document = DocumentFile(
            project_id=project_a,
            work_id=work_id,
            access_scope="private",
            kind="uploaded_pdf",
            object_key="located-private.pdf",
            mime="application/pdf",
            bytes=10,
            content_hash="e" * 64,
        )
        session.add(document)
        await session.flush()
        parsed = DocumentParse(
            document_file_id=document.id,
            parser_version=PARSER_VERSION,
            status="parsed",
            extracted_text=passage,
            metadata_json={
                "structure_segments": [
                    {
                        "char_start": 0,
                        "char_end": len(passage),
                        "page_number": 3,
                        "page_locator_reliable": True,
                        "section_title": "Results",
                    }
                ]
            },
        )
        session.add(parsed)
        await session.flush()
        session.add(
            DocumentChunk(
                document_parse_id=parsed.id,
                chunk_no=0,
                text=passage,
                content_role="scientific_prose",
                char_start=0,
                char_end=len(passage),
                metadata_json={"char_start": 0, "char_end": len(passage)},
            )
        )
        await session.commit()

    source = (
        await load_persisted_fulltext_sources(
            _context(project_a, session_factory, tmp_path),
            [work_id],
        )
    )[str(work_id)]
    assert "[[PAGE=3 | SECTION=Results]]" in source.text
    assert passage in source.text


async def test_private_card_cache_never_reads_another_project(
    session_factory,
    tmp_path,
) -> None:
    _owner_id, project_a, project_b, work_id = await _seed_projects(session_factory)
    async with session_factory() as session:
        await upsert_card(
            session,
            project_id=project_a,
            work_id=work_id,
            summary="PROJECT A SECRET CARD",
            source_hash="same-private-source",
        )
        await session.commit()
    async with session_factory() as session:
        private_hit = await _find_card_cache(
            session,
            project_id=project_b,
            work_id=work_id,
            source_hash="same-private-source",
            private_source=True,
        )
        shared_hit = await _find_card_cache(
            session,
            project_id=project_b,
            work_id=work_id,
            source_hash="same-private-source",
            private_source=False,
        )
    assert private_hit is None
    assert shared_hit is not None
    assert shared_hit.summary == "PROJECT A SECRET CARD"


async def test_private_persisted_source_overrides_transient_oa_for_cards(
    session_factory,
    tmp_path,
) -> None:
    _owner_id, project_a, project_b, work_id = await _seed_projects(session_factory)
    async with session_factory() as session:
        entry, _ = await upsert_entry(
            session,
            project_id=project_a,
            work_id=work_id,
            added_via="search",
            status="selected",
            verified=True,
        )
        entry.bibtex_key = "private2026source"
        document = DocumentFile(
            project_id=project_a,
            work_id=work_id,
            access_scope="private",
            kind="uploaded_pdf",
            object_key="preferred-private.pdf",
            mime="application/pdf",
            bytes=10,
            content_hash="d" * 64,
        )
        session.add(document)
        await session.flush()
        session.add(
            DocumentParse(
                document_file_id=document.id,
                parser_version=PARSER_VERSION,
                status="parsed",
                extracted_text="PREFERRED PRIVATE FULL TEXT",
            )
        )
        await session.commit()

    await generate_cards(
        _context(project_a, session_factory, tmp_path),
        fulltexts={str(work_id): "TRANSIENT OA TEXT"},
        work_ids=[work_id],
    )
    async with session_factory() as session:
        card = await session.scalar(
            select(LiteratureCard).where(
                LiteratureCard.project_id == project_a,
                LiteratureCard.work_id == work_id,
            )
        )
        work = await session.get(ScholarlyWork, work_id)
    assert card is not None and work is not None
    assert card.source_hash == card_source_hash(
        title=work.canonical_title,
        abstract=work.abstract,
        fulltext="PREFERRED PRIVATE FULL TEXT",
    )


async def test_card_generation_uses_configured_bounded_concurrency(
    session_factory,
    tmp_path,
    monkeypatch,
) -> None:
    _owner_id, project_id, _other_project, _work_id = await _seed_projects(session_factory)
    async with session_factory() as session:
        for index in range(7):
            title = f"Concurrent Card Work {index}"
            work = ScholarlyWork(
                canonical_title=title,
                normalized_title_hash=normalized_title_hash(title),
                abstract=f"Abstract evidence for work {index}.",
                is_retracted=False,
            )
            session.add(work)
            await session.flush()
            await upsert_entry(
                session,
                project_id=project_id,
                work_id=work.id,
                added_via="search",
                status="selected",
                verified=True,
            )
        await session.commit()

    active = 0
    peak = 0

    async def fake_extract_card(**_kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return (
            {
                "summary": "bounded",
                "contributions": [],
                "methods": [],
                "results": [],
                "limitations": [],
                "quotable_points": [],
                "extraction_model": "test",
            },
            False,
        )

    monkeypatch.setattr(
        "paperforge_worker.pipelines.cards.extract_card",
        fake_extract_card,
    )
    context = _context(project_id, session_factory, tmp_path)
    context.settings.card_concurrency = 3
    outcome = await generate_cards(context)

    assert outcome.requested == 7
    assert outcome.generated == 7
    assert peak == 3


async def test_private_pdf_derives_only_project_scoped_evidence(
    session_factory,
    tmp_path,
) -> None:
    _owner_id, project_a, project_b, work_id = await _seed_projects(session_factory)
    async with session_factory() as session:
        for project_id in (project_a, project_b):
            entry, _ = await upsert_entry(
                session,
                project_id=project_id,
                work_id=work_id,
                added_via="search",
                status="selected",
                verified=True,
            )
            entry.bibtex_key = f"private{str(project_id)[:6]}"
        document = DocumentFile(
            project_id=project_a,
            work_id=work_id,
            access_scope="private",
            kind="uploaded_pdf",
            object_key="project-a.pdf",
            mime="application/pdf",
            bytes=10,
            content_hash="c" * 64,
        )
        session.add(document)
        await session.flush()
        session.add(
            DocumentParse(
                document_file_id=document.id,
                parser_version=PARSER_VERSION,
                status="parsed",
                extracted_text="Results show that the intervention improved outcomes.",
            )
        )
        await upsert_card(
            session,
            project_id=project_a,
            work_id=work_id,
            summary="Private evidence card",
            quotable_points=[
                {
                    "text": "Results show that the intervention improved outcomes.",
                    "page": 3,
                    "section": "Results",
                    "paragraph": 1,
                }
            ],
            fulltext_used=True,
            source_hash="private-card",
        )
        await session.commit()

    outcome = await extract_evidence_units(
        _context(project_a, session_factory, tmp_path),
        fulltexts={str(work_id): "TRANSIENT PUBLIC RESULT claims accuracy was 100 percent."},
        work_ids=[work_id],
    )
    assert outcome.units >= 1
    async with session_factory() as session:
        units = list(
            (
                await session.scalars(select(EvidenceUnit).where(EvidenceUnit.work_id == work_id))
            ).all()
        )
        visible_to_b = await list_evidence_units(
            session,
            project_b,
            work_id=work_id,
        )
    assert units
    assert {unit.project_id for unit in units} == {project_a}
    assert all("TRANSIENT PUBLIC" not in unit.text for unit in units)
    assert visible_to_b == []


async def test_pdf_match_uses_local_doi_and_waits_for_confirmation(
    session_factory,
    tmp_path,
    monkeypatch,
) -> None:
    # A genuine cross-project lookup must still fail; keep this assertion fast.
    monkeypatch.setattr(
        "paperforge_worker.pipelines.pdf_upload.PDF_VISIBILITY_TIMEOUT_SECONDS",
        0.0,
    )
    _owner_id, project_a, project_b, work_id = await _seed_projects(session_factory)
    buffer = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.add_metadata(
        {
            "/Title": "Private PDF Evidence",
            "/Author": "Ada Lovelace",
            "/Subject": "doi:10.5555/private-pdf",
        }
    )
    writer.write(buffer)
    content = buffer.getvalue()
    upload_id = uuid.uuid4()
    settings = WorkerSettings(
        _env_file=None,
        storage_backend="filesystem",
        storage_fs_root=str(tmp_path / "objects"),
        llm_default_provider="noop",
    )
    object_key = f"private/{upload_id}.pdf"
    make_object_store(settings).put(object_key, content, content_type="application/pdf")
    async with session_factory() as session:
        session.add(
            LiteraturePdfUpload(
                id=upload_id,
                project_id=project_a,
                filename="paper.pdf",
                object_key=object_key,
                mime="application/pdf",
                bytes=len(content),
                content_hash=hashlib.sha256(content).hexdigest(),
                status="matching",
            )
        )
        await session.commit()
    context = _context(project_a, session_factory, tmp_path)
    context.settings = settings

    with pytest.raises(ValueError, match="pdf_upload_not_found"):
        await match_uploaded_pdf(
            _context(project_b, session_factory, tmp_path),
            upload_id,
        )
    outcome = await match_uploaded_pdf(context, upload_id)

    assert outcome.status == "needs_confirmation"
    assert outcome.matched_work_id == str(work_id)
    assert outcome.match_method == "local_doi_exact"
    async with session_factory() as session:
        upload = await session.get(LiteraturePdfUpload, upload_id)
        library_entry = await session.scalar(
            select(LibraryEntry).where(LibraryEntry.project_id == project_a)
        )
    assert upload is not None and upload.status == "needs_confirmation"
    # Matching never admits the work or analyzes private text before confirmation.
    assert upload.document_file_id is None
    assert library_entry is None

    async with session_factory() as session:
        upload = await session.get(LiteraturePdfUpload, upload_id)
        assert upload is not None
        upload.status = "parsing"
        await session.commit()
    await _persist_match_failure(
        context,
        upload_id,
        reason="stale_duplicate_worker",
        metadata=None,
        candidates=[],
        diagnostics={},
    )
    async with session_factory() as session:
        upload = await session.get(LiteraturePdfUpload, upload_id)
    assert upload is not None and upload.status == "parsing"


async def test_pdf_match_waits_for_upload_row_visibility(monkeypatch) -> None:
    upload = SimpleNamespace(
        status="matching",
        matched_work_id=None,
        object_key="private/paper.pdf",
        bytes=123,
        content_hash="a" * 64,
    )
    results = [None, None, upload]
    sleep_calls = 0

    class _Context:
        project_id = uuid.uuid4()

        @asynccontextmanager
        async def session(self):
            yield self

        async def scalar(self, _statement):
            return results.pop(0)

    async def _no_sleep(_seconds: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1

    monkeypatch.setattr(
        "paperforge_worker.pipelines.pdf_upload.asyncio.sleep",
        _no_sleep,
    )
    visible = await _load_upload(_Context(), uuid.uuid4())  # type: ignore[arg-type]
    assert visible is upload
    assert sleep_calls == 2


async def test_confirm_prepare_waits_for_committed_parsing_state(monkeypatch) -> None:
    upload_id = uuid.uuid4()
    work_id = uuid.uuid4()
    document_id = uuid.uuid4()
    before_commit = SimpleNamespace(
        id=upload_id,
        status="needs_confirmation",
        matched_work_id=work_id,
        document_file_id=None,
    )
    after_commit = SimpleNamespace(
        id=upload_id,
        status="parsing",
        matched_work_id=work_id,
        document_file_id=document_id,
    )
    document = SimpleNamespace(id=document_id, work_id=work_id)
    results = [before_commit, after_commit, document]
    sleep_calls = 0

    class _Context:
        project_id = uuid.uuid4()

        @asynccontextmanager
        async def session(self):
            yield self

        async def scalar(self, _statement):
            return results.pop(0)

    async def _no_sleep(_seconds: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1

    monkeypatch.setattr(
        "paperforge_worker.pipelines.pdf_upload.asyncio.sleep",
        _no_sleep,
    )
    target = await prepare_uploaded_pdf(
        _Context(),  # type: ignore[arg-type]
        upload_id,
    )
    assert target.document_file_id == document_id
    assert target.work_id == work_id
    assert sleep_calls == 1


async def test_confirmed_pdf_pipeline_reaches_ready_with_fulltext_card(
    session_factory,
    tmp_path,
) -> None:
    _owner_id, project_a, _project_b, work_id = await _seed_projects(session_factory)
    content = _minimal_pdf(
        [
            f"Results sentence {index} reports accuracy {70 + index % 20} percent."
            for index in range(60)
        ]
    )
    upload_id = uuid.uuid4()
    object_key = f"private/{upload_id}.pdf"
    settings = WorkerSettings(
        _env_file=None,
        storage_backend="filesystem",
        storage_fs_root=str(tmp_path / "objects"),
        llm_default_provider="noop",
    )
    make_object_store(settings).put(object_key, content, content_type="application/pdf")
    async with session_factory() as session:
        entry, _ = await upsert_entry(
            session,
            project_id=project_a,
            work_id=work_id,
            added_via="pdf_upload",
            status="selected",
            verified=True,
        )
        entry.bibtex_key = "private2026ready"
        document = DocumentFile(
            project_id=project_a,
            work_id=work_id,
            access_scope="private",
            kind="uploaded_pdf",
            object_key=object_key,
            mime="application/pdf",
            bytes=len(content),
            content_hash=hashlib.sha256(content).hexdigest(),
        )
        session.add(document)
        await session.flush()
        session.add(
            LiteraturePdfUpload(
                id=upload_id,
                project_id=project_a,
                filename="confirmed.pdf",
                object_key=object_key,
                mime="application/pdf",
                bytes=len(content),
                content_hash=hashlib.sha256(content).hexdigest(),
                status="parsing",
                matched_work_id=work_id,
                document_file_id=document.id,
            )
        )
        await session.commit()

    result = await run_uploaded_pdf_pipeline(
        {
            "settings": settings,
            "session_factory": session_factory,
            "scholar_cache": InMemoryHttpCache(),
        },
        str(project_a),
        upload_id=str(upload_id),
    )

    assert result["status"] == "ready"
    assert result["parse"]["parsed"] is True
    async with session_factory() as session:
        upload = await session.get(LiteraturePdfUpload, upload_id)
        card = await session.scalar(
            select(LiteratureCard).where(
                LiteratureCard.project_id == project_a,
                LiteratureCard.work_id == work_id,
            )
        )
        parsed = await session.scalar(
            select(DocumentParse).where(
                DocumentParse.document_file_id == document.id,
                DocumentParse.parser_version == PARSER_VERSION,
            )
        )
    assert upload is not None and upload.status == "ready"
    assert card is not None and card.fulltext_used
    assert parsed is not None and parsed.status == "parsed"


async def test_quality_warns_when_core_is_unused_in_current_document(
    session_factory,
    tmp_path,
) -> None:
    _owner_id, project_a, _project_b, core_work_id = await _seed_projects(session_factory)
    async with session_factory() as session:
        core_entry, _ = await upsert_entry(
            session,
            project_id=project_a,
            work_id=core_work_id,
            added_via="search",
            status="selected",
            verified=True,
        )
        core_entry.bibtex_key = "core2026paper"
        core_entry.literature_role = "core"
        general_work = ScholarlyWork(
            canonical_title="General cited work",
            normalized_title_hash=normalized_title_hash("General cited work"),
            doi="10.5555/general-cited",
            is_retracted=False,
        )
        session.add(general_work)
        await session.flush()
        general_entry, _ = await upsert_entry(
            session,
            project_id=project_a,
            work_id=general_work.id,
            added_via="search",
            status="selected",
            verified=True,
        )
        general_entry.bibtex_key = "general2026paper"

        # A citation in an old document version must not satisfy current-body use.
        old_document = await create_document(
            session,
            project_id=project_a,
            outline_id=None,
        )
        old_section = await upsert_section(
            session,
            document_id=old_document.id,
            section_key="old",
            title="Old",
            order_no=0,
            body_ir={"blocks": []},
            cite_keys=[core_entry.bibtex_key],
        )
        await replace_citation_usage(
            session,
            project_id=project_a,
            section_id=old_section.id,
            usages=[
                {
                    "work_id": core_work_id,
                    "cite_key": core_entry.bibtex_key,
                    "context_snippet": "old version",
                }
            ],
        )
        current_document = await create_document(
            session,
            project_id=project_a,
            outline_id=None,
        )
        current_section = await upsert_section(
            session,
            document_id=current_document.id,
            section_key="current",
            title="Current",
            order_no=0,
            body_ir={
                "blocks": [
                    {
                        "type": "paragraph",
                        "runs": [
                            {"t": "text", "v": "Current manuscript background."},
                            {"t": "cite", "keys": [general_entry.bibtex_key]},
                        ],
                    }
                ]
            },
            cite_keys=[general_entry.bibtex_key],
        )
        await replace_citation_usage(
            session,
            project_id=project_a,
            section_id=current_section.id,
            usages=[
                {
                    "work_id": general_work.id,
                    "cite_key": general_entry.bibtex_key,
                    "context_snippet": "current version",
                }
            ],
        )
        await session.commit()

    report = await _quality(
        _context(project_a, session_factory, tmp_path),
        quality_profile="draft",
    )
    warning = next(item for item in report.warnings if item.get("code") == "core_literature_unused")
    assert warning["evaluated"] is True
    assert warning["work_ids"] == [str(core_work_id)]
    assert warning["titles"] == ["Private PDF Evidence"]


def test_core_unused_detection_uses_role_and_pinned_compatibility() -> None:
    core_work = SimpleNamespace(id=uuid.uuid4(), canonical_title="Core")
    pinned_work = SimpleNamespace(id=uuid.uuid4(), canonical_title="Pinned legacy")
    general_work = SimpleNamespace(id=uuid.uuid4(), canonical_title="General")
    entries = [
        (
            SimpleNamespace(
                literature_role="core",
                user_pinned=False,
                bibtex_key="core2026",
            ),
            core_work,
        ),
        (
            SimpleNamespace(
                literature_role="general",
                user_pinned=True,
                bibtex_key="legacy2025",
            ),
            pinned_work,
        ),
        (
            SimpleNamespace(
                literature_role="general",
                user_pinned=False,
                bibtex_key="general2024",
            ),
            general_work,
        ),
    ]
    unused = _unused_core_literature(
        entries,
        [SimpleNamespace(work_id=pinned_work.id)],
    )
    assert [item["work_id"] for item in unused] == [str(core_work.id)]
