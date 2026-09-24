"""Read-only search, permission filtering BEFORE retrieval, hash-bound incremental indexing."""

import asyncio
import hashlib
import json
import time
import uuid

from db.models.library import DocumentFile, EvidenceUnit, LibraryEntry, ScholarlyWork
from db.models.retrieval import EvidenceEmbedding
from sqlalchemy import or_, select, text
from sqlalchemy.dialects.postgresql import insert

from retrieval import models
from retrieval.ranking import VERSION, cosine, lexical_rank, rrf

CORPUS_LIMIT = 10000


def content_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def visible_evidence(project_id):
    return (
        select(EvidenceUnit)
        .join(LibraryEntry, LibraryEntry.work_id == EvidenceUnit.work_id)
        .join(ScholarlyWork, ScholarlyWork.id == EvidenceUnit.work_id)
        .outerjoin(DocumentFile, DocumentFile.id == EvidenceUnit.source_document_file_id)
        .where(
            LibraryEntry.project_id == project_id,
            LibraryEntry.status == "selected",
            ScholarlyWork.is_retracted.is_(False),
            or_(
                EvidenceUnit.source_document_file_id.is_(None),
                DocumentFile.access_scope == "shared",
                DocumentFile.project_id == project_id,
            ),
            or_(EvidenceUnit.project_id.is_(None), EvidenceUnit.project_id == project_id),
        )
        .order_by(EvidenceUnit.id)
    )


async def search(
    session,
    project_id: uuid.UUID,
    query: str,
    *,
    limit=12,
    mode="hybrid",
    embed=models.embed,
    rerank=models.rerank,
) -> dict:
    started = time.monotonic()
    rows = list((await session.scalars(visible_evidence(project_id).limit(CORPUS_LIMIT + 1))).all())
    truncated = len(rows) > CORPUS_LIMIT
    rows = rows[:CORPUS_LIMIT]
    by_id = {str(row.id): row for row in rows}
    documents = {key: row.text for key, row in by_id.items()}
    lexical = lexical_rank(query, documents)
    dense = []
    warnings = []
    backend = "none"
    coverage = 0
    if mode != "lexical" and rows:
        indexed = list(
            (
                await session.scalars(
                    select(EvidenceEmbedding).where(
                        EvidenceEmbedding.project_id == project_id,
                        EvidenceEmbedding.model == models.MODEL,
                        EvidenceEmbedding.evidence_id.in_([row.id for row in rows]),
                    )
                )
            ).all()
        )
        indexed = [
            row
            for row in indexed
            if row.content_hash == content_hash(documents[str(row.evidence_id)])
        ]
        coverage = len(indexed)
        if indexed:
            try:
                vector = (await asyncio.to_thread(embed, [query]))[0]
                # Validate before SQL so malformed/zero vectors cannot break the transaction.
                cosine(vector, vector)
                compatible = [row for row in indexed if len(row.embedding) == len(vector)]
                has_vector = await session.scalar(
                    text("SELECT EXISTS(SELECT 1 FROM pg_extension WHERE extname='vector')")
                )
                if has_vector and compatible:
                    # A failed optional vector query must not poison the caller transaction.
                    async with session.begin_nested():
                        result = await session.execute(
                            text("""
                            SELECT evidence_id,
                                1-(embedding::vector <=> CAST(:query AS vector)) AS score
                            FROM evidence_embedding WHERE project_id=:project AND model=:model
                            AND evidence_id=ANY(CAST(:ids AS uuid[]))
                            ORDER BY score DESC, evidence_id
                            LIMIT 40
                        """),
                            {
                                "query": json.dumps(vector),
                                "project": project_id,
                                "model": models.MODEL,
                                "ids": [row.evidence_id for row in compatible],
                            },
                        )
                    dense = [(str(row[0]), float(row[1])) for row in result]
                    backend = "pgvector_exact"
                else:
                    dense = sorted(
                        [
                            (str(row.evidence_id), cosine(vector, row.embedding))
                            for row in compatible
                        ],
                        key=lambda item: (-item[1], item[0]),
                    )[:40]
                    backend = "portable_exact"
            except Exception as error:
                warnings.append(type(error).__name__ + ":embedding_unavailable")
        else:
            warnings.append("embedding_index_missing")
    if mode == "vector":
        ranking = dense
    elif mode == "lexical":
        ranking = lexical
    else:
        ranking = rrf(lexical, dense)
    reranked = False
    if mode == "hybrid_rerank" and ranking:
        try:
            scores = await asyncio.to_thread(rerank, query, [documents[key] for key, _ in ranking])
            if len(scores) != len(ranking):
                raise ValueError("reranker_length_mismatch")
            import math

            if not all(math.isfinite(score) for score in scores):
                raise ValueError("invalid_reranker_score")
            ranking = sorted(
                [(key, score) for (key, _), score in zip(ranking, scores, strict=True)],
                key=lambda item: (-item[1], item[0]),
            )
            reranked = True
        except Exception as error:
            warnings.append(
                str(error)
                if str(error) == "reranker_language_unsupported"
                else "reranker_unavailable"
            )
    return {
        "version": VERSION,
        "mode": mode,
        "vector_backend": backend,
        "reranked": reranked,
        "corpus_count": len(rows),
        "indexed_count": coverage,
        "corpus_truncated": truncated,
        "warnings": warnings,
        "elapsed_ms": round((time.monotonic() - started) * 1000),
        "results": [
            {
                "evidence_id": key,
                "work_id": str(by_id[key].work_id),
                "text": by_id[key].text,
                "content_hash": content_hash(by_id[key].text),
                "page": by_id[key].page,
                "section_path": by_id[key].section_path,
                "source_document_file_id": str(by_id[key].source_document_file_id)
                if by_id[key].source_document_file_id
                else None,
                "char_start": by_id[key].char_start,
                "char_end": by_id[key].char_end,
                "grade": by_id[key].grade,
                "score": score,
                "verification_status": "not_assessed",
            }
            for key, score in ranking[:limit]
        ],
    }


