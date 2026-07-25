"""SCOPE 阶段：主题 → 研究范围 + 关键词矩阵 + 时间窗 + 子主题。

改造自 DeepSearch literature_review/protocol_generation.py（1159 行）：
保留「LLM 起草 + 确定性回退 + 严格 JSON schema 校验」模式；
去掉 PICO/PECO 强制框架、协议锁定(locked)语义、版本裁决。产物可编辑、可随时重生成。

Draft-first：LLM 不可用/输出不合法时，确定性回退保证 SCOPE 永远有产物。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from llm_runtime import LLMRunner

MAX_KEYWORD_GROUPS = 6
MAX_KEYWORDS_PER_GROUP = 8
MAX_SUBTOPICS = 8
DEFAULT_YEAR_SPAN = 6

_SYSTEM_PROMPT_ZH = """你是科研文献调研的规划助手。根据论文主题输出检索范围规划。
只输出 JSON，不要解释。字段：
{
  "research_question": "一句话研究问题",
  "scope_summary": "2-3 句范围说明（包含/排除什么）",
  "keyword_groups": [{"name": "概念名", "keywords": ["同义词1", "同义词2"]}],
  "subtopics": ["子主题1", "子主题2"],
  "time_range": {"start_year": 2019, "end_year": 2025},
  "inclusion_notes": ["纳入偏好"],
  "exclusion_notes": ["排除偏好"]
}
要求：keyword_groups 覆盖主题的正交概念面（方法/任务/领域/评价），每组给同义词与常见缩写；
不要编造不存在的专有名词；时间窗按领域节奏给出合理区间。"""

_SYSTEM_PROMPT_EN = """You plan literature searches for research papers. Given a paper topic,
output a search scope plan. Output JSON only, no commentary. Fields:
{
  "research_question": "one-sentence research question",
  "scope_summary": "2-3 sentences on what is in and out of scope",
  "keyword_groups": [{"name": "concept", "keywords": ["synonym1", "synonym2"]}],
  "subtopics": ["subtopic1", "subtopic2"],
  "time_range": {"start_year": 2019, "end_year": 2025},
  "inclusion_notes": ["inclusion preference"],
  "exclusion_notes": ["exclusion preference"]
}
Requirements: keyword_groups must cover orthogonal facets (method/task/domain/evaluation)
with synonyms and common abbreviations; never invent proper nouns that do not exist."""

_STOPWORDS_EN = frozenset(
    {
        "a", "an", "the", "of", "for", "and", "or", "to", "in", "on", "with",
        "using", "via", "based", "study", "review", "survey", "analysis",
        "research", "paper", "towards", "toward", "about",
    }
)
_STOPWORDS_ZH = frozenset({"的", "了", "与", "和", "及", "研究", "综述", "分析", "方法", "基于"})


async def generate_scope(
    topic: str,
    *,
    language: str = "en",
    paper_type: str = "review",
    runner: LLMRunner | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """生成研究范围。永远返回合法 scope（LLM 失败即走确定性回退）。"""
    fallback = deterministic_scope(topic, language=language, now=now)
    if runner is None or not runner.enabled:
        return fallback

    system_prompt = _SYSTEM_PROMPT_ZH if language == "zh" else _SYSTEM_PROMPT_EN
    user_prompt = (
        f"论文类型: {paper_type}\n主题: {topic}\n输出语言: 中文"
        if language == "zh"
        else f"Paper type: {paper_type}\nTopic: {topic}\nOutput language: English"
    )
    result = await runner.agenerate_json(
        "planner",
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        max_output_tokens=1500,
        temperature=0.2,
        metadata={"stage": "scope"},
    )
    if not result.ok or not isinstance(result.value, dict):
        return {**fallback, "generator": "deterministic_fallback", "fallback_reason": result.error}
    normalized = normalize_scope(result.value, topic=topic, language=language, now=now)
    normalized["generator"] = f"llm:{result.model or runner.model_for('planner')}"
    return normalized


def normalize_scope(
    raw: dict[str, Any],
    *,
    topic: str,
    language: str = "en",
    now: datetime | None = None,
) -> dict[str, Any]:
    """把 LLM 原始输出收敛到严格 schema；缺字段用确定性回退补齐。"""
    fallback = deterministic_scope(topic, language=language, now=now)
    groups: list[dict[str, Any]] = []
    for item in _as_list(raw.get("keyword_groups"))[:MAX_KEYWORD_GROUPS]:
        if isinstance(item, dict):
            name = _clean_text(item.get("name"))
            keywords = [
                kw for kw in (_clean_text(k) for k in _as_list(item.get("keywords"))) if kw
            ][:MAX_KEYWORDS_PER_GROUP]
        elif isinstance(item, str):
            name = _clean_text(item)
            keywords = [name] if name else []
        else:
            continue
        if name and keywords:
            groups.append({"name": name, "keywords": keywords})
    if not groups:
        groups = fallback["keyword_groups"]

    subtopics = [
        text for text in (_clean_text(s) for s in _as_list(raw.get("subtopics"))) if text
    ][:MAX_SUBTOPICS]

    return {
        "topic": topic,
        "language": language,
        "research_question": (
            _clean_text(raw.get("research_question")) or fallback["research_question"]
        ),
        "scope_summary": _clean_text(raw.get("scope_summary")) or fallback["scope_summary"],
        "keyword_groups": groups,
        "subtopics": subtopics or fallback["subtopics"],
        "time_range": _normalize_time_range(raw.get("time_range"), fallback["time_range"]),
        "inclusion_notes": [
            text for text in (_clean_text(s) for s in _as_list(raw.get("inclusion_notes"))) if text
        ],
        "exclusion_notes": [
            text for text in (_clean_text(s) for s in _as_list(raw.get("exclusion_notes"))) if text
        ],
        "generated_at": (now or datetime.now(UTC)).isoformat(),
    }


def deterministic_scope(
    topic: str,
    *,
    language: str = "en",
    now: datetime | None = None,
) -> dict[str, Any]:
    """不依赖 LLM 的确定性回退：从主题切词构造关键词矩阵与近 N 年时间窗。"""
    clock = now or datetime.now(UTC)
    end_year = clock.year
    start_year = end_year - DEFAULT_YEAR_SPAN + 1
    terms = topic_terms(topic)
    groups = [{"name": term, "keywords": [term]} for term in terms[:MAX_KEYWORD_GROUPS]]
    if not groups:
        groups = [{"name": topic.strip() or "topic", "keywords": [topic.strip() or "topic"]}]
    if language == "zh":
        question = f"{topic} 的研究现状与关键进展是什么？"
        summary = f"围绕「{topic}」检索 {start_year}–{end_year} 年的同行评议文献与预印本。"
    else:
        question = f"What is the state of the art in {topic}?"
        summary = (
            f"Search peer-reviewed literature and preprints on {topic} "
            f"published between {start_year} and {end_year}."
        )
    return {
        "topic": topic,
        "language": language,
        "research_question": question,
        "scope_summary": summary,
        "keyword_groups": groups,
        "subtopics": terms[:MAX_SUBTOPICS] or [topic],
        "time_range": {"start_year": start_year, "end_year": end_year},
        "inclusion_notes": [],
        "exclusion_notes": [],
        "generator": "deterministic",
        "generated_at": clock.isoformat(),
    }


def topic_terms(topic: str) -> list[str]:
    """切出主题中的实义词（中英双语），保持出现顺序且去重。"""
    text = (topic or "").strip()
    if not text:
        return []
    terms: list[str] = []
    seen: set[str] = set()
    for token in re.findall(r"[A-Za-z][A-Za-z0-9\-]+|[一-鿿]{2,}", text):
        lowered = token.lower()
        if lowered in _STOPWORDS_EN or token in _STOPWORDS_ZH:
            continue
        if lowered in seen:
            continue
        seen.add(lowered)
        terms.append(token)
    return terms


def search_queries(scope: dict[str, Any], *, max_queries: int = 4) -> list[str]:
    """由 scope 生成检索式：主查询 + 各概念面组合（provider 侧还会各自净化语法）。"""
    topic = _clean_text(scope.get("topic")) or ""
    groups = [
        group for group in _as_list(scope.get("keyword_groups")) if isinstance(group, dict)
    ]
    queries: list[str] = []
    if topic:
        queries.append(topic)
    if groups:
        joined = " AND ".join(
            "(" + " OR ".join(f'"{kw}"' for kw in _as_list(group.get("keywords"))[:4]) + ")"
            for group in groups[:3]
            if _as_list(group.get("keywords"))
        )
        if joined:
            queries.append(joined)
    for subtopic in _as_list(scope.get("subtopics"))[:2]:
        text = _clean_text(subtopic)
        if not text or text.lower() in topic.lower():
            # 确定性回退的 subtopics 就是主题切词，单独成查询只会稀释召回。
            continue
        queries.append(f"{topic} {text}" if topic else text)
    deduped: list[str] = []
    seen: set[str] = set()
    for query in queries:
        key = query.lower()
        if query and key not in seen:
            seen.add(key)
            deduped.append(query)
    return deduped[:max_queries]


def scope_filters(scope: dict[str, Any]) -> dict[str, Any]:
    """把 scope 的时间窗翻译为 provider 通用 filters。"""
    time_range = scope.get("time_range")
    if not isinstance(time_range, dict):
        return {}
    bounds: dict[str, Any] = {}
    if isinstance(time_range.get("start_year"), int):
        bounds["start_year"] = time_range["start_year"]
    if isinstance(time_range.get("end_year"), int):
        bounds["end_year"] = time_range["end_year"]
    return {"time_range": bounds} if bounds else {}


def _normalize_time_range(raw: Any, fallback: dict[str, int]) -> dict[str, int]:
    if not isinstance(raw, dict):
        return fallback
    start = raw.get("start_year")
    end = raw.get("end_year")
    start = start if isinstance(start, int) and 1800 <= start <= 2200 else fallback["start_year"]
    end = end if isinstance(end, int) and 1800 <= end <= 2200 else fallback["end_year"]
    if start > end:
        start, end = end, start
    return {"start_year": start, "end_year": end}


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value in (None, ""):
        return []
    return [value]


def _clean_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())
