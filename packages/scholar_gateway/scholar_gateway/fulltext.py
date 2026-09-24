"""OA 全文获取（规划 + 合规抓取）。

迁移自 DeepSearch literature_review/oa_fulltext.py（设计 §3.1）。改动：
彻底解耦 ledger 写入链（search_query→candidate_url→fetch_job→content_snapshot→source_chunk），
只产出「候选 URL 规划」与「字节 + mime」；持久化由 worker 写 ``document_file`` + 对象存储。

合规红线（设计 §1.4 / §7）：只走 OA/官方渠道给出的 URL（work_url 的 pdf 链接、
arXiv PDF、Europe PMC render），不提供任何绕 paywall 能力。
"""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlsplit

from scholar_gateway.http import HttpFetchResult, SafeHttpClient

# 单篇最多尝试的 OA URL 数：够覆盖 primary/best-OA/arXiv/PMC，又不至于无界重试。
MAX_URLS_PER_WORK = 5
PDF_MIME_TYPES = frozenset({"application/pdf", "application/x-pdf"})
FULLTEXT_MIME_TYPES = PDF_MIME_TYPES | frozenset(
    {
        "text/html",
        "application/xhtml+xml",
        "application/xml",
        "text/xml",
        "application/jats+xml",
        "application/x-arxiv-source",
    }
)


@dataclass(frozen=True)
class OaFulltextTarget:
    """一篇待获取全文的文献（解耦 ORM：只要标识符与已知链接）。"""

    work_id: str
    arxiv_id: str | None = None
    pmcid: str | None = None
    doi: str | None = None
    links: tuple[dict[str, Any], ...] = ()
    relevance_score: float | None = None
    influential_citation_count: int | None = None


@dataclass(frozen=True)
class OaFulltextCandidate:
    work_id: str
    url: str
    source: str
    url_type: str = "pdf"
    license: str | None = None


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
    license: str | None = None


@dataclass(frozen=True)
class OaFulltextAttempt:
    """One URL-level acquisition attempt for the durable retry/audit ledger."""

    work_id: str
    url: str
    source: str
    status: str
    http_status: int | None = None
    mime_type: str | None = None
    error_code: str | None = None
    error_detail: str | None = None


@dataclass(frozen=True)
class OaFulltextAcquisitionResult:
    documents: tuple[OaFulltextDocument, ...] = ()
    failures: tuple[dict[str, Any], ...] = ()
    attempts: tuple[OaFulltextAttempt, ...] = ()

    def to_event_payload(self) -> dict[str, Any]:
        return {
            "acquired_count": len(self.documents),
            "failure_count": len(self.failures),
            "acquired_work_ids": [doc.work_id for doc in self.documents],
            "failures": list(self.failures),
            "attempts": [
                {
                    "work_id": attempt.work_id,
                    "url": attempt.url,
                    "source": attempt.source,
                    "status": attempt.status,
                    "http_status": attempt.http_status,
                    "mime_type": attempt.mime_type,
                    "error_code": attempt.error_code,
                    "error_detail": attempt.error_detail,
                }
                for attempt in self.attempts
            ],
        }


