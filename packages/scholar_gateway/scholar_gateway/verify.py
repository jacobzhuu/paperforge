"""R1 入库核验：DOI / 标题反查（设计 §4.4.3）。

`library_entry` 只能来自：
(a) provider 真实检索响应；(b) DOI/BibTeX 导入且经 Crossref/OpenAlex 反查核验；
(c) LLM 建议的文献，必须先反查（DOI 存在，或标题模糊匹配 ≥0.90 且作者/年份吻合）
并成功解析为 scholarly_work 才准入，失败即丢弃并记录 ``verification_failed``。

本模块只做「反查」，不做持久化：核验通过返回真实 provider 响应映射出的候选，
调用方据此写库；核验失败返回带原因的失败对象，绝不放宽。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from scholar_gateway.cache import HttpCacheBackend, NullHttpCache
from scholar_gateway.models import ScholarlyDiscoveryQuery, ScholarlyWorkCandidate
from scholar_gateway.normalize import (
    normalize_arxiv_id,
    normalize_doi,
    normalize_title_for_dedupe,
    token_set_jaccard,
)
from scholar_gateway.providers import ProviderConfig, build_adapter
from scholar_gateway.providers.mapping import (
    candidate_from_crossref_item,
    candidate_from_openalex_item,
)

# 标题模糊匹配阈值（设计 §4.4.3 明确要求 ≥0.90）。
TITLE_MATCH_THRESHOLD = 0.90
# 年份允许 ±1：预印本与正式发表常跨年。
YEAR_TOLERANCE = 1


@dataclass(frozen=True)
class VerificationRequest:
    """一条待核验的引用线索（来自 DOI 输入、.bib 条目或 LLM 建议）。"""

    doi: str | None = None
    title: str | None = None
    authors: tuple[str, ...] = ()
    publication_year: int | None = None
    arxiv_id: str | None = None
    source_label: str | None = None


@dataclass(frozen=True)
class VerificationResult:
    """核验结果。``candidate is None`` 即核验失败，调用方必须丢弃该条。"""

    request: VerificationRequest
    candidate: ScholarlyWorkCandidate | None
    matched_by: str | None = None
    confidence: float | None = None
    failure_reason: str | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)

    @property
    def verified(self) -> bool:
        return self.candidate is not None


def verify_reference(
    request: VerificationRequest,
    *,
    client: httpx.Client | None,
    config: ProviderConfig | None = None,
    cache: HttpCacheBackend | None = None,
) -> VerificationResult:
    """按 DOI → arXiv → 标题 的顺序反查；任一步命中即返回。"""
    config = config or ProviderConfig()
    cache = cache or NullHttpCache()
    if client is None:
        return VerificationResult(
            request=request,
            candidate=None,
            failure_reason="verification_unavailable",
            diagnostics={"reason": "no http client configured"},
        )

    doi = normalize_doi(request.doi)
    if doi:
        result = _verify_by_doi(doi, request, client=client, config=config, cache=cache)
        if result.verified:
            return result
        doi_failure = result
    else:
        doi_failure = None

    # arXiv 的 DataCite DOI（10.48550/arXiv.xxxx）不在 Crossref/OpenAlex 索引里，
    # 但 arXiv 官方 API 是同样权威的来源——补一条官方通道，不放宽核验标准。
    arxiv_id = normalize_arxiv_id(request.arxiv_id) or _arxiv_id_from_doi(doi)
    if arxiv_id:
        result = _verify_by_arxiv(arxiv_id, request, client=client, config=config, cache=cache)
        if result.verified:
            return result

    if request.title and request.title.strip():
        result = _verify_by_title(request, client=client, config=config, cache=cache)
        if result.verified:
            return result
        return result

    return doi_failure or VerificationResult(
        request=request,
        candidate=None,
        failure_reason="verification_failed",
        diagnostics={"reason": "no doi, arxiv id, or title to verify"},
    )


_ARXIV_DOI_RE = re.compile(r"^10\.48550/arxiv\.(?P<arxiv_id>.+)$", re.IGNORECASE)


def _arxiv_id_from_doi(doi: str | None) -> str | None:
    """从 arXiv 的 DataCite DOI 中取出 arXiv id。"""
    if not doi:
        return None
    match = _ARXIV_DOI_RE.match(doi.strip())
    return normalize_arxiv_id(match.group("arxiv_id")) if match else None


def _verify_by_doi(
    doi: str,
    request: VerificationRequest,
    *,
    client: httpx.Client,
    config: ProviderConfig,
    cache: HttpCacheBackend,
) -> VerificationResult:
    """DOI 精确反查：Crossref 权威，OpenAlex 兜底。"""
    crossref = _fetch_json(
        client,
        f"https://api.crossref.org/works/{doi}",
        params={"mailto": config.contact_email} if config.contact_email else None,
        config=config,
        cache=cache,
        provider_name="crossref",
    )
    if isinstance(crossref, dict):
        message = crossref.get("message")
        if isinstance(message, dict):
            candidate = candidate_from_crossref_item(
                _lookup_query(doi),
                message,
                datetime.now(UTC),
            )
            if candidate is not None:
                return VerificationResult(
                    request=request,
                    candidate=candidate,
                    matched_by="doi_crossref",
                    confidence=1.0,
                )

    openalex = _fetch_json(
        client,
        f"https://api.openalex.org/works/doi:{doi}",
        params={"mailto": config.contact_email} if config.contact_email else None,
        config=config,
        cache=cache,
        provider_name="openalex",
    )
    if isinstance(openalex, dict) and openalex.get("id"):
        candidate = candidate_from_openalex_item(
            _lookup_query(doi),
            openalex,
            datetime.now(UTC),
        )
        if candidate is not None:
            return VerificationResult(
                request=request,
                candidate=candidate,
                matched_by="doi_openalex",
                confidence=1.0,
            )

    return VerificationResult(
        request=request,
        candidate=None,
        failure_reason="verification_failed",
        diagnostics={"doi": doi, "reason": "doi not found in crossref or openalex"},
    )


def _verify_by_arxiv(
    arxiv_id: str,
    request: VerificationRequest,
    *,
    client: httpx.Client,
    config: ProviderConfig,
    cache: HttpCacheBackend,
) -> VerificationResult:
    adapter = build_adapter("arxiv", client=client, config=config, cache=cache)
    result = adapter.discover(ScholarlyDiscoveryQuery(query_text=f"id:{arxiv_id}", limit=5))
    for candidate in result.candidates:
        if candidate.arxiv_id == arxiv_id:
            return VerificationResult(
                request=request,
                candidate=candidate,
                matched_by="arxiv_id",
                confidence=1.0,
            )
    return VerificationResult(
        request=request,
        candidate=None,
        failure_reason="verification_failed",
        diagnostics={"arxiv_id": arxiv_id, "reason": "arxiv id not found"},
    )


def _verify_by_title(
    request: VerificationRequest,
    *,
    client: httpx.Client,
    config: ProviderConfig,
    cache: HttpCacheBackend,
) -> VerificationResult:
    """标题模糊匹配：相似度 ≥0.90 且作者或年份吻合才算核验通过。"""
    title = (request.title or "").strip()
    best: tuple[float, ScholarlyWorkCandidate] | None = None
    checked = 0
    for provider in ("crossref", "openalex"):
        adapter = build_adapter(provider, client=client, config=config, cache=cache)
        result = adapter.discover(ScholarlyDiscoveryQuery(query_text=title, limit=5))
        for candidate in result.candidates:
            checked += 1
            score = title_similarity(title, candidate.title)
            if best is None or score > best[0]:
                best = (score, candidate)
            if score < TITLE_MATCH_THRESHOLD:
                continue
            if not _metadata_agrees(request, candidate):
                continue
            return VerificationResult(
                request=request,
                candidate=candidate,
                matched_by=f"title_{provider}",
                confidence=round(score, 4),
            )
    return VerificationResult(
        request=request,
        candidate=None,
        failure_reason="verification_failed",
        diagnostics={
            "title": title,
            "checked_candidates": checked,
            "best_similarity": round(best[0], 4) if best else None,
            "reason": ("no candidate reached the 0.90 title threshold with agreeing author/year"),
        },
    )


def title_similarity(left: str | None, right: str | None) -> float:
    normalized_left = normalize_title_for_dedupe(left)
    normalized_right = normalize_title_for_dedupe(right)
    if not normalized_left or not normalized_right:
        return 0.0
    if normalized_left == normalized_right:
        return 1.0
    return token_set_jaccard(normalized_left, normalized_right)


def _metadata_agrees(
    request: VerificationRequest,
    candidate: ScholarlyWorkCandidate,
) -> bool:
    """作者或年份至少有一项对得上；两项都无从比较时不算核验通过。"""
    year_ok: bool | None = None
    if request.publication_year and candidate.publication_year:
        year_ok = abs(request.publication_year - candidate.publication_year) <= YEAR_TOLERANCE
        if not year_ok:
            return False

    author_ok: bool | None = None
    if request.authors and candidate.authors:
        wanted = {_surname(name) for name in request.authors if _surname(name)}
        have = {_surname(a.author_name) for a in candidate.authors if _surname(a.author_name)}
        if wanted and have:
            author_ok = bool(wanted & have)
            if not author_ok:
                return False

    return bool(year_ok or author_ok)


def _surname(name: str) -> str:
    text = (name or "").strip()
    if not text:
        return ""
    surname = text.split(",")[0] if "," in text else text.split()[-1]
    return "".join(ch for ch in surname.lower() if ch.isalpha())


def _lookup_query(text: str) -> ScholarlyDiscoveryQuery:
    return ScholarlyDiscoveryQuery(query_text=text, limit=1)


def _fetch_json(
    client: httpx.Client,
    url: str,
    *,
    params: dict[str, Any] | None,
    config: ProviderConfig,
    cache: HttpCacheBackend,
    provider_name: str,
) -> Any:
    """单条反查 GET：走同一缓存后端，失败返回 None（调用方判定核验失败）。"""
    clean_params = {k: v for k, v in (params or {}).items() if v}
    cached = cache.get(url=url, params=clean_params, ttl_hours=config.cache_ttl_hours)
    if cached is not None:
        try:
            import json

            return json.loads(cached.text)
        except ValueError:
            return None
    headers = {"User-Agent": config.user_agent} if config.user_agent else {}
    try:
        response = client.get(
            url,
            params=clean_params or None,
            headers=headers,
            timeout=config.timeout_seconds,
        )
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    cache.put(
        url=url,
        params=clean_params,
        status_code=response.status_code,
        content_type=response.headers.get("content-type"),
        body=response.content,
        provider_name=provider_name,
    )
    try:
        return response.json()
    except ValueError:
        return None
