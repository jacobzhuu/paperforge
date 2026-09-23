"""DOI / BibTeX 导入 + R1 入库核验（设计 §4.4.3 R1）。

用户给的 DOI 或 .bib 只是**线索**：必须经 Crossref/OpenAlex 反查，
以真实 provider 响应重建元数据后才允许入库；反查失败即丢弃并记录
``verification_failed``，绝不因为「用户说它存在」就放行。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from db import assign_bibtex_key, reference_metadata_payload, upsert_entry, upsert_work
from paper_ir import ReferenceMetadata, make_bibtex_key, parse_bibtex_entries
from scholar_gateway import VerificationRequest, verify_reference

from paperforge_worker.context import JobContext

MAX_IMPORT_ITEMS = 200


@dataclass
class ImportOutcome:
    requested: int = 0
    verified: int = 0
    rejected: int = 0
    entries: list[dict[str, Any]] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        return {
            "requested": self.requested,
            "verified": self.verified,
            "rejected": self.rejected,
            "entries": self.entries,
            "failures": self.failures,
        }


def requests_from_dois(dois: list[str]) -> list[VerificationRequest]:
    return [
        VerificationRequest(doi=doi.strip(), source_label="doi_import")
        for doi in dois
        if doi and doi.strip()
    ]


def requests_from_bibtex(text: str) -> list[VerificationRequest]:
    """解析 .bib 为待核验线索。条目字段一律不直接入库（R1）。"""
    return [
        VerificationRequest(
            doi=entry.doi,
            title=entry.title,
            authors=entry.authors,
            publication_year=entry.publication_year,
            arxiv_id=entry.arxiv_id,
            source_label=f"bibtex:{entry.key}" if entry.key else "bibtex_import",
        )
        for entry in parse_bibtex_entries(text)
    ]


async def import_references(
    context: JobContext,
    requests: list[VerificationRequest],
    *,
    added_via: str,
    status: str = "selected",
) -> ImportOutcome:
    """逐条反查并入库。任何一条失败都不影响其他条（draft-first）。"""
    outcome = ImportOutcome(requested=len(requests))
    if not requests:
        return outcome
    if added_via not in {
        "doi_import", "bibtex_import", "llm_suggested_verified", "mcp_web_verified"
    }:
        raise ValueError(f"unsupported import source: {added_via}")

    for index, request in enumerate(requests[:MAX_IMPORT_ITEMS]):
        result = await asyncio.to_thread(
            verify_reference,
            request,
            client=context.http_client,
            config=context.settings.provider_config("crossref"),
            cache=context.scholar_cache,
        )
        if not result.verified or result.candidate is None:
            outcome.rejected += 1
            failure = {
                "doi": request.doi,
                "title": request.title,
                "source": request.source_label,
                "reason": result.failure_reason or "verification_failed",
                "diagnostics": result.diagnostics,
            }
            outcome.failures.append(failure)
            context.warn("import", failure["reason"], {"source": request.source_label})
            await context.emit("import.rejected", failure, stage="import")
            continue

        async with context.session() as session:
            work, _created = await upsert_work(session, result.candidate)
            entry, _entry_created = await upsert_entry(
                session,
                project_id=context.project_id,
                work_id=work.id,
                added_via=added_via,
                status=status,
                rank_reason={
                    "method": "verified_import",
                    "matched_by": result.matched_by,
                    "confidence": result.confidence,
                    "provider": result.candidate.provider_name,
                    "provider_record_id": result.candidate.provider_record_id,
                },
                verified=True,
            )
            # R3：核验入库时一次性生成并持久化 key；渲染期只消费。
            payload = await reference_metadata_payload(session, work)
            bibtex_key = await assign_bibtex_key(
                session,
                entry,
                make_bibtex_key,
                reference=ReferenceMetadata(**payload),
            )
            outcome.entries.append(
                {
                    "entry_id": str(entry.id),
                    "work_id": str(work.id),
                    "bibtex_key": bibtex_key,
                    "title": work.canonical_title,
                    "matched_by": result.matched_by,
                    "is_retracted": work.is_retracted,
                }
            )
        outcome.verified += 1
        await context.emit(
            "import.verified",
            {"index": index, "matched_by": result.matched_by, "title": result.candidate.title},
            stage="import",
        )
    return outcome