def plan_oa_fulltext(
    targets: list[OaFulltextTarget],
    *,
    max_works: int = 40,
) -> OaFulltextPlan:
    """为有界的入库文献子集挑选 OA PDF/全文 URL。"""
    selected = sorted(
        targets,
        key=lambda target: (
            target.relevance_score is not None,
            target.relevance_score or 0.0,
            target.influential_citation_count is not None,
            target.influential_citation_count or 0,
        ),
        reverse=True,
    )[: max(0, max_works)]
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

    顺序：PMC JATS / arXiv 源码 → 已知 OA 链接 → arXiv/PMC PDF。
    """
    candidates: list[OaFulltextCandidate] = []
    seen: set[str] = set()

    def _add(
        url: str,
        *,
        source: str,
        url_type: str = "pdf",
        license: str | None = None,
    ) -> None:
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
                license=license,
            )
        )

    if target.pmcid and target.pmcid.strip():
        pmc = target.pmcid.strip()
        numeric_pmc = pmc[3:] if pmc.upper().startswith("PMC") else pmc
        _add(
            "https://pmc.ncbi.nlm.nih.gov/api/oai/v1/mh/"
            f"?verb=GetRecord&identifier=oai:pubmedcentral.nih.gov:{numeric_pmc}"
            "&metadataPrefix=pmc",
            source="pmc_oai_jats",
            url_type="jats",
        )

    if target.arxiv_id and target.arxiv_id.strip():
        _add(
            f"https://arxiv.org/e-print/{target.arxiv_id.strip()}",
            source="arxiv_source",
            url_type="latex_source",
        )

    for link in target.links:
        url = str(link.get("url") or "").strip()
        url_type = str(link.get("url_type") or "").strip().lower()
        source = str(link.get("source_name") or "work_url").strip() or "work_url"
        if (
            url
            and bool(link.get("is_oa"))
            and url_type in {"pdf", "html", "fulltext", "jats", "latex_source"}
        ):
            _add(
                url,
                source=source,
                url_type="html" if url_type == "fulltext" else url_type,
                license=str(link.get("license") or "").strip() or None,
            )

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


def discover_unpaywall_links(
    target: OaFulltextTarget,
    *,
    http_client: SafeHttpClient,
    contact_email: str,
) -> tuple[dict[str, Any], ...]:
    """通过 Unpaywall v2 补充 best/all OA locations；无邮箱时按官方要求禁用。"""
    doi = (target.doi or "").strip()
    email = contact_email.strip()
    if not doi or not email:
        return ()
    url = f"https://api.unpaywall.org/v2/{quote(doi, safe='')}?email={quote(email, safe='@')}"
    try:
        result = http_client.fetch(url, accept="application/json")
    except Exception:  # noqa: BLE001 - OA 补充源失败不阻断
        return ()
    if not result.ok or not result.content:
        return ()
    try:
        payload = json.loads(result.content)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
        return ()
    if not isinstance(payload, dict):
        return ()
    locations = [
        payload.get("best_oa_location"),
        *(payload.get("oa_locations") or []),
    ]
    links: list[dict[str, Any]] = []
    seen: set[str] = set()
    for location in locations:
        if not isinstance(location, dict):
            continue
        location_url = str(
            location.get("url_for_pdf")
            or location.get("url")
            or location.get("url_for_landing_page")
            or ""
        ).strip()
        if not location_url or location_url in seen:
            continue
        seen.add(location_url)
        links.append(
            {
                "url": location_url,
                "url_type": "pdf" if location.get("url_for_pdf") else "html",
                "source_name": "unpaywall",
                "is_oa": True,
                "license": location.get("license"),
            }
        )
    return tuple(links[:MAX_URLS_PER_WORK])


def discover_doaj_links(
    target: OaFulltextTarget,
    *,
    http_client: SafeHttpClient,
) -> tuple[dict[str, Any], ...]:
    """从 DOAJ v4 文章元数据读取出版方提交的 full-text URL。"""
    doi = (target.doi or "").strip()
    if not doi:
        return ()
    query = quote(f"bibjson.identifier.id:{doi}", safe="")
    url = f"https://doaj.org/api/v4/search/articles/{query}?pageSize=1"
    try:
        result = http_client.fetch(url, accept="application/json")
    except Exception:  # noqa: BLE001 - OA 补充源失败不阻断
        return ()
    if not result.ok or not result.content:
        return ()
    try:
        payload = json.loads(result.content)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
        return ()
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list) or not results:
        return ()
    bibjson = results[0].get("bibjson") if isinstance(results[0], dict) else None
    if not isinstance(bibjson, dict):
        return ()
    licenses = bibjson.get("license") or []
    license_name = next(
        (
            str(item.get("title") or item.get("type") or "").strip()
            for item in licenses
            if isinstance(item, dict) and (item.get("title") or item.get("type"))
        ),
        None,
    )
    links: list[dict[str, Any]] = []
    for item in bibjson.get("link") or []:
        if not isinstance(item, dict):
            continue
        link_type = str(item.get("type") or "").casefold()
        content_type = str(item.get("content_type") or "").casefold()
        url_value = str(item.get("url") or "").strip()
        if not url_value or ("fulltext" not in link_type and "pdf" not in content_type):
            continue
        links.append(
            {
                "url": url_value,
                "url_type": "pdf" if "pdf" in content_type else "html",
                "source_name": "doaj",
                "is_oa": True,
                "license": license_name,
            }
        )
    return tuple(links[:MAX_URLS_PER_WORK])


def acquire_oa_fulltext(
    plan: OaFulltextPlan,
    *,
    http_client: SafeHttpClient,
    concurrency: int = 1,
    per_host_concurrency: int = 1,
) -> OaFulltextAcquisitionResult:
    """按规划抓取全文；每篇最多试 MAX_URLS_PER_WORK 个 URL，首个成功即停。

    Draft-first：任一篇失败只记 failure，不影响其他文献，也绝不抛出。

    ``concurrency`` 是**按文献**的并发度。两条边界都不可谈判：

    * **每篇内部的 URL 回退保持串行。** ``MAX_URLS_PER_WORK`` 的「首个成功即停」是一个
      回退**顺序**（OA 正本优先于落地页），并发发出会改变最终留下哪一个 URL，
      还白花 5 倍带宽。
    * **同一 host 默认仍然串行**（``per_host_concurrency``）。``SafeHttpClient`` 自己
      完全没有限速，而一次抓取计划高度集中在少数几个 host（arXiv/PMC/DOAJ/出版商
      落地页）。无界扇出会被限流甚至封禁——那不是「慢一点」，那是全文变少、
      卡片变薄，也就是**质量退化**。

    返回顺序与串行时一致：结果按 work 在计划中出现的顺序展平。``attempts`` 会被
    调用方按这个顺序写成 ``fulltext_attempt`` 行，完成序会让那张表变得不稳定。
    """
    candidates_by_work: dict[str, list[OaFulltextCandidate]] = {}
    for candidate in plan.candidates:
        candidates_by_work.setdefault(candidate.work_id, []).append(candidate)

    work_ids = list(candidates_by_work)
    if not work_ids:
        return OaFulltextAcquisitionResult((), (), ())

    # host 信号量预先建好——懒建会让字典本身成为并发写入点。
    host_gates: dict[str, threading.Semaphore] = {}
    per_host = max(1, int(per_host_concurrency))
    for candidates in candidates_by_work.values():
        for candidate in candidates[:MAX_URLS_PER_WORK]:
            host = (urlsplit(candidate.url).hostname or "").casefold()
            if host not in host_gates:
                host_gates[host] = threading.Semaphore(per_host)

    def _acquire_one(
        work_id: str,
    ) -> tuple[OaFulltextDocument | None, list[OaFulltextAttempt]]:
        work_attempts: list[OaFulltextAttempt] = []
        for candidate in candidates_by_work[work_id][:MAX_URLS_PER_WORK]:
            host = (urlsplit(candidate.url).hostname or "").casefold()
            with host_gates[host]:
                result, attempt = _fetch(http_client, candidate)
            work_attempts.append(attempt)
            if result is not None:
                return result, work_attempts
        return None, work_attempts

    workers = max(1, min(int(concurrency), len(work_ids)))
    if workers == 1:
        per_work = [_acquire_one(work_id) for work_id in work_ids]
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            # `map` 按输入序返回，所以下面的展平与串行执行逐项相同。
            per_work = list(pool.map(_acquire_one, work_ids))

    documents: list[OaFulltextDocument] = []
    failures: list[dict[str, Any]] = []
    attempts: list[OaFulltextAttempt] = []
    for work_id, (document, work_attempts) in zip(work_ids, per_work, strict=True):
        attempts.extend(work_attempts)
        if document is None:
            failures.append({"work_id": work_id, "reason": "all_oa_urls_failed"})
        else:
            documents.append(document)

    return OaFulltextAcquisitionResult(tuple(documents), tuple(failures), tuple(attempts))


def _fetch(
    http_client: SafeHttpClient,
    candidate: OaFulltextCandidate,
) -> tuple[OaFulltextDocument | None, OaFulltextAttempt]:
    try:
        result: HttpFetchResult = http_client.fetch(
            candidate.url,
            accept="application/pdf,text/html;q=0.8,*/*;q=0.5",
        )
    except Exception:  # noqa: BLE001 - 抓取是尽力而为
        return None, OaFulltextAttempt(
            work_id=candidate.work_id,
            url=candidate.url,
            source=candidate.source,
            status="failed",
            error_code="fetch_exception",
        )
    if not result.ok or result.content is None or result.content_hash is None:
        return None, OaFulltextAttempt(
            work_id=candidate.work_id,
            url=candidate.url,
            source=candidate.source,
            status="failed",
            http_status=result.http_status,
            mime_type=result.mime_type,
            error_code=result.error_code or "empty_response",
        )
    mime_type = result.mime_type or "application/octet-stream"
    if candidate.url_type == "latex_source" and _looks_like_archive(result.content):
        mime_type = "application/x-arxiv-source"
    elif candidate.url_type == "jats" and _looks_like_xml(result.content):
        mime_type = "application/jats+xml"
    if mime_type not in FULLTEXT_MIME_TYPES and not _looks_like_pdf(result.content):
        return None, OaFulltextAttempt(
            work_id=candidate.work_id,
            url=candidate.url,
            source=candidate.source,
            status="rejected",
            http_status=result.http_status,
            mime_type=mime_type,
            error_code="not_fulltext",
            error_detail="response is neither a PDF nor a supported full-text source",
        )
    if _looks_like_pdf(result.content):
        mime_type = "application/pdf"
    document = OaFulltextDocument(
        work_id=candidate.work_id,
        url=result.final_url or candidate.url,
        source=candidate.source,
        mime_type=mime_type,
        content=result.content,
        content_hash=result.content_hash,
        license=candidate.license,
    )
    return document, OaFulltextAttempt(
        work_id=candidate.work_id,
        url=candidate.url,
        source=candidate.source,
        status="acquired",
        http_status=result.http_status,
        mime_type=mime_type,
    )


def _looks_like_pdf(content: bytes) -> bool:
    return content[:5] == b"%PDF-"


def _looks_like_xml(content: bytes) -> bool:
    # bytes has lower(), not str.casefold(). Valid JATS used to crash here.
    prefix = content.lstrip()[:200].lower()
    return prefix.startswith(b"<?xml") or b"<article" in prefix or b"<oai-pmh" in prefix


def _looks_like_archive(content: bytes) -> bool:
    return content.startswith(b"\x1f\x8b") or (len(content) > 265 and content[257:262] == b"ustar")
