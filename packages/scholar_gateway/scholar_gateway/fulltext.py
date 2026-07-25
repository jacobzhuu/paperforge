"""OA 全文获取（规划 + 合规抓取）。

迁移自 DeepSearch literature_review/oa_fulltext.py（设计 §3.1）。改动：
彻底解耦 ledger 写入链（search_query→candidate_url→fetch_job→content_snapshot→source_chunk），
只产出「候选 URL 规划」与「字节 + mime」；持久化由 worker 写 ``document_file`` + 对象存储。

合规红线（设计 §1.4 / §7）：只走 OA/官方渠道给出的 URL（work_url 的 pdf 链接、
arXiv PDF、Europe PMC render），不提供任何绕 paywall 能力。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from scholar_gateway.http import HttpFetchResult, SafeHttpClient

# 单篇最多尝试的 OA URL 数：够覆盖 primary/best-OA/arXiv/PMC，又不至于无界重试。
MAX_URLS_PER_WORK = 3
PDF_MIME_TYPES = frozenset({"application/pdf", "application/x-pdf"})
FULLTEXT_MIME_TYPES = PDF_MIME_TYPES | frozenset({"text/html", "application/xml", "text/xml"})


@dataclass(frozen=True)
class OaFulltextTarget:
    """一篇待获取全文的文献（解耦 ORM：只要标识符与已知链接）。"""

    work_id: str
    arxiv_id: str | None = None
    pmcid: str | None = None
    doi: str | None = None
    links: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class OaFulltextCandidate:
    work_id: str
    url: str
    source: str
    url_type: str = "pdf"


@dataclass(frozen=True)
class OaFulltextPlan:
    candidates: tuple[OaFulltextCandidate, ...]
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def to_event_payload(self) -> dict[str, Any]:
        return {
            "candidate_count": len(self.candidates),
            "diagnostics": self.diagnostics,
            "candidates": [
                {
                    "work_id": item.work_id,
                    "url": item.url,
                    "source": item.source,
                    "url_type": item.url_type,
                }
                for item in self.candidates
            ],
        }


@dataclass(frozen=True)
class OaFulltextDocument:
    work_id: str
    url: str
    source: str
    mime_type: str
    content: bytes
    content_hash: str


@dataclass(frozen=True)
class OaFulltextAcquisitionResult:
    documents: tuple[OaFulltextDocument, ...] = ()
    failures: tuple[dict[str, Any], ...] = ()

    def to_event_payload(self) -> dict[str, Any]:
        return {
            "acquired_count": len(self.documents),
            "failure_count": len(self.failures),
            "acquired_work_ids": [doc.work_id for doc in self.documents],
            "failures": list(self.failures),
        }


def plan_oa_fulltext(
    targets: list[OaFulltextTarget],
    *,
    max_works: int = 12,
) -> OaFulltextPlan:
    """为有界的入库文献子集挑选 OA PDF/全文 URL。"""
    selected = targets[: max(0, max_works)]
    candidates: list[OaFulltextCandidate] = []
    skipped: list[dict[str, Any]] = []

    for target in selected:
        resolved = resolve_oa_urls(target)
        if not resolved:
            skipped.append({"work_id": target.work_id, "reason": "no_oa_pdf_candidate"})
            continue
        candidates.extend(resolved[:MAX_URLS_PER_WORK])

    return OaFulltextPlan(
        candidates=tuple(candidates),
        diagnostics={
            "planned_work_count": len(selected),
            "resolved_count": len({item.work_id for item in candidates}),
            "candidate_url_count": len(candidates),
            "skipped": skipped,
            "status": "planned",
        },
    )


def resolve_oa_urls(target: OaFulltextTarget) -> list[OaFulltextCandidate]:
    """返回有序的 OA 全文 URL 候选。

    顺序：持久化的 pdf work_url（入库顺序）→ arXiv PDF → Europe PMC render。
    """
    candidates: list[OaFulltextCandidate] = []
    seen: set[str] = set()

    def _add(url: str, *, source: str, url_type: str = "pdf") -> None:
        normalized = url.strip()
        if not normalized or normalized in seen:
            return
        seen.add(normalized)
        candidates.append(
            OaFulltextCandidate(
                work_id=target.work_id,
                url=normalized,
                source=source,
                url_type=url_type,
            )
        )

    for link in target.links:
        url = str(link.get("url") or "").strip()
        url_type = str(link.get("url_type") or "").strip().lower()
        source = str(link.get("source_name") or "work_url").strip() or "work_url"
        if url and url_type == "pdf":
            _add(url, source=source, url_type="pdf")

    if target.arxiv_id and target.arxiv_id.strip():
        _add(
            f"https://arxiv.org/pdf/{target.arxiv_id.strip()}.pdf",
            source="arxiv",
        )

    if target.pmcid and target.pmcid.strip():
        pmc = target.pmcid.strip()
        if not pmc.upper().startswith("PMC"):
            pmc = f"PMC{pmc}"
        _add(f"https://europepmc.org/articles/{pmc}?pdf=render", source="europe_pmc")

    return candidates


def acquire_oa_fulltext(
    plan: OaFulltextPlan,
    *,
    http_client: SafeHttpClient,
) -> OaFulltextAcquisitionResult:
    """按规划抓取全文；每篇最多试 MAX_URLS_PER_WORK 个 URL，首个成功即停。

    Draft-first：任一篇失败只记 failure，不影响其他文献，也绝不抛出。
    """
    candidates_by_work: dict[str, list[OaFulltextCandidate]] = {}
    for candidate in plan.candidates:
        candidates_by_work.setdefault(candidate.work_id, []).append(candidate)

    documents: list[OaFulltextDocument] = []
    failures: list[dict[str, Any]] = []

    for work_id, work_candidates in candidates_by_work.items():
        acquired = False
        for candidate in work_candidates[:MAX_URLS_PER_WORK]:
            result = _fetch(http_client, candidate)
            if result is None:
                continue
            documents.append(result)
            acquired = True
            break
        if not acquired:
            failures.append({"work_id": work_id, "reason": "all_oa_urls_failed"})

    return OaFulltextAcquisitionResult(tuple(documents), tuple(failures))


def _fetch(
    http_client: SafeHttpClient,
    candidate: OaFulltextCandidate,
) -> OaFulltextDocument | None:
    try:
        result: HttpFetchResult = http_client.fetch(
            candidate.url,
            accept="application/pdf,text/html;q=0.8,*/*;q=0.5",
        )
    except Exception:  # noqa: BLE001 - 抓取是尽力而为
        return None
    if not result.ok or result.content is None or result.content_hash is None:
        return None
    mime_type = result.mime_type or "application/octet-stream"
    if mime_type not in FULLTEXT_MIME_TYPES and not _looks_like_pdf(result.content):
        return None
    if _looks_like_pdf(result.content):
        mime_type = "application/pdf"
    return OaFulltextDocument(
        work_id=candidate.work_id,
        url=result.final_url or candidate.url,
        source=candidate.source,
        mime_type=mime_type,
        content=result.content,
        content_hash=result.content_hash,
    )


def _looks_like_pdf(content: bytes) -> bool:
    return content[:5] == b"%PDF-"
