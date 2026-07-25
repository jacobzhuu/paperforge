# 迁移自 DeepSearch literature_review/normalization.py（纯函数，零改动）。
from __future__ import annotations

import hashlib
import html
import re
import unicodedata

_DOI_PATTERN = re.compile(r"10\.\d{4,9}/\S+", re.IGNORECASE)
_OPENALEX_WORK_PATTERN = re.compile(r"\bW\d+\b", re.IGNORECASE)
_ARXIV_NEW_PATTERN = re.compile(r"\d{4}\.\d{4,5}(?:v\d+)?", re.IGNORECASE)
_ARXIV_OLD_PATTERN = re.compile(r"[a-z.-]+(?:/[a-z.-]+)?/\d{7}(?:v\d+)?", re.IGNORECASE)
_SEMANTIC_SCHOLAR_ID_PATTERN = re.compile(r"\b[0-9a-f]{16,64}\b", re.IGNORECASE)


def normalize_doi(value: str | None) -> str | None:
    if not value:
        return None
    text = html.unescape(value).strip()
    text = re.sub(r"^(?:doi\s*:?\s*)", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", text, flags=re.IGNORECASE)
    match = _DOI_PATTERN.search(text)
    doi = match.group(0) if match else text
    doi = _strip_terminal_punctuation(doi).lower()
    return doi if _DOI_PATTERN.fullmatch(doi) else None


def normalize_pmid(value: str | int | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    match = re.search(r"(?:pubmed/|pmid\s*:?\s*)?(\d+)", text, re.IGNORECASE)
    if match is None:
        return None
    normalized = match.group(1).lstrip("0") or "0"
    return normalized


def normalize_pmcid(value: str | int | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    match = re.search(r"(?:pmc/articles/)?(?:pmcid\s*:?\s*)?(PMC)?\s*(\d+)", text, re.IGNORECASE)
    if match is None:
        return None
    return f"PMC{match.group(2).lstrip('0') or '0'}"


def normalize_arxiv_id(value: str | None) -> str | None:
    if not value:
        return None
    text = html.unescape(value).strip()
    text = re.sub(r"^arxiv\s*:?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^https?://(?:www\.)?arxiv\.org/(?:abs|pdf)/", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\.pdf$", "", text, flags=re.IGNORECASE)
    match = _ARXIV_NEW_PATTERN.search(text) or _ARXIV_OLD_PATTERN.search(text)
    if match is None:
        return None
    arxiv_id = re.sub(r"v\d+$", "", match.group(0), flags=re.IGNORECASE).lower()
    return arxiv_id


def normalize_openalex_id(value: str | None) -> str | None:
    if not value:
        return None
    match = _OPENALEX_WORK_PATTERN.search(value.strip())
    return match.group(0).upper() if match else None


def normalize_semantic_scholar_id(value: str | None) -> str | None:
    if not value:
        return None
    text = value.strip()
    text = re.sub(r"^https?://(?:www\.)?semanticscholar\.org/paper/", "", text, flags=re.I)
    if "/" in text:
        text = text.rstrip("/").split("/")[-1]
    match = _SEMANTIC_SCHOLAR_ID_PATTERN.search(text)
    if match is None:
        return text if text and re.fullmatch(r"[A-Za-z0-9_-]{8,128}", text) else None
    return match.group(0).lower()


def normalize_corpus_id(value: str | int | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    match = re.search(r"(?:corpus\s*id|corpusid)?\s*:?\s*(\d+)", text, re.IGNORECASE)
    return match.group(1).lstrip("0") or "0" if match else None


def normalize_title_for_dedupe(value: str | None) -> str | None:
    if not value:
        return None
    text = html.unescape(value)
    text = unicodedata.normalize("NFKC", text)
    text = text.casefold()
    text = re.sub(r"[\u2010-\u2015]", "-", text)
    text = re.sub(r"[^0-9a-z]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def normalized_title_hash(value: str | None) -> str | None:
    normalized = normalize_title_for_dedupe(value)
    if normalized is None:
        return None
    return f"sha256:{hashlib.sha256(normalized.encode('utf-8')).hexdigest()}"


def singularize_token(token: str) -> str:
    """Light English plural trim shared by topic matching and theme merge."""
    if token.casefold().endswith("ies") and len(token) > 4:
        return token[:-3] + "y"
    if token.casefold().endswith("s") and len(token) > 3:
        return token[:-1]
    return token


def normalize_match_text(value: str) -> str:
    """Casefold + singularize tokens for fuzzy label / topic comparison."""
    tokens = str(value).casefold().replace("_", " ").replace("-", " ").split()
    return " ".join(singularize_token(token) for token in tokens)


def token_set(value: str) -> frozenset[str]:
    normalized = normalize_match_text(value)
    return frozenset(normalized.split()) if normalized else frozenset()


def token_set_jaccard(left: str, right: str) -> float:
    left_tokens = token_set(left)
    right_tokens = token_set(right)
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def _strip_terminal_punctuation(value: str) -> str:
    return value.strip().strip(" \t\r\n.,;\"'<>[]{}")
