"""Freeze the exact current-document soft-check inputs, without production writes."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from db import (
    document_snapshot_hash,
    latest_document,
    list_citation_usage,
    list_entries,
    list_evidence_units,
    list_sections,
)
from db.models import PaperProject
from paperforge_worker.pipelines.citation_decisions import (
    TYPESAFE_SOFT_CHECK_VERSION,
    citation_pairs,
    semantic_sources,
)
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


def digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def save(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Evaluation artifacts can contain private manuscript text. Never overwrite a run.
    with path.open("x", encoding="utf-8") as stream:
        path.chmod(0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2, default=str)


async def freeze(database_url: str, output: Path, target: int = 400) -> dict:
    engine = create_async_engine(database_url)
    groups, seen = [], set()
    try:
        async with async_sessionmaker(engine)() as session:
            await session.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            )
            projects = list(
                await session.scalars(
                    select(PaperProject)
                    .where(PaperProject.deleted_at.is_(None))
                    .order_by(PaperProject.created_at.desc(), PaperProject.id)
                )
            )
            for project in projects:
                document = await latest_document(session, project.id)
                if document is None:
                    continue
                sections = await list_sections(session, document.id)
                ids = {s.id for s in sections}
                usages = [
                    u for u in await list_citation_usage(session, project.id) if u.section_id in ids
                ]
                entries = await list_entries(session, project.id, status="selected")
                units = await list_evidence_units(session, project.id)
                sources = semantic_sources(
                    entries, [{"work_id": str(u.work_id), "text": u.text} for u in units]
                )
                usage_dicts = [
                    {
                        "cite_key": u.cite_key,
                        "section_key": "",
                        "context_snippet": u.context_snippet,
                    }
                    for u in usages
                ]
                pairs = citation_pairs(usage_dicts, sources)
                if not pairs or any(p["pair_hash"] in seen for p in pairs):
                    # Keep complete production batches for call-savings estimates;
                    # avoid cross-project duplicate text leaking between splits.
                    continue
                selected = {entry.bibtex_key: str(work.id) for entry, work in entries}
                with_units = {str(u.work_id) for u in units if u.text}
                for p in pairs:
                    p["source_kind"] = (
                        "evidence_unit" if selected.get(p["cite_key"]) in with_units else "abstract"
                    )
                    p["context_truncated"] = (
                        len(
                            str(
                                next(
                                    u["context_snippet"]
                                    for u in usage_dicts
                                    if u["cite_key"] == p["cite_key"]
                                    and str(u["context_snippet"])[:300] == p["context"]
                                )
                            )
                        )
                        > 300
                    )
                    p["evidence_truncated"] = len(sources[p["cite_key"]]) > 400
                    p["sample_id"] = f"p{len(groups):02d}-{p['index']:02d}"
                groups.append(
                    {
                        "project_id": str(project.id),
                        "document_id": str(document.id),
                        "document_version": document.version,
                        "language": project.language,
                        "snapshot": document_snapshot_hash(sections),
                        "pairs": pairs,
                    }
                )
                seen.update(p["pair_hash"] for p in pairs)
                if len(seen) >= target:
                    break
    finally:
        await engine.dispose()
    totals = {"dev": 0, "test": 0}
    for group in groups:
        split = min(totals, key=totals.get)
        group["split"] = split
        totals[split] += len({p["pair_hash"] for p in group["pairs"]})
    corpus = {
        "version": TYPESAFE_SOFT_CHECK_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "target_unique_pairs": target,
        "unique_pairs": len(seen),
        "projects": len(groups),
        "split_counts": totals,
        "groups": groups,
    }
    corpus["corpus_hash"] = digest(groups)
    save(output / "corpus.json", corpus)
    save(
        output / "blind_review.json",
        {
            "corpus_hash": corpus["corpus_hash"],
            "reviewer": "AI-reviewed: Codex",
            "rubric": "0 unrelated/no support; 1 topical only; 2 partial/ambiguous; "
            "3 mostly supported; 4 direct support. Mark ambiguous when label is uncertain.",
            "items": [
                {
                    "sample_id": p["sample_id"],
                    "pair_hash": p["pair_hash"],
                    "context": p["context"],
                    "evidence": p["evidence"],
                    "grade": None,
                    "ambiguous": None,
                    "reason": None,
                }
                for g in groups
                for p in g["pairs"]
            ],
        },
    )
    return {k: v for k, v in corpus.items() if k != "groups"}
