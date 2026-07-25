"""切块质量评分（文献全文取向）。

改造自 DeepSearch parsing/quality.py（916 行）的 chunk 侧评分（设计 §3.1）。
保留：信息密度、样板/导航噪声、参考文献段识别、重复度、中英双语 token 化。
去掉：source/域权威度、crawlability、vendor 域名清单、deployment query 等
OSINT 专属信号（PaperForge 的来源是 OA 全文与用户素材，不是开放网页）。

用途：CARDS 阶段挑选送进 extractor 的块——参考文献段与导航噪声不该占用
长上下文预算，也不该被当作可引用要点。评分只用于排序与过滤，绝不阻断（draft-first）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-_.]*")
_CJK_RE = re.compile(r"[一-鿿]")
_SENTENCE_END_RE = re.compile(r"[.!?。！？]")

_STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "of",
        "and",
        "or",
        "to",
        "in",
        "for",
        "on",
        "with",
        "is",
        "are",
        "be",
        "by",
        "as",
        "that",
        "this",
        "at",
        "from",
    }
)

_NAVIGATION_PHRASES = (
    "skip to main content",
    "cookie policy",
    "privacy policy",
    "terms of service",
    "all rights reserved",
    "sign in",
    "subscribe to our newsletter",
    "share this article",
    "download citation",
    "back to top",
    "跳转到主要内容",
    "版权所有",
    "隐私政策",
)

_STRONG_REFERENCE_PHRASES = (
    "doi:",
    "et al.",
    "pp.",
    "vol.",
    "no.",
    "in proceedings of",
    "arxiv preprint",
)

_REFERENCE_HEADINGS = (
    "references",
    "bibliography",
    "参考文献",
    "引用文献",
)

_EXPLANATORY_TERMS = (
    "we ",
    "our ",
    "this paper",
    "results show",
    "we propose",
    "本文",
    "我们",
    "结果表明",
)


@dataclass(frozen=True)
class ChunkQuality:
    content_quality_score: float
    information_density_score: float
    boilerplate_score: float
    query_relevance_score: float
    is_boilerplate_like: bool
    is_navigation_noise: bool
    is_reference_section: bool
    usable_for_cards: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)


def assess_chunk_quality(
    *,
    text: str,
    query: str | None = None,
) -> ChunkQuality:
    """对单个切块打分。``query`` 可选：给定时叠加主题相关性信号。"""
    normalized = " ".join(text.split())
    lower = normalized.lower()
    reasons: list[str] = []

    density = information_density_score(normalized)
    boilerplate = boilerplate_score(lower)
    relevance = query_relevance_score(normalized, query) if query else 0.0
    is_reference = looks_like_reference_section(lower)
    is_navigation = looks_like_navigation_noise(lower)
    boilerplate_like = boilerplate >= 0.5 or is_navigation

    if is_reference:
        reasons.append("reference_section")
    if is_navigation:
        reasons.append("navigation_noise")
    if len(normalized) < 40:
        reasons.append("too_short")

    score = max(0.0, min(1.0, density * 0.6 + relevance * 0.3 - boilerplate * 0.4 + 0.1))
    usable = (
        not is_reference
        and not is_navigation
        and len(normalized) >= 40
        and score >= 0.2
    )
    if not usable and not reasons:
        reasons.append("low_information_density")

    return ChunkQuality(
        content_quality_score=round(score, 4),
        information_density_score=round(density, 4),
        boilerplate_score=round(boilerplate, 4),
        query_relevance_score=round(relevance, 4),
        is_boilerplate_like=boilerplate_like,
        is_navigation_noise=is_navigation,
        is_reference_section=is_reference,
        usable_for_cards=usable,
        reasons=tuple(reasons),
    )


def tokenize(value: str) -> tuple[str, ...]:
    return tuple(token.lower() for token in _TOKEN_PATTERN.findall(value))


def cjk_chars(value: str) -> list[str]:
    return _CJK_RE.findall(value)


def looks_like_prose(text: str) -> bool:
    return bool(_SENTENCE_END_RE.search(text))


def has_high_repetition(text: str) -> bool:
    tokens = tokenize(text)
    if len(tokens) < 12:
        return False
    return len(set(tokens)) / len(tokens) < 0.35


def information_density_score(text: str) -> float:
    """唯一 token 比 + 长度 + 成句信号；重复度高则扣分。中文按字统计。"""
    tokens = tokenize(text)
    if not tokens:
        chars = cjk_chars(text)
        if not chars:
            return 0.0
        char_count = len(chars)
        unique_ratio = len(set(chars)) / char_count
        length_score = min(char_count / 120, 1.0)
        sentence_signal = 0.2 if looks_like_prose(text) else 0.0
        return min(1.0, max(0.0, unique_ratio * 0.42 + length_score * 0.38 + sentence_signal))
    token_count = len(tokens)
    unique_ratio = len(set(tokens)) / token_count
    length_score = min(token_count / 90, 1.0)
    sentence_signal = 0.2 if looks_like_prose(text) else 0.0
    score = unique_ratio * 0.42 + length_score * 0.38 + sentence_signal
    if has_high_repetition(text):
        score -= 0.25
    return min(1.0, max(0.0, score))


def boilerplate_score(lower_text: str) -> float:
    if not lower_text:
        return 1.0
    score = 0.18 * sum(1 for phrase in _NAVIGATION_PHRASES if phrase in lower_text)
    token_count = max(1, len(tokenize(lower_text)))
    linkish = sum(
        lower_text.count(term)
        for term in ("menu", "sidebar", "navigation", "footer", "privacy", "cookie")
    )
    score += min(0.45, linkish / token_count)
    return min(1.0, score)


def query_relevance_score(text: str, query: str | None) -> float:
    if not query:
        return 0.0
    query_tokens = [token for token in dict.fromkeys(tokenize(query)) if token not in _STOPWORDS]
    cjk_score = _cjk_overlap_score(text, query)
    if not query_tokens:
        return cjk_score
    text_tokens = set(tokenize(text))
    token_score = len([t for t in query_tokens if t in text_tokens]) / len(query_tokens)
    return max(token_score, cjk_score)


def _cjk_overlap_score(text: str, query: str) -> float:
    query_chars = set(cjk_chars(query))
    if not query_chars:
        return 0.0
    text_chars = set(cjk_chars(text))
    if not text_chars:
        return 0.0
    return len(query_chars & text_chars) / len(query_chars)


def looks_like_reference_section(lower_text: str) -> bool:
    stripped = lower_text.strip()
    if stripped in _REFERENCE_HEADINGS:
        return True
    if stripped.startswith(tuple(f"{heading} " for heading in _REFERENCE_HEADINGS)):
        return True
    strong_hits = sum(1 for phrase in _STRONG_REFERENCE_PHRASES if phrase in lower_text)
    if strong_hits >= 2 and not _contains_explanatory_terms(lower_text):
        return True
    token_count = max(1, len(tokenize(lower_text)))
    return strong_hits >= 1 and strong_hits / token_count >= 0.08


def looks_like_navigation_noise(lower_text: str) -> bool:
    phrase_hits = sum(1 for phrase in _NAVIGATION_PHRASES if phrase in lower_text)
    if phrase_hits >= 2:
        return True
    token_count = max(1, len(tokenize(lower_text)))
    nav_hits = sum(
        lower_text.count(term)
        for term in ("menu", "sidebar", "navigation", "footer", "privacy", "cookie")
    )
    return token_count <= 80 and nav_hits / token_count >= 0.12


def _contains_explanatory_terms(lower_text: str) -> bool:
    return any(term in lower_text for term in _EXPLANATORY_TERMS)