async def index_project(
    session_factory, project_id, *, embed=models.embed, stop_check=None, before_commit=None
):
    async with session_factory() as session:
        rows = list(
            (await session.scalars(visible_evidence(project_id).limit(CORPUS_LIMIT + 1))).all()
        )
        if len(rows) > CORPUS_LIMIT:
            raise ValueError("corpus exceeds indexing limit")
        cached = {
            (r.evidence_id, r.content_hash)
            for r in (
                await session.scalars(
                    select(EvidenceEmbedding).where(
                        EvidenceEmbedding.project_id == project_id,
                        EvidenceEmbedding.model == models.MODEL,
                    )
                )
            ).all()
        }
    pending = [row for row in rows if (row.id, content_hash(row.text)) not in cached]
    written = 0
    for offset in range(0, len(pending), 16):
        if stop_check is not None:
            await stop_check()
        batch = pending[offset : offset + 16]
        vectors = await asyncio.to_thread(embed, [row.text for row in batch])
        if len(vectors) != len(batch):
            raise ValueError("embedding count mismatch")
        async with session_factory() as session:
            visible = {
                row.id: row
                for row in (
                    await session.scalars(
                        visible_evidence(project_id).where(
                            EvidenceUnit.id.in_([r.id for r in batch])
                        )
                    )
                ).all()
            }
            for row, vector in zip(batch, vectors, strict=True):
                cosine(vector, vector)
                current = visible.get(row.id)
                if current is None or content_hash(current.text) != content_hash(row.text):
                    continue
                stmt = insert(EvidenceEmbedding).values(
                    project_id=project_id,
                    evidence_id=row.id,
                    model=models.MODEL,
                    content_hash=content_hash(row.text),
                    embedding=vector,
                )
                await session.execute(
                    stmt.on_conflict_do_update(
                        index_elements=["project_id", "evidence_id", "model"],
                        set_={
                            "content_hash": stmt.excluded.content_hash,
                            "embedding": stmt.excluded.embedding,
                        },
                    )
                )
                written += 1
            if before_commit is not None:
                await before_commit(session)
            await session.commit()
    return {"indexed": written, "reused": len(rows) - len(pending), "model": models.MODEL}
