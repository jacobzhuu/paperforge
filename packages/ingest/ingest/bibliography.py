"""Deterministic bibliographic clues extracted from a user-supplied PDF.

The result is deliberately a *clue*, not trusted scholarly metadata. The
library upload pipeline must still match it against an existing work or verify
it through an authoritative provider before creating a library entry.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from typing import Any

from ingest.document_extractors import DocumentParseError

_DOI_RE = re.compile(r"\b10\.\d{4,9}/[^\s\"<>]+", re.IGNORECASE)
_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
_AUTHOR_SPLIT_RE = re.compile(r"\s*(?:;|\||\band\b|、)\s*", re.IGNORECASE)
_TITLE_NOISE_RE = re.compile(
    r"^(?:untitled|microsoft word|document|article|preprint|manuscript|"
    r"arxiv|doi\b|https?://|www\.)",
    re.IGNORECASE,
)
_FIRST_PAGE_MAX_CHARS = 20_000


@dataclass(frozen=True)
class PdfBibliographicMetadata:
    doi: str | None = None
    title: str | None = None
    authors: tuple[str, ...] = ()
    publication_year: int | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "doi": self.doi,
            "title": self.title,
            "authors": list(self.authors),
            "publication_year": self.publication_year,
        }


def extract_pdf_bibliographic_metadata(
    content: bytes,
    *,
    max_pages: int = 3,
) -> PdfBibliographicMetadata:
    """Read bounded PDF metadata/first pages without executing embedded content."""
    if not content.startswith(b"%PDF-"):
        raise DocumentParseError("pdf_signature_mismatch")
    try:
        from pypdf import PdfReader

        reader = PdfReader(BytesIO(content))
        if reader.is_encrypted:
            raise DocumentParseError("encrypted_pdf_not_supported")
        raw_metadata: Any = reader.metadata or {}
        page_text = "\n".join(
            (page.extract_text() or "") for page in reader.pages[: max(1, max_pages)]
        )[:_FIRST_PAGE_MAX_CHARS]
    except DocumentParseError:
        raise
    except Exception as error:  # noqa: BLE001 - normalize parser failures for the upload API
        raise DocumentParseError(f"pdf_metadata_parse_failed:{type(error).__name__}") from error

    return infer_pdf_bibliographic_metadata(
        page_text,
        document_metadata={
            "title": _metadata_value(raw_metadata, "/Title", "title"),
            "author": _metadata_value(raw_metadata, "/Author", "author"),
            "subject": _metadata_value(raw_metadata, "/Subject", "subject"),
            "creation_date": getattr(raw_metadata, "creation_date", None),
        },
    )


def infer_pdf_bibliographic_metadata(
    text: str,
    *,
    document_metadata: dict[str, Any] | None = None,
) -> PdfBibliographicMetadata:
    """Infer DOI/title/authors/year from already extracted first-page text."""
    metadata = document_metadata or {}
    title = _clean_title(metadata.get("title")) or _title_from_text(text)
    authors = _authors(metadata.get("author"))
    if not authors:
        authors = _authors_from_text(text, title=title)

    doi_source = "\n".join(
        str(value) for value in (metadata.get("subject"), metadata.get("title"), text) if value
    )
    doi_match = _DOI_RE.search(doi_source)
    doi = _clean_doi(doi_match.group(0)) if doi_match else None
    year = _metadata_year(metadata.get("creation_date")) or _year_from_text(text)
    return PdfBibliographicMetadata(
        doi=doi,
        title=title,
        authors=tuple(authors),
        publication_year=year,
    )


def _metadata_value(metadata: Any, *names: str) -> Any:
    for name in names:
        try:
            value = metadata.get(name)
        except (AttributeError, TypeError):
            value = None
        if value:
            return value
    return None


def _clean_line(value: Any) -> str:
    return " ".join(str(value or "").replace("\x00", " ").split()).strip()


def _clean_title(value: Any) -> str | None:
    title = _clean_line(value)
    if not title or len(title) < 8 or _TITLE_NOISE_RE.match(title):
        return None
    return title[:500]


def _title_from_text(text: str) -> str | None:
    lines = [_clean_line(line) for line in (text or "").splitlines()]
    candidates: list[str] = []
    for line in lines[:30]:
        if not line or _TITLE_NOISE_RE.match(line) or _DOI_RE.search(line):
            continue
        if line.casefold() in {"abstract", "摘要"}:
            break
        if len(line) < 8 or len(line) > 500:
            continue
        candidates.append(line)
        if len(candidates) >= 3:
            break
    if not candidates:
        return None
    if len(candidates) > 1 and len(candidates[0]) < 24 and len(candidates[1]) > len(candidates[0]):
        candidates = candidates[1:]
    return " ".join(candidates)[:500]


def _authors(value: Any) -> list[str]:
    raw = _clean_line(value)
    if not raw:
        return []
    parts = _AUTHOR_SPLIT_RE.split(raw)
    # Preserve a single "Surname, Given" pair, but split longer comma lists.
    if len(parts) == 1 and raw.count(",") >= 2:
        parts = raw.split(",")
    return [part[:200] for part in (_clean_line(item) for item in parts) if len(part) >= 2][:50]


def _authors_from_text(text: str, *, title: str | None) -> list[str]:
    lines = [_clean_line(line) for line in (text or "").splitlines()]
    if title:
        title_tokens = set(title.split())
        start = next(
            (
                index + 1
                for index, line in enumerate(lines[:30])
                if line and len(title_tokens & set(line.split())) >= max(1, len(title_tokens) // 2)
            ),
            1,
        )
    else:
        start = 1
    for line in lines[start : start + 5]:
        lowered = line.casefold()
        if (
            not line
            or len(line) > 300
            or "abstract" in lowered
            or "university" in lowered
            or "institute" in lowered
            or "department" in lowered
            or "@" in line
            or _DOI_RE.search(line)
        ):
            continue
        parts = _AUTHOR_SPLIT_RE.split(line)
        if len(parts) == 1:
            parts = [item.strip() for item in line.split(",")]
        cleaned = [
            item
            for item in (_clean_line(part) for part in parts)
            if 2 <= len(item) <= 100 and not any(char.isdigit() for char in item)
        ]
        if cleaned and len(cleaned) <= 30:
            return cleaned
    return []


def _metadata_year(value: Any) -> int | None:
    if isinstance(value, datetime):
        return value.year
    match = _YEAR_RE.search(_clean_line(value))
    return int(match.group(0)) if match else None


def _year_from_text(text: str) -> int | None:
    match = _YEAR_RE.search((text or "")[:5_000])
    return int(match.group(0)) if match else None


def _clean_doi(value: str) -> str | None:
    doi = value.strip().rstrip(".,;:)]}>'\"").lower()
    return doi if _DOI_RE.fullmatch(doi) else None
