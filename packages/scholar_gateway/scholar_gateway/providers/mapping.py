"""provider 响应 → ``ScholarlyWorkCandidate`` 的确定性映射。

迁移自 DeepSearch literature_review/adapters.py 的 ``_candidate_from_*`` 系列
与字段清洗辅助函数（设计 §3.1）。改动：identifier/link/author 字段名对齐
PaperForge 模型（url_type / source_name / raw_affiliation）；OA PDF 链接标记 is_oa，
供 fulltext.py 规划合规抓取。
"""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any
from urllib.parse import quote

from scholar_gateway.models import (
    ScholarlyAuthorCandidate,
    ScholarlyDiscoveryQuery,
    ScholarlyIdentifier,
    ScholarlyLinkCandidate,
    ScholarlyWorkCandidate,
)
from scholar_gateway.normalize import (
    normalize_arxiv_id,
    normalize_corpus_id,
    normalize_doi,
    normalize_openalex_id,
    normalize_pmcid,
    normalize_pmid,
    normalize_semantic_scholar_id,
    normalize_title_for_dedupe,
    normalized_title_hash,
)

ATOM_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "opensearch": "http://a9.com/-/spec/opensearch/1.1/",
}


def candidate(
    *,
    query: ScholarlyDiscoveryQuery,
    title: str,
    normalized_title: str,
    normalized_title_hash: str,
    provider_name: str,
    provider_record_id: str | None,
    provider_record_url: str | None,
    retrieved_at: datetime,
    identifiers: tuple[ScholarlyIdentifier, ...],
    links: tuple[ScholarlyLinkCandidate, ...],
    authors: tuple[ScholarlyAuthorCandidate, ...],
    raw_provider_metadata: dict[str, Any],
    abstract: str | None = None,
    publication_year: int | None = None,
    publication_date: str | None = None,
    work_type: str | None = None,
    venue_name: str | None = None,
    publisher: str | None = None,
    language: str | None = None,
    doi: str | None = None,
    pmid: str | None = None,
    pmcid: str | None = None,
    arxiv_id: str | None = None,
    openalex_id: str | None = None,
    semantic_scholar_id: str | None = None,
    corpus_id: str | None = None,
    oa_status: str | None = None,
    license: str | None = None,
    is_retracted: bool = False,
    citation_count: int | None = None,
    influential_citation_count: int | None = None,
) -> ScholarlyWorkCandidate:
    return ScholarlyWorkCandidate(
        title=title,
        normalized_title=normalized_title,
        normalized_title_hash=normalized_title_hash,
        provider_name=provider_name,
        provider_record_id=provider_record_id,
        provider_record_url=provider_record_url,
        query_text=query.query_text,
        retrieved_at=retrieved_at,
        source_strategy_id=query.source_strategy_id,
        abstract=abstract,
        publication_year=publication_year,
        publication_date=publication_date,
        work_type=work_type,
        venue_name=venue_name,
        publisher=publisher,
        language=language,
        doi=doi,
        pmid=pmid,
        pmcid=pmcid,
        arxiv_id=arxiv_id,
        openalex_id=openalex_id,
        semantic_scholar_id=semantic_scholar_id,
        corpus_id=corpus_id,
        oa_status=oa_status,
        license=license,
        is_retracted=is_retracted,
        citation_count=citation_count,
        influential_citation_count=influential_citation_count,
        identifiers=identifiers,
        links=links,
        authors=authors,
        raw_provider_metadata=raw_provider_metadata,
    )


def identifiers(
    *,
    doi: str | None = None,
    pmid: str | None = None,
    pmcid: str | None = None,
    arxiv_id: str | None = None,
    openalex_id: str | None = None,
    semantic_scholar_id: str | None = None,
    corpus_id: str | None = None,
) -> tuple[ScholarlyIdentifier, ...]:
    values = (
        ("doi", doi, True),
        ("pmid", pmid, False),
        ("pmcid", pmcid, False),
        ("arxiv", arxiv_id, False),
        ("openalex", openalex_id, False),
        ("semantic_scholar", semantic_scholar_id, False),
        ("corpus_id", corpus_id, False),
    )
    return tuple(
        ScholarlyIdentifier(id_type=id_type, id_value=value, is_primary=is_primary)
        for id_type, value, is_primary in values
        if value
    )


