"""各 provider 的查询语法净化与时间窗过滤构造。

迁移自 DeepSearch literature_review/adapters.py 的查询构造段（设计 §3.1）。
这些规则是踩过 HTTP 400 / 零命中坑之后的产物：OpenAlex 的 ``search=`` 不支持
字段前缀与管道年份组、arXiv 已带字段前缀的原生查询不能再套 ``all:``。
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

_ARXIV_FIELD_PREFIX_RE = re.compile(
    r"(?:^|[\s(])(?:ti|abs|au|co|jr|cat|rn|id|all):",
    re.IGNORECASE,
)
_OPENALEX_TEXT_FIELD_RE = re.compile(
    r"\b(?:title_and_abstract|title|abstract|fulltext|display_name)\s*:\s*",
    re.IGNORECASE,
)
_OPENALEX_YEAR_CLAUSE_RE = re.compile(
    r"\s*(?:\bAND\s+)?\bpublication_year\s*:\s*(\d{4})(?:\s*[-–]\s*(\d{4}))?\s*",
    re.IGNORECASE,
)
_OPENALEX_WILDCARD_RE = re.compile(r"[*?]+")
# LLM 写的原生查询常把 ``AND (2020|2021|2022)`` 这类年份交替塞进检索文本。
# ``|`` 是 OpenAlex *filter* 语法，出现在 ``search=`` 里会被 400 拒绝；
# 年份约束已由日期 filter 承担，因此整组连同前面的布尔词一并删除。
_OPENALEX_YEAR_PIPE_GROUP_RE = re.compile(
    r"(?:\b(?:AND|OR|NOT)\s+)?\(\s*(?:19|20)\d{2}(?:\s*\|\s*(?:19|20)\d{2})+\s*\)",
    re.IGNORECASE,
)
_OPENALEX_BARE_YEAR_PIPES_RE = re.compile(
    r"(?:\b(?:AND|OR|NOT)\s+)?\b(?:19|20)\d{2}(?:\s*\|\s*(?:19|20)\d{2})+\b",
    re.IGNORECASE,
)
_DANGLING_BOOLEAN_RE = re.compile(r"^\s*(?:AND|OR|NOT)\b\s*|\s*\b(?:AND|OR|NOT)\s*$")


def arxiv_search_query(query_text: str) -> str:
    """纯文本查询包 ``all:``；已带 arXiv 字段前缀的原生查询原样透传。"""
    text = " ".join(query_text.split())
    if _ARXIV_FIELD_PREFIX_RE.search(text):
        return text
    return f"all:{text}"


def openalex_search_and_filters(query_text: str) -> tuple[str, list[str]]:
    """把字段前缀式原生查询翻译为 OpenAlex 支持的 search + filter 组合。"""
    text = _openalex_strip_year_pipe_groups(" ".join(query_text.split()))
    filter_parts: list[str] = []
    lowered = text.lower()
    if not _OPENALEX_TEXT_FIELD_RE.search(text) and "publication_year" not in lowered:
        return _openalex_without_unsupported_wildcards(text), filter_parts

    def _year_clause_to_filter(match: re.Match[str]) -> str:
        start_year = match.group(1)
        end_year = match.group(2) or start_year
        filter_parts.append(f"from_publication_date:{start_year}-01-01")
        filter_parts.append(f"to_publication_date:{end_year}-12-31")
        return " "

    text = _OPENALEX_YEAR_CLAUSE_RE.sub(_year_clause_to_filter, text)
    text = _OPENALEX_TEXT_FIELD_RE.sub("", text)
    text = _DANGLING_BOOLEAN_RE.sub("", " ".join(text.split())).strip()
    if not text:
        text = " ".join(query_text.split())
    return _openalex_without_unsupported_wildcards(text), filter_parts


def _openalex_strip_year_pipe_groups(text: str) -> str:
    cleaned = _OPENALEX_YEAR_PIPE_GROUP_RE.sub(" ", text)
    cleaned = _OPENALEX_BARE_YEAR_PIPES_RE.sub(" ", cleaned)
    cleaned = _DANGLING_BOOLEAN_RE.sub("", " ".join(cleaned.split())).strip()
    return cleaned or text


def _openalex_without_unsupported_wildcards(text: str) -> str:
    """去掉 OpenAlex 会当作通配符语法解释的自然语言标点。"""
    return " ".join(_OPENALEX_WILDCARD_RE.sub(" ", text).split())


def merge_filter_parts(*part_groups: list[str] | None) -> str | None:
    """合并 ``key:value`` 过滤片段；同键先到先得。"""
    merged: dict[str, str] = {}
    for parts in part_groups:
        for part in parts or []:
            key, _, value = part.partition(":")
            if key and value:
                merged.setdefault(key, value)
    return ",".join(f"{key}:{value}" for key, value in merged.items()) or None


def _filter_year(filters: object, *keys: str) -> int | None:
    if not isinstance(filters, dict):
        return None
    time_range = filters.get("time_range")
    if not isinstance(time_range, dict):
        return None
    for key in keys:
        value = time_range.get(key)
        if isinstance(value, int) and 1000 <= value <= 9999:
            return value
        if isinstance(value, str) and value[:4].isdigit():
            year = int(value[:4])
            if 1000 <= year <= 9999:
                return year
    return None


def _filter_date(filters: object, *keys: str) -> date | None:
    if not isinstance(filters, dict):
        return None
    time_range = filters.get("time_range")
    if not isinstance(time_range, dict):
        return None
    for key in keys:
        raw = time_range.get(key)
        if isinstance(raw, date):
            return raw
        if isinstance(raw, str):
            text = raw.strip()[:10]
            if len(text) == 10 and text[4] == "-" and text[7] == "-":
                try:
                    return date.fromisoformat(text)
                except ValueError:
                    continue
    return None


def time_range_bounds(filters: object) -> tuple[date | None, date | None]:
    """解析日精度时间边界；仅给年份时展开为 1 月 1 日 / 12 月 31 日。"""
    start = _filter_date(filters, "start_date", "start", "from_date", "from")
    end = _filter_date(filters, "end_date", "end", "to_date", "to")
    if start is None:
        start_year = _filter_year(filters, "start_year", "from_year", "start", "from")
        start = date(start_year, 1, 1) if start_year is not None else None
    if end is None:
        end_year = _filter_year(filters, "end_year", "to_year", "end", "to")
        end = date(end_year, 12, 31) if end_year is not None else None
    return start, end


def crossref_date_filter(filters: object) -> str | None:
    start, end = time_range_bounds(filters)
    parts: list[str] = []
    if start is not None:
        parts.append(f"from-pub-date:{start.isoformat()}")
    if end is not None:
        parts.append(f"until-pub-date:{end.isoformat()}")
    return ",".join(parts) or None


def openalex_date_filter(filters: object) -> str | None:
    start, end = time_range_bounds(filters)
    parts: list[str] = []
    if start is not None:
        parts.append(f"from_publication_date:{start.isoformat()}")
    if end is not None:
        parts.append(f"to_publication_date:{end.isoformat()}")
    return ",".join(parts) or None


def semantic_scholar_time_params(filters: object) -> dict[str, str]:
    """整年区间用 ``year``；跨年内窗口用日精度 ``publicationDateOrYear``。"""
    start, end = time_range_bounds(filters)
    if start is None or end is None:
        return {}
    year_aligned = start.month == 1 and start.day == 1 and end.month == 12 and end.day == 31
    if year_aligned:
        if start.year == end.year:
            return {"year": str(start.year)}
        return {"year": f"{start.year}-{end.year}"}
    return {"publicationDateOrYear": f"{start.isoformat()}:{end.isoformat()}"}


def arxiv_submitted_date_clause(filters: object) -> str | None:
    start, end = time_range_bounds(filters)
    if start is None or end is None:
        return None
    return f"submittedDate:[{start.strftime('%Y%m%d')}0000 TO {end.strftime('%Y%m%d')}2359]"


def europe_pmc_date_clause(filters: object) -> str | None:
    start, end = time_range_bounds(filters)
    if start is None and end is None:
        return None
    lower = start.isoformat() if start else "1800-01-01"
    upper = end.isoformat() if end else date.today().isoformat()
    return f"(FIRST_PDATE:[{lower} TO {upper}])"


def filters_as_dict(filters: object) -> dict[str, Any]:
    return filters if isinstance(filters, dict) else {}
