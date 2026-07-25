"""Deterministic scholarly reference formatting (APA / GB-T 7714 / author-year).

迁移自 DeepSearch literature_review/citation_format.py。
改动：输入类型 ReviewEvidenceCard → 简化的 ReferenceMetadata（解耦溯源字段）。
中文感知格式化逻辑保持不变。
"""

from __future__ import annotations

from typing import Literal

from paper_ir.reference import ReferenceMetadata

CitationStyle = Literal["author_year", "apa", "gbt7714"]


def format_scholarly_reference(
    card: ReferenceMetadata,
    *,
    style: CitationStyle = "author_year",
) -> str:
    """Format a durable reference string for a review evidence card."""
    normalized = (style or "author_year").strip().lower().replace("-", "_")
    if normalized in {"gbt7714", "gb_t_7714", "gb/t7714", "gb/t_7714"}:
        return _format_gbt7714(card)
    if normalized == "apa":
        return _format_apa(card)
    return _format_author_year(card)


def normalize_citation_style(value: str | None) -> CitationStyle:
    raw = (value or "author_year").strip().lower()
    compact = (
        raw.replace("-", "")
        .replace("_", "")
        .replace("/", "")
        .replace(" ", "")
    )
    if compact in {"gbt7714", "gb7714"}:
        return "gbt7714"
    if compact == "apa":
        return "apa"
    if compact in {"authoryear", "author_year"}:
        return "author_year"
    return "author_year"


def _format_author_year(card: ReferenceMetadata) -> str:
    authors = _authors_text(card, style="author_year")
    year = str(card.publication_year or "n.d.")
    title = card.title or card.normalized_title or "Untitled work"
    parts = [f"{authors} ({year}).", title.rstrip(".") + "."]
    if card.venue_name:
        parts.append(card.venue_name.rstrip(".") + ".")
    if card.doi:
        parts.append(f"doi:{card.doi}")
    elif _reference_url(card):
        parts.append(_reference_url(card) or "")
    return " ".join(part for part in parts if part.strip())


def _format_apa(card: ReferenceMetadata) -> str:
    authors = _authors_text(card, style="apa")
    year = str(card.publication_year or "n.d.")
    title = card.title or card.normalized_title or "Untitled work"
    parts = [f"{authors} ({year}).", f"{title.rstrip('.')}."]
    if card.venue_name:
        parts.append(f"{card.venue_name.rstrip('.')}.")
    if card.doi:
        parts.append(f"https://doi.org/{card.doi}")
    elif _reference_url(card):
        parts.append(_reference_url(card) or "")
    return " ".join(part for part in parts if part.strip())


def _format_gbt7714(card: ReferenceMetadata) -> str:
    authors = _authors_text(card, style="gbt7714")
    year = str(card.publication_year or "n.d.")
    title = card.title or card.normalized_title or "Untitled work"
    venue = (card.venue_name or "").strip()
    # Compact GB/T 7714-ish author-year form for journal/preprint metadata.
    body = f"{authors}. {title.rstrip('.')}[J]." if venue else f"{authors}. {title.rstrip('.')}[A]."
    if venue:
        body = f"{authors}. {title.rstrip('.')}[J]. {venue}, {year}."
    else:
        body = f"{body} {year}."
    if card.doi:
        body = f"{body} DOI:{card.doi}."
    elif _reference_url(card):
        body = f"{body} {_reference_url(card)}."
    return " ".join(body.split())


def _authors_text(card: ReferenceMetadata, *, style: str) -> str:
    authors = card.citation_metadata.get("authors")
    if not isinstance(authors, list) or not authors:
        return "Unknown author"
    names: list[str] = []
    for author in authors:
        if isinstance(author, dict):
            name = str(author.get("author_name") or "").strip()
            if name:
                names.append(name)
    if not names:
        return "Unknown author"
    if style == "apa":
        formatted = [_apa_name(name) for name in names]
        if len(formatted) == 1:
            return formatted[0]
        if len(formatted) == 2:
            return f"{formatted[0]}, & {formatted[1]}"
        return ", ".join(formatted[:-1]) + f", & {formatted[-1]}"
    if style == "gbt7714":
        # Keep full names; join with Chinese enumeration comma when mixed CJK.
        joiner = "，" if any(_has_cjk(name) for name in names) else ", "
        return joiner.join(names[:3]) + ("等" if len(names) > 3 else "")
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return f"{names[0]} et al."


def _apa_name(full_name: str) -> str:
    parts = [part for part in full_name.replace(",", " ").split() if part]
    if not parts:
        return "Unknown author"
    if len(parts) == 1:
        return parts[0]
    family = parts[-1]
    initials = " ".join(f"{part[0]}." for part in parts[:-1] if part)
    return f"{family}, {initials}".strip()


def _reference_url(card: ReferenceMetadata) -> str | None:
    metadata = card.citation_metadata if isinstance(card.citation_metadata, dict) else {}
    for key in ("landing_page_url", "url", "provider_record_url"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _has_cjk(value: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in value)