def links(values: tuple[tuple[str | None, str, str], ...]) -> tuple[ScholarlyLinkCandidate, ...]:
    """去重保序地构造链接；``pdf`` 类型即视为可直接获取的 OA 全文入口。"""
    seen: set[str] = set()
    result: list[ScholarlyLinkCandidate] = []
    for url, url_type, source_name in values:
        if not url or url in seen:
            continue
        seen.add(url)
        result.append(
            ScholarlyLinkCandidate(
                url=url,
                url_type=url_type,
                source_name=source_name,
                is_oa=url_type == "pdf",
            )
        )
    return tuple(result)


def first_string(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def first_list_string(value: object) -> str | None:
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str) and item.strip():
                return item.strip()
    return None


def clean_markup(value: str | None) -> str | None:
    if not value:
        return None
    text = re.sub(r"<[^>]+>", " ", value)
    text = " ".join(html.unescape(text).split())
    return text or None


def int_or_none(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        return None
    try:
        return int(value)
    except ValueError:
        return None


def year_from_date(value: str | None) -> int | None:
    if not value or len(value) < 4:
        return None
    return int_or_none(value[:4])


def doi_url(doi: str | None) -> str | None:
    return f"https://doi.org/{quote(doi, safe='/')}" if doi else None


def dict_without(payload: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if key not in keys}


# ---------------- Crossref ----------------


def crossref_authors(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    authors: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        name = " ".join(
            part
            for part in (first_string(item.get("given")), first_string(item.get("family")))
            if part
        ) or first_string(item.get("name"))
        if name:
            authors.append(name)
    return authors


def crossref_date(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    parts = value.get("date-parts")
    if not isinstance(parts, list) or not parts:
        return None
    first_parts = parts[0]
    if not isinstance(first_parts, list) or not first_parts:
        return None
    try:
        year = int(first_parts[0])
        month = int(first_parts[1]) if len(first_parts) > 1 else 1
        day = int(first_parts[2]) if len(first_parts) > 2 else 1
    except (TypeError, ValueError):
        return None
    return f"{year:04d}-{month:02d}-{day:02d}"


def crossref_links(value: object) -> tuple[tuple[str, str | None], ...]:
    if not isinstance(value, list):
        return ()
    result: list[tuple[str, str | None]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        url = first_string(item.get("URL"))
        if url:
            result.append((url, first_string(item.get("content-type"))))
    return tuple(result)


def candidate_from_crossref_item(
    query: ScholarlyDiscoveryQuery,
    item: dict[str, Any],
    retrieved_at: datetime,
) -> ScholarlyWorkCandidate | None:
    title = first_list_string(item.get("title")) or first_string(item.get("title"))
    normalized_title = normalize_title_for_dedupe(title)
    title_hash = normalized_title_hash(title)
    if not title or normalized_title is None or title_hash is None:
        return None
    doi = normalize_doi(first_string(item.get("DOI")))
    publication_date = (
        crossref_date(item.get("published-print"))
        or crossref_date(item.get("published-online"))
        or crossref_date(item.get("issued"))
    )
    authors = tuple(
        ScholarlyAuthorCandidate(author_name=name, author_order=index)
        for index, name in enumerate(crossref_authors(item.get("author")), start=1)
    )
    return candidate(
        query=query,
        title=title,
        normalized_title=normalized_title,
        normalized_title_hash=title_hash,
        provider_name="crossref",
        provider_record_id=doi,
        provider_record_url=first_string(item.get("URL")) or doi_url(doi),
        retrieved_at=retrieved_at,
        abstract=clean_markup(first_string(item.get("abstract"))),
        publication_year=year_from_date(publication_date),
        publication_date=publication_date,
        work_type=first_string(item.get("type")),
        venue_name=first_list_string(item.get("container-title")),
        publisher=first_string(item.get("publisher")),
        language=first_string(item.get("language")),
        doi=doi,
        # Crossref 的 update-to 携带撤稿关系；标记后 R1 白名单会拒绝该文献。
        is_retracted=crossref_is_retracted(item),
        citation_count=int_or_none(item.get("is-referenced-by-count")),
        identifiers=identifiers(doi=doi),
        links=links(
            (
                (first_string(item.get("URL")), "landing_page", "crossref"),
                *(
                    (url, "pdf" if content_type == "application/pdf" else "html", "crossref")
                    for url, content_type in crossref_links(item.get("link"))
                ),
            )
        ),
        authors=authors,
        raw_provider_metadata=item,
    )


def crossref_is_retracted(item: dict[str, Any]) -> bool:
    """Crossref ``update-to`` 中的 retraction/withdrawal 关系即视为撤稿。"""
    updates = item.get("update-to")
    if not isinstance(updates, list):
        return False
    for update in updates:
        if not isinstance(update, dict):
            continue
        label = str(update.get("type") or update.get("label") or "").lower()
        if "retract" in label or "withdraw" in label:
            return True
    return False


# ---------------- OpenAlex ----------------


def openalex_source(item: dict[str, Any]) -> str | None:
    primary_location = item.get("primary_location")
    if isinstance(primary_location, dict):
        source = primary_location.get("source")
        if isinstance(source, dict):
            return first_string(source.get("display_name"))
    host_venue = item.get("host_venue")
    if isinstance(host_venue, dict):
        return first_string(host_venue.get("display_name"))
    return None


def openalex_urls(item: dict[str, Any]) -> tuple[str | None, tuple[str, ...]]:
    """返回落地页 URL 与去重后的 PDF URL（primary → best OA 顺序）。"""
    landing: str | None = None
    pdfs: list[str] = []
    seen: set[str] = set()

    def _add_pdf(url: str | None) -> None:
        if not url or url in seen:
            return
        seen.add(url)
        pdfs.append(url)

    primary_location = item.get("primary_location")
    if isinstance(primary_location, dict):
        landing = first_string(primary_location.get("landing_page_url"))
        _add_pdf(first_string(primary_location.get("pdf_url")))

    best_oa = item.get("best_oa_location")
    if isinstance(best_oa, dict):
        if landing is None:
            landing = first_string(best_oa.get("landing_page_url"))
        _add_pdf(first_string(best_oa.get("pdf_url")))

    return landing, tuple(pdfs)


def openalex_authors(value: object) -> tuple[tuple[str, str | None], ...]:
    if not isinstance(value, list):
        return ()
    authors: list[tuple[str, str | None]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        author = item.get("author")
        if not isinstance(author, dict):
            continue
        name = first_string(author.get("display_name"))
        if not name:
            continue
        institutions = item.get("institutions")
        affiliation = None
        if isinstance(institutions, list) and institutions:
            first = institutions[0]
            if isinstance(first, dict):
                affiliation = first_string(first.get("display_name"))
        authors.append((name, affiliation))
    return tuple(authors)


def openalex_abstract(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    positions: dict[int, str] = {}
    for word, raw_indexes in value.items():
        if not isinstance(word, str) or not isinstance(raw_indexes, list):
            continue
        for raw_index in raw_indexes:
            if isinstance(raw_index, int):
                positions[raw_index] = word
    if not positions:
        return None
    return " ".join(positions[index] for index in sorted(positions))


def candidate_from_openalex_item(
    query: ScholarlyDiscoveryQuery,
    item: dict[str, Any],
    retrieved_at: datetime,
) -> ScholarlyWorkCandidate | None:
    title = first_string(item.get("display_name")) or first_string(item.get("title"))
    normalized_title = normalize_title_for_dedupe(title)
    title_hash = normalized_title_hash(title)
    if not title or normalized_title is None or title_hash is None:
        return None
    raw_ids = item.get("ids")
    ids: dict[str, Any] = raw_ids if isinstance(raw_ids, dict) else {}
    doi = normalize_doi(first_string(item.get("doi")) or first_string(ids.get("doi")))
    openalex_id = normalize_openalex_id(
        first_string(item.get("id")) or first_string(ids.get("openalex"))
    )
    pmid = normalize_pmid(first_string(ids.get("pmid")))
    pmcid = normalize_pmcid(first_string(ids.get("pmcid")))
    raw_open_access = item.get("open_access")
    open_access: dict[str, Any] = raw_open_access if isinstance(raw_open_access, dict) else {}
    authors = tuple(
        ScholarlyAuthorCandidate(
            author_name=name,
            author_order=index,
            raw_affiliation=affiliation,
        )
        for index, (name, affiliation) in enumerate(
            openalex_authors(item.get("authorships")), start=1
        )
    )
    landing_url, pdf_urls = openalex_urls(item)
    best_oa = item.get("best_oa_location")
    license_value = first_string(best_oa.get("license")) if isinstance(best_oa, dict) else None
    return candidate(
        query=query,
        title=title,
        normalized_title=normalized_title,
        normalized_title_hash=title_hash,
        provider_name="openalex",
        provider_record_id=openalex_id,
        provider_record_url=first_string(item.get("id")),
        retrieved_at=retrieved_at,
        abstract=openalex_abstract(item.get("abstract_inverted_index")),
        publication_year=int_or_none(item.get("publication_year")),
        publication_date=first_string(item.get("publication_date")),
        work_type=first_string(item.get("type")),
        venue_name=openalex_source(item),
        doi=doi,
        pmid=pmid,
        pmcid=pmcid,
        openalex_id=openalex_id,
        oa_status=first_string(open_access.get("oa_status")),
        license=license_value,
        # OpenAlex 直接暴露撤稿标记（设计 §8：撤稿文献默认不入写作白名单）。
        is_retracted=bool(item.get("is_retracted")),
        citation_count=int_or_none(item.get("cited_by_count")),
        identifiers=identifiers(doi=doi, pmid=pmid, pmcid=pmcid, openalex_id=openalex_id),
        links=links(
            ((landing_url, "landing_page", "openalex"),)
            + tuple((pdf_url, "pdf", "openalex") for pdf_url in pdf_urls)
        ),
        authors=authors,
        raw_provider_metadata=item,
    )


# ---------------- Semantic Scholar ----------------


def semantic_scholar_authors(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    authors: list[str] = []
    for item in value:
        if isinstance(item, dict):
            name = first_string(item.get("name"))
            if name:
                authors.append(name)
    return tuple(authors)


def candidate_from_semantic_scholar_item(
    query: ScholarlyDiscoveryQuery,
    item: dict[str, Any],
    retrieved_at: datetime,
) -> ScholarlyWorkCandidate | None:
    title = first_string(item.get("title"))
    normalized_title = normalize_title_for_dedupe(title)
    title_hash = normalized_title_hash(title)
    if not title or normalized_title is None or title_hash is None:
        return None
    raw_external_ids = item.get("externalIds")
    external_ids: dict[str, Any] = raw_external_ids if isinstance(raw_external_ids, dict) else {}
    doi = normalize_doi(first_string(external_ids.get("DOI")))
    pmid = normalize_pmid(first_string(external_ids.get("PubMed")))
    pmcid = normalize_pmcid(first_string(external_ids.get("PubMedCentral")))
    arxiv_id = normalize_arxiv_id(first_string(external_ids.get("ArXiv")))
    semantic_scholar_id = normalize_semantic_scholar_id(first_string(item.get("paperId")))
    corpus_id = normalize_corpus_id(item.get("corpusId"))
    publication_types = item.get("publicationTypes")
    raw_pdf = item.get("openAccessPdf")
    pdf: dict[str, Any] = raw_pdf if isinstance(raw_pdf, dict) else {}
    authors = tuple(
        ScholarlyAuthorCandidate(author_name=name, author_order=index)
        for index, name in enumerate(semantic_scholar_authors(item.get("authors")), start=1)
    )
    return candidate(
        query=query,
        title=title,
        normalized_title=normalized_title,
        normalized_title_hash=title_hash,
        provider_name="semantic_scholar",
        provider_record_id=semantic_scholar_id,
        provider_record_url=first_string(item.get("url")),
        retrieved_at=retrieved_at,
        abstract=clean_markup(first_string(item.get("abstract"))),
        publication_year=int_or_none(item.get("year")),
        publication_date=first_string(item.get("publicationDate")),
        work_type=(
            first_string(publication_types[0])
            if isinstance(publication_types, list) and publication_types
            else None
        ),
        venue_name=first_string(item.get("venue")),
        doi=doi,
        pmid=pmid,
        pmcid=pmcid,
        arxiv_id=arxiv_id,
        semantic_scholar_id=semantic_scholar_id,
        corpus_id=corpus_id,
        license=first_string(pdf.get("license")),
        citation_count=int_or_none(item.get("citationCount")),
        influential_citation_count=int_or_none(item.get("influentialCitationCount")),
        identifiers=identifiers(
            doi=doi,
            pmid=pmid,
            pmcid=pmcid,
            arxiv_id=arxiv_id,
            semantic_scholar_id=semantic_scholar_id,
            corpus_id=corpus_id,
        ),
        links=links(
            (
                (first_string(item.get("url")), "landing_page", "semantic_scholar"),
                (first_string(pdf.get("url")), "pdf", "semantic_scholar"),
            )
        ),
        authors=authors,
        raw_provider_metadata=item,
    )


# ---------------- Europe PMC ----------------


def europe_pmc_authors(author_string: object) -> tuple[ScholarlyAuthorCandidate, ...]:
    raw = first_string(author_string)
    if not raw:
        return ()
    parts = [part.strip() for part in raw.split(", ") if part.strip()]
    return tuple(
        ScholarlyAuthorCandidate(author_name=name, author_order=index)
        for index, name in enumerate(parts, start=1)
    )


def candidate_from_europe_pmc_item(
    query: ScholarlyDiscoveryQuery,
    item: dict[str, Any],
    retrieved_at: datetime,
) -> ScholarlyWorkCandidate | None:
    title = first_string(item.get("title"))
    normalized_title = normalize_title_for_dedupe(title)
    title_hash = normalized_title_hash(title)
    if not title or normalized_title is None or title_hash is None:
        return None
    doi = normalize_doi(first_string(item.get("doi")))
    pmid = normalize_pmid(first_string(item.get("pmid")))
    pmcid = normalize_pmcid(first_string(item.get("pmcid")))
    source_url = None
    if pmid:
        source_url = f"https://europepmc.org/article/MED/{pmid}"
    elif pmcid:
        source_url = f"https://europepmc.org/article/PMC/{pmcid.replace('PMC', '')}"
    pdf_url = f"https://europepmc.org/articles/{pmcid}?pdf=render" if pmcid else None
    return candidate(
        query=query,
        title=title,
        normalized_title=normalized_title,
        normalized_title_hash=title_hash,
        provider_name="europe_pmc",
        provider_record_id=pmid or pmcid or doi,
        provider_record_url=source_url,
        retrieved_at=retrieved_at,
        # Europe PMC 的 abstractText 带 JATS 标记，清洗后再入库供卡片使用。
        abstract=clean_markup(first_string(item.get("abstractText"))),
        publication_year=int_or_none(item.get("pubYear")),
        publication_date=first_string(item.get("firstPublicationDate")),
        work_type=first_string(item.get("pubType")),
        venue_name=first_string(item.get("journalTitle")),
        doi=doi,
        pmid=pmid,
        pmcid=pmcid,
        oa_status="green" if item.get("isOpenAccess") in {True, "Y", "y"} else None,
        license=first_string(item.get("license")),
        citation_count=int_or_none(item.get("citedByCount")),
        identifiers=identifiers(doi=doi, pmid=pmid, pmcid=pmcid),
        links=links(((source_url, "landing_page", "europe_pmc"), (pdf_url, "pdf", "europe_pmc"))),
        authors=europe_pmc_authors(item.get("authorString")),
        raw_provider_metadata=item,
    )


# ---------------- arXiv Atom ----------------


def atom_child_text(parent: ET.Element, tag: str) -> str | None:
    element = parent.find(f"atom:{tag}", ATOM_NS)
    if element is None:
        element = parent.find(f"{{http://www.w3.org/2005/Atom}}{tag}")
    if element is None or element.text is None:
        return None
    return html.unescape("".join(element.itertext())).strip() or None


def arxiv_authors(entry: ET.Element) -> tuple[ScholarlyAuthorCandidate, ...]:
    author_els = entry.findall("atom:author", ATOM_NS)
    if not author_els:
        author_els = entry.findall("{http://www.w3.org/2005/Atom}author")
    authors: list[ScholarlyAuthorCandidate] = []
    for index, author_el in enumerate(author_els, start=1):
        name = atom_child_text(author_el, "name")
        if name:
            authors.append(ScholarlyAuthorCandidate(author_name=name, author_order=index))
    return tuple(authors)


def _atom_entries(root: ET.Element) -> list[ET.Element]:
    entries = root.findall("atom:entry", ATOM_NS)
    if not entries:
        entries = root.findall("{http://www.w3.org/2005/Atom}entry")
    return entries


def candidates_from_arxiv_atom(
    query: ScholarlyDiscoveryQuery,
    atom_text: str,
    retrieved_at: datetime,
) -> tuple[ScholarlyWorkCandidate, ...]:
    try:
        root = ET.fromstring(atom_text)
    except ET.ParseError:
        return ()
    results: list[ScholarlyWorkCandidate] = []
    for entry in _atom_entries(root):
        title = clean_markup(atom_child_text(entry, "title") or "")
        abstract = clean_markup(atom_child_text(entry, "summary"))
        arxiv_id_raw = atom_child_text(entry, "id") or ""
        arxiv_id = normalize_arxiv_id(arxiv_id_raw.rsplit("/", 1)[-1])
        published = atom_child_text(entry, "published")
        authors = arxiv_authors(entry)
        normalized_title = normalize_title_for_dedupe(title)
        title_hash = normalized_title_hash(title)
        if not title or normalized_title is None or title_hash is None:
            continue
        pdf_url = f"https://arxiv.org/pdf/{arxiv_id}.pdf" if arxiv_id else None
        results.append(
            candidate(
                query=query,
                title=title,
                normalized_title=normalized_title,
                normalized_title_hash=title_hash,
                provider_name="arxiv",
                provider_record_id=arxiv_id,
                provider_record_url=f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else None,
                retrieved_at=retrieved_at,
                abstract=abstract,
                publication_year=year_from_date(published),
                publication_date=(
                    published[:10] if published and len(published) >= 10 else published
                ),
                work_type="preprint",
                venue_name="arXiv",
                arxiv_id=arxiv_id,
                oa_status="green",
                identifiers=identifiers(arxiv_id=arxiv_id),
                links=links(
                    (
                        (
                            f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else None,
                            "landing_page",
                            "arxiv",
                        ),
                        (pdf_url, "pdf", "arxiv"),
                    )
                ),
                authors=authors,
                raw_provider_metadata={"atom_id": arxiv_id_raw},
            )
        )
    return tuple(results)


def arxiv_atom_total_results(atom_text: str) -> int | None:
    try:
        root = ET.fromstring(atom_text)
    except ET.ParseError:
        return None
    element = root.find("opensearch:totalResults", ATOM_NS)
    if element is None:
        element = root.find("{http://a9.com/-/spec/opensearch/1.1/}totalResults")
    return int_or_none(element.text if element is not None else None)


def arxiv_atom_entry_count(atom_text: str) -> int:
    try:
        root = ET.fromstring(atom_text)
    except ET.ParseError:
        return 0
    return len(_atom_entries(root))
