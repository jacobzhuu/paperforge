"""INGEST 阶段：OA 全文获取 → 解析 → section 感知切块（设计 §4.4.1）。

合规红线（§1.4）：只走 OA/官方渠道给出的 URL，不绕 paywall。抓不到就摘要级降级——
卡片质量下降是可以接受的，绕过访问控制不是。

产物：`document_file` 行 + 对象存储里的原始字节；解析出的 section 感知块
供 CARDS 阶段把摘要级卡片升级为全文级卡片。
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field, replace
from typing import Any

from db import get_work_urls, list_entries
from db.models.library import DocumentChunk, DocumentFile, DocumentParse, FulltextAttempt
from db.text_safety import sanitize_pg_text
from ingest import (
    ChunkText,
    assess_chunk_quality,
    classify_content_role,
    select_section_aware_chunks,
    try_extract_and_chunk,
)
from observability import get_logger
from scholar_gateway import (
    OaFulltextTarget,
    SafeHttpClient,
    acquire_oa_fulltext,
    discover_doaj_links,
    discover_unpaywall_links,
    plan_oa_fulltext,
)
from sqlalchemy import and_, case, or_, select

from paperforge_worker.context import JobContext

logger = get_logger(__name__)

# 单次任务的全文抓取预算：按引文影响力挑选，避免为长尾文献消耗大量带宽。
DEFAULT_MAX_WORKS = 40
MAX_FULLTEXT_CHARS = 120_000
MAX_SELECTED_CHUNKS = 96


@dataclass
class FulltextOutcome:
    selected: int = 0
    planned: int = 0
    reused: int = 0
    acquired: int = 0
    parsed: int = 0
    documents_ok: int = 0
    documents_failed: int = 0
    failures: list[dict[str, Any]] = field(default_factory=list)
    coverage: float = 0.0

    def to_payload(self) -> dict[str, Any]:
        return {
            "selected": self.selected,
            "planned": self.planned,
            "reused": self.reused,
            "acquired": self.acquired,
            "parsed": self.parsed,
            "documents_ok": self.documents_ok,
            "documents_failed": self.documents_failed,
            "parse_coverage": (self.documents_ok / self.acquired if self.acquired else 0.0),
            "coverage": round(self.coverage, 4),
            "failures": self.failures[:10],
        }


@dataclass(frozen=True)
class FulltextSource:
    """An authorized parsed source, including the ACL needed by derived caches."""

    work_id: str
    document_file_id: Any
    access_scope: str
    text: str

    @property
    def is_private(self) -> bool:
        return self.access_scope == "private"


@dataclass
class DocumentParseOutcome:
    document_file_id: str
    work_id: str
    parsed: bool = False
    text_chars: int = 0
    chunks: int = 0
    error: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "document_file_id": self.document_file_id,
            "work_id": self.work_id,
            "parsed": self.parsed,
            "text_chars": self.text_chars,
            "chunks": self.chunks,
            "error": self.error,
        }


@dataclass(frozen=True)
class _StoredChunk:
    chunk_no: int
    text: str
    metadata: dict[str, Any]


async def acquire_fulltexts(
    context: JobContext,
    *,
    max_works: int = DEFAULT_MAX_WORKS,
) -> tuple[FulltextOutcome, dict[str, str]]:
    """抓取并解析 OA 全文，返回 (统计, work_id → 全文文本)。"""
    outcome = FulltextOutcome()
    texts: dict[str, str] = {}

    async with context.session() as session:
        entries = await list_entries(session, context.project_id, status="selected")
        selected_count = len(entries)

    outcome.selected = selected_count
    persisted_sources = await load_persisted_fulltext_sources(
        context,
        [work.id for _entry, work in entries],
    )
    texts.update({work_id: source.text for work_id, source in persisted_sources.items()})
    outcome.reused = len(persisted_sources)
    targets: list[OaFulltextTarget] = []
    async with context.session() as session:
        for entry, work in entries:
            if str(work.id) in persisted_sources:
                continue
            links = await get_work_urls(session, work.id)
            targets.append(
                OaFulltextTarget(
                    work_id=str(work.id),
                    arxiv_id=work.arxiv_id,
                    pmcid=work.pmcid,
                    doi=work.doi,
                    links=tuple(links),
                    relevance_score=entry.relevance_score,
                    influential_citation_count=work.influential_citation_count,
                )
            )
    if not targets:
        outcome.coverage = outcome.reused / selected_count if selected_count else 0.0
        await context.emit("ingest.fulltext", outcome.to_payload(), stage="ingest")
        return outcome, texts

    http = SafeHttpClient(
        user_agent=context.settings.user_agent(),
        timeout_seconds=context.settings.scholar_timeout_seconds * 2,
    )
    try:
        targets = await asyncio.to_thread(
            _enrich_unpaywall_targets,
            targets,
            http,
            context.settings.scholar_contact_email,
        )
        plan = plan_oa_fulltext(targets, max_works=max_works)
        outcome.planned = len({c.work_id for c in plan.candidates})
        if not plan.candidates:
            outcome.coverage = outcome.reused / selected_count if selected_count else 0.0
            await context.emit("ingest.fulltext", outcome.to_payload(), stage="ingest")
            return outcome, texts
        result = await asyncio.to_thread(acquire_oa_fulltext, plan, http_client=http)
    finally:
        http.close()

    outcome.acquired = len(result.documents)
    outcome.failures = list(result.failures)

    # URL-level attempts are first-class operational evidence.  A work-level
    # summary is retained for compatibility, but never hides whether a DOI,
    # arXiv, or OA landing URL was actually tried.
    async with context.session() as session:
        for attempt in result.attempts:
            session.add(
                FulltextAttempt(
                    project_id=context.project_id,
                    job_id=context.job_id,
                    work_id=_as_uuid(attempt.work_id),
                    url=attempt.url,
                    source=attempt.source,
                    status=attempt.status,
                    http_status=attempt.http_status,
                    mime_type=attempt.mime_type,
                    error_code=attempt.error_code,
                    error_detail=attempt.error_detail,
                )
            )

    from storage import make_object_store

    store = make_object_store(context.settings)
    for document in result.documents:
        try:
            await _persist_one_document(
                context,
                store=store,
                document=document,
                outcome=outcome,
                texts=texts,
            )
        except Exception as error:  # noqa: BLE001 - one bad PDF must not abort ingest
            outcome.documents_failed += 1
            failure = {
                "work_id": document.work_id,
                "error": type(error).__name__,
                "message": str(error)[:300],
            }
            outcome.failures.append(failure)
            context.warn("ingest.document_failed", type(error).__name__, failure)
            await context.emit("ingest.document_failed", failure, stage="ingest")

    outcome.coverage = (outcome.parsed + outcome.reused) / selected_count if selected_count else 0.0
    await context.emit(
        "ingest.fulltext",
        outcome.to_payload(),
        stage="ingest",
    )
    return outcome, texts


async def _persist_one_document(
    context: JobContext,
    *,
    store: Any,
    document: Any,
    outcome: FulltextOutcome,
    texts: dict[str, str],
) -> None:
    """Persist and parse one OA document in isolation (R14 / N0-4)."""
    key = f"shared/oa/works/{document.work_id}/fulltext-{document.content_hash[:12]}.bin"
    async with context.session() as session:
        stored_document = await session.scalar(
            select(DocumentFile).where(
                DocumentFile.work_id == _as_uuid(document.work_id),
                DocumentFile.content_hash == document.content_hash,
                DocumentFile.access_scope == "shared",
                DocumentFile.project_id.is_(None),
            )
        )
        if stored_document is None:
            store.put(key, document.content)
            stored_document = DocumentFile(
                project_id=None,
                work_id=_as_uuid(document.work_id),
                access_scope="shared",
                kind=_document_kind(document.mime_type),
                object_key=key,
                mime=document.mime_type,
                bytes=len(document.content),
                fetched_from_url=document.url,
                license=document.license,
                content_hash=document.content_hash,
            )
            session.add(stored_document)
            await session.flush()
        stored_document_id = stored_document.id
    parsed, chunks, error = try_extract_and_chunk(
        mime_type=document.mime_type,
        content=document.content,
    )
    if parsed is None:
        outcome.documents_failed += 1
        outcome.failures.append({"work_id": document.work_id, "reason": str(error)})
        async with context.session() as session:
            await _persist_parse(
                session,
                document_file_id=stored_document_id,
                status="failed",
                extracted_text=None,
                metadata=None,
                chunks=[],
                error={"reason": str(error)[:1000]},
            )
        return
    parsed_license = str((parsed.metadata or {}).get("license") or "").strip()
    if parsed_license and not document.license:
        async with context.session() as session:
            document_row = await session.get(DocumentFile, stored_document_id)
            if document_row is not None:
                document_row.license = parsed_license[:128]
    async with context.session() as session:
        await _persist_parse(
            session,
            document_file_id=stored_document_id,
            status="parsed",
            extracted_text=sanitize_pg_text(parsed.text, max_chars=MAX_FULLTEXT_CHARS),
            metadata=parsed.metadata or {},
            chunks=chunks,
            error=None,
        )
    usable = [
        _located_chunk_text(chunk, parsed.metadata or {}) for chunk in _select_card_chunks(chunks)
    ]
    text = sanitize_pg_text("\n\n".join(usable), max_chars=MAX_FULLTEXT_CHARS) or ""
    outcome.documents_ok += 1
    if text:
        texts[document.work_id] = text
        outcome.parsed += 1


PARSER_VERSION = "ingest_fulltext_v2"


async def parse_document_file(
    context: JobContext,
    document_file_id: Any,
) -> tuple[DocumentParseOutcome, dict[str, str]]:
    """Parse one project-authorized, already persisted document.

    Uploaded PDFs are private.  The authorization predicate lives here rather
    than at the caller so a future worker entry point cannot accidentally parse
    another project's raw object by guessing its UUID.
    """
    document_uuid = _coerce_uuid(document_file_id)
    async with context.session() as session:
        document = await session.scalar(
            select(DocumentFile).where(
                DocumentFile.id == document_uuid,
                _document_access_clause(context),
            )
        )
        if document is None:
            raise ValueError("document_not_found_or_not_authorized")
        work_id = str(document.work_id)
        object_key = document.object_key
        mime = document.mime or "application/pdf"
        expected_bytes = document.bytes
        expected_hash = document.content_hash

    outcome = DocumentParseOutcome(
        document_file_id=str(document_uuid),
        work_id=work_id,
    )
    from storage import make_object_store

    store = make_object_store(context.settings)
    try:
        content = await asyncio.to_thread(store.get, object_key)
    except Exception as error:  # noqa: BLE001 - normalize object-store failures
        reason = f"object_read_failed:{type(error).__name__}"
        await _persist_failed_document_parse(context, document_uuid, reason)
        outcome.error = reason
        return outcome, {}

    integrity_error = _document_integrity_error(
        content,
        expected_bytes=expected_bytes,
        expected_hash=expected_hash,
    )
    if integrity_error:
        await _persist_failed_document_parse(context, document_uuid, integrity_error)
        outcome.error = integrity_error
        return outcome, {}

    parsed, chunks, parse_error = await asyncio.to_thread(
        try_extract_and_chunk,
        mime_type=mime,
        content=content,
    )
    if parsed is None:
        reason = str(parse_error or "document_parse_failed")[:1000]
        await _persist_failed_document_parse(context, document_uuid, reason)
        outcome.error = reason
        return outcome, {}

    async with context.session() as session:
        await _persist_parse(
            session,
            document_file_id=document_uuid,
            status="parsed",
            extracted_text=sanitize_pg_text(parsed.text, max_chars=MAX_FULLTEXT_CHARS),
            metadata=parsed.metadata or {},
            chunks=chunks,
            error=None,
        )
    usable = [
        _located_chunk_text(chunk, parsed.metadata or {}) for chunk in _select_card_chunks(chunks)
    ]
    text = sanitize_pg_text("\n\n".join(usable), max_chars=MAX_FULLTEXT_CHARS) or ""
    outcome.parsed = bool(text)
    outcome.text_chars = len(text)
    outcome.chunks = len(chunks)
    if not text:
        outcome.error = "no_usable_fulltext"
        await _persist_failed_document_parse(context, document_uuid, outcome.error)
        return outcome, {}
    return outcome, {work_id: text}


async def load_persisted_fulltext_sources(
    context: JobContext,
    work_ids: list[Any],
) -> dict[str, FulltextSource]:
    """Load only shared or current-project parses, preserving source provenance."""
    if not work_ids:
        return {}
    async with context.session() as session:
        rows = (
            await session.execute(
                select(DocumentFile, DocumentParse)
                .join(DocumentParse, DocumentParse.document_file_id == DocumentFile.id)
                .where(
                    DocumentFile.work_id.in_(work_ids),
                    _document_access_clause(context),
                    DocumentParse.parser_version == PARSER_VERSION,
                    DocumentParse.status == "parsed",
                    DocumentParse.extracted_text.is_not(None),
                )
                # A user's explicitly uploaded copy is the preferred source for
                # their project; shared OA remains the safe fallback.
                .order_by(
                    DocumentFile.work_id,
                    case((DocumentFile.access_scope == "private", 0), else_=1),
                    DocumentFile.created_at.desc(),
                    DocumentFile.id.desc(),
                )
            )
        ).all()
        selected: dict[str, tuple[DocumentFile, DocumentParse]] = {}
        for document, parsed in rows:
            selected.setdefault(str(document.work_id), (document, parsed))

        sources: dict[str, FulltextSource] = {}
        for work_id, (document, parsed) in selected.items():
            chunks = list(
                (
                    await session.scalars(
                        select(DocumentChunk)
                        .where(DocumentChunk.document_parse_id == parsed.id)
                        .order_by(DocumentChunk.chunk_no)
                    )
                ).all()
            )
            if chunks:
                usable = [
                    _located_chunk_text(_persisted_chunk(chunk), parsed.metadata_json or {})
                    for chunk in _select_card_chunks(chunks)
                ]
                text = (
                    sanitize_pg_text(
                        "\n\n".join(usable),
                        max_chars=MAX_FULLTEXT_CHARS,
                    )
                    or ""
                )
            else:
                text = (
                    sanitize_pg_text(
                        parsed.extracted_text,
                        max_chars=MAX_FULLTEXT_CHARS,
                    )
                    or ""
                )
            if text:
                sources[work_id] = FulltextSource(
                    work_id=work_id,
                    document_file_id=document.id,
                    access_scope=document.access_scope,
                    text=text,
                )
    return sources


async def load_persisted_fulltexts(
    context: JobContext,
    work_ids: list[Any],
) -> dict[str, str]:
    """Load durable parsed full text for reruns or stages launched independently."""
    sources = await load_persisted_fulltext_sources(context, work_ids)
    return {work_id: source.text for work_id, source in sources.items()}


async def _persist_failed_document_parse(
    context: JobContext,
    document_file_id: Any,
    reason: str,
) -> None:
    async with context.session() as session:
        await _persist_parse(
            session,
            document_file_id=document_file_id,
            status="failed",
            extracted_text=None,
            metadata=None,
            chunks=[],
            error={"reason": str(reason)[:1000]},
        )


async def _persist_parse(
    session: Any,
    *,
    document_file_id: Any,
    status: str,
    extracted_text: str | None,
    metadata: dict[str, Any] | None,
    chunks: list[Any],
    error: dict[str, Any] | None,
) -> None:
    """Idempotently store parse text and addressable chunks for later stages."""
    parsed = await session.scalar(
        select(DocumentParse).where(
            DocumentParse.document_file_id == document_file_id,
            DocumentParse.parser_version == PARSER_VERSION,
        )
    )
    if parsed is None:
        parsed = DocumentParse(
            document_file_id=document_file_id,
            parser_version=PARSER_VERSION,
            status=status,
        )
        session.add(parsed)
        await session.flush()
    parsed.status = status
    parsed.extracted_text = sanitize_pg_text(extracted_text) if extracted_text else None
    parsed.metadata_json = metadata
    parsed.error_json = error
    if status != "parsed":
        return
    existing = {
        item.chunk_no: item
        for item in (
            await session.scalars(
                select(DocumentChunk).where(DocumentChunk.document_parse_id == parsed.id)
            )
        ).all()
    }
    for chunk in chunks:
        item = existing.pop(chunk.chunk_no, None)
        chunk_metadata = dict(chunk.metadata or {})
        safe_text = sanitize_pg_text(chunk.text) or ""
        if item is None:
            item = DocumentChunk(
                document_parse_id=parsed.id,
                chunk_no=chunk.chunk_no,
                text=safe_text,
            )
            session.add(item)
        item.text = safe_text
        item.content_role = classify_content_role(safe_text).role
        item.page = _as_int(chunk_metadata.get("page_number"))
        item.section_path = (
            str(chunk_metadata.get("section_title") or chunk_metadata.get("heading") or "")[:1000]
            or None
        )
        item.object_ref = str(chunk_metadata.get("object_ref") or "")[:64] or None
        item.char_start = _as_int(chunk_metadata.get("char_start"))
        item.char_end = _as_int(chunk_metadata.get("char_end"))
        item.metadata_json = chunk_metadata
    for stale in existing.values():
        await session.delete(stale)


def _as_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _document_access_clause(context: JobContext) -> Any:
    return or_(
        and_(
            DocumentFile.access_scope == "shared",
            DocumentFile.project_id.is_(None),
        ),
        and_(
            DocumentFile.access_scope == "private",
            DocumentFile.project_id == context.project_id,
        ),
    )


def _document_integrity_error(
    content: bytes,
    *,
    expected_bytes: int | None,
    expected_hash: str | None,
) -> str | None:
    if expected_bytes is not None and len(content) != expected_bytes:
        return "document_size_mismatch"
    if expected_hash:
        actual_hash = hashlib.sha256(content).hexdigest()
        if actual_hash.casefold() != str(expected_hash).casefold():
            return "document_hash_mismatch"
    return None


def _persisted_chunk(chunk: DocumentChunk) -> _StoredChunk:
    metadata = dict(chunk.metadata_json or {})
    if chunk.char_start is not None:
        metadata.setdefault("char_start", chunk.char_start)
    if chunk.char_end is not None:
        metadata.setdefault("char_end", chunk.char_end)
    if chunk.page is not None:
        metadata.setdefault("page_number", chunk.page)
    if chunk.section_path:
        metadata.setdefault("section_title", chunk.section_path)
    if chunk.object_ref:
        metadata.setdefault("object_ref", chunk.object_ref)
    return _StoredChunk(
        chunk_no=chunk.chunk_no,
        text=chunk.text,
        metadata=metadata,
    )


def _select_card_chunks(chunks: list[Any]) -> list[Any]:
    """选择卡片抽取块：章节优先，随后按原文顺序补齐合格科学文本。"""
    by_id = {chunk.chunk_no: chunk for chunk in chunks}
    prioritized = select_section_aware_chunks(
        [ChunkText(chunk_id=chunk.chunk_no, text=chunk.text) for chunk in chunks],
        max_chunks=MAX_SELECTED_CHUNKS,
        char_budget=MAX_FULLTEXT_CHARS,
    )
    selected_ids = [item.chunk_id for item in prioritized]
    selected_set = set(selected_ids)
    selected_chars = sum(len(item.text) for item in prioritized)

    for chunk in chunks:
        if chunk.chunk_no in selected_set:
            continue
        role = classify_content_role(chunk.text)
        quality = assess_chunk_quality(text=chunk.text)
        if not role.claim_eligible or (
            role.role != "structured_table" and not quality.usable_for_cards
        ):
            continue
        if selected_chars >= MAX_FULLTEXT_CHARS or len(selected_ids) >= MAX_SELECTED_CHUNKS:
            break
        selected_ids.append(chunk.chunk_no)
        selected_set.add(chunk.chunk_no)
        selected_chars += len(chunk.text)

    # 最终仍按原文顺序交给模型，避免章节优先级重排破坏语义与定位。
    return [by_id[chunk_id] for chunk_id in sorted(selected_ids) if chunk_id in by_id]


def content_key(prefix: str, data: bytes) -> str:
    return f"{prefix}/{hashlib.sha256(data).hexdigest()[:16]}"


def _enrich_unpaywall_targets(
    targets: list[OaFulltextTarget],
    http: SafeHttpClient,
    contact_email: str,
) -> list[OaFulltextTarget]:
    enriched: list[OaFulltextTarget] = []
    for target in targets:
        links = discover_unpaywall_links(
            target,
            http_client=http,
            contact_email=contact_email,
        )
        doaj_links = discover_doaj_links(target, http_client=http)
        discovered = (*links, *doaj_links)
        enriched.append(
            replace(target, links=(*target.links, *discovered)) if discovered else target
        )
    return enriched


def _document_kind(mime_type: str) -> str:
    return {
        "application/pdf": "oa_pdf",
        "application/jats+xml": "jats",
        "application/xml": "xml",
        "text/xml": "xml",
        "application/x-arxiv-source": "latex_source",
    }.get(mime_type, "html")


def _as_uuid(value: str):
    import uuid

    return uuid.UUID(value)


def _coerce_uuid(value: Any):
    import uuid

    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _located_chunk_text(chunk: Any, metadata: dict[str, Any]) -> str:
    """把可靠页码/章节标记注入全文上下文，供证据锚点确定性回存。"""
    chunk_metadata = dict(chunk.metadata or {})
    start = int(chunk_metadata.get("char_start") or 0)
    segments = metadata.get("structure_segments") or []
    matching = [
        segment
        for segment in segments
        if int(segment.get("char_start") or 0) <= start < int(segment.get("char_end") or 0)
    ]
    located: dict[str, Any] = {
        key: value
        for key, value in {
            "page_number": chunk_metadata.get("page_number"),
            "page_locator_reliable": chunk_metadata.get("page_number") is not None,
            "section_title": (chunk_metadata.get("section_title") or chunk_metadata.get("heading")),
            "object_ref": chunk_metadata.get("object_ref"),
        }.items()
        if value is not None
    }
    # 页/章节段和表格/图/式对象可能重叠；合并而不是只取第一个。
    for segment in sorted(matching, key=lambda item: bool(item.get("object_ref"))):
        located.update({key: value for key, value in segment.items() if value is not None})
    markers: list[str] = []
    if located.get("page_locator_reliable") and located.get("page_number") is not None:
        markers.append(f"PAGE={located['page_number']}")
    section = located.get("section_title") or located.get("heading")
    if section:
        markers.append(f"SECTION={str(section)[:160]}")
    object_ref = str(located.get("object_ref") or "")
    if ":" in object_ref:
        kind, value = object_ref.split(":", 1)
        marker_name = {
            "table": "TABLE",
            "fig": "FIG",
            "figure": "FIG",
            "eq": "EQ",
            "equation": "EQ",
            "algo": "ALGO",
            "algorithm": "ALGO",
        }.get(kind.casefold())
        if marker_name and value:
            markers.append(f"{marker_name}={value[:64]}")
    prefix = f"[[{' | '.join(markers)}]]\n" if markers else ""
    return prefix + chunk.text
