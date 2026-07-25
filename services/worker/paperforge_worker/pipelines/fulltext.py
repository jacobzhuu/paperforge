"""INGEST 阶段：OA 全文获取 → 解析 → section 感知切块（设计 §4.4.1）。

合规红线（§1.4）：只走 OA/官方渠道给出的 URL，不绕 paywall。抓不到就摘要级降级——
卡片质量下降是可以接受的，绕过访问控制不是。

产物：`document_file` 行 + 对象存储里的原始字节；解析出的 section 感知块
供 CARDS 阶段把摘要级卡片升级为全文级卡片。
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from typing import Any

from db import get_work_urls, list_entries
from db.models.library import DocumentFile
from ingest import assess_chunk_quality, try_extract_and_chunk
from observability import get_logger
from scholar_gateway import (
    OaFulltextTarget,
    SafeHttpClient,
    acquire_oa_fulltext,
    plan_oa_fulltext,
)

from paperforge_worker.context import JobContext

logger = get_logger(__name__)

# 单次任务的全文抓取预算：按引文影响力挑选，避免为长尾文献消耗大量带宽。
DEFAULT_MAX_WORKS = 12
MAX_FULLTEXT_CHARS = 120_000


@dataclass
class FulltextOutcome:
    planned: int = 0
    acquired: int = 0
    parsed: int = 0
    failures: list[dict[str, Any]] = field(default_factory=list)
    coverage: float = 0.0

    def to_payload(self) -> dict[str, Any]:
        return {
            "planned": self.planned,
            "acquired": self.acquired,
            "parsed": self.parsed,
            "coverage": round(self.coverage, 4),
            "failures": self.failures[:10],
        }


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
        targets: list[OaFulltextTarget] = []
        for _entry, work in entries:
            links = await get_work_urls(session, work.id)
            targets.append(
                OaFulltextTarget(
                    work_id=str(work.id),
                    arxiv_id=work.arxiv_id,
                    pmcid=work.pmcid,
                    doi=work.doi,
                    links=tuple(links),
                )
            )
        selected_count = len(entries)

    if not targets:
        return outcome, texts

    plan = plan_oa_fulltext(targets, max_works=max_works)
    outcome.planned = len({c.work_id for c in plan.candidates})
    if not plan.candidates:
        return outcome, texts

    http = SafeHttpClient(
        user_agent=context.settings.user_agent(),
        timeout_seconds=context.settings.scholar_timeout_seconds * 2,
    )
    try:
        result = await asyncio.to_thread(acquire_oa_fulltext, plan, http_client=http)
    finally:
        http.close()

    outcome.acquired = len(result.documents)
    outcome.failures = list(result.failures)

    from storage import FilesystemObjectStore

    store = FilesystemObjectStore(context.settings.storage_fs_root)
    for document in result.documents:
        key = f"works/{document.work_id}/fulltext-{document.content_hash[:12]}.bin"
        store.put(key, document.content)
        async with context.session() as session:
            session.add(
                DocumentFile(
                    work_id=_as_uuid(document.work_id),
                    kind="oa_pdf" if document.mime_type == "application/pdf" else "html",
                    object_key=key,
                    mime=document.mime_type,
                    bytes=len(document.content),
                    fetched_from_url=document.url,
                )
            )
        parsed, chunks, error = try_extract_and_chunk(
            mime_type=document.mime_type,
            content=document.content,
        )
        if parsed is None:
            outcome.failures.append({"work_id": document.work_id, "reason": str(error)})
            continue
        # 只保留可用于卡片抽取的块：参考文献段与导航噪声不该占用长上下文预算。
        usable = [
            chunk.text
            for chunk in chunks
            if assess_chunk_quality(text=chunk.text).usable_for_cards
        ]
        text = "\n\n".join(usable)[:MAX_FULLTEXT_CHARS]
        if text:
            texts[document.work_id] = text
            outcome.parsed += 1

    outcome.coverage = outcome.parsed / selected_count if selected_count else 0.0
    await context.emit(
        "ingest.fulltext",
        outcome.to_payload(),
        stage="ingest",
    )
    return outcome, texts


def content_key(prefix: str, data: bytes) -> str:
    return f"{prefix}/{hashlib.sha256(data).hexdigest()[:16]}"


def _as_uuid(value: str):
    import uuid

    return uuid.UUID(value)
