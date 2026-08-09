"""从最终摘要自动生成投稿题名与关键词。

这一层只消费摘要、项目题名和既有研究范围，不接触或补写实验数据。LLM 不可用时
退回确定性提取；无论哪条路径都返回可直接保存的短题名与关键词列表。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from llm_runtime import LLMRunner

_SYSTEM_ZH = """你是学术论文元数据编辑。仅依据给定摘要生成投稿题名与关键词。
只输出 JSON：{"title": "简洁、准确的论文题名", "keywords": ["关键词1", "关键词2"]}。
要求：题名不机械照抄项目名，不添加摘要中没有的结论；给出 3-6 个短关键词，
不要返回完整句子、年份范围、编号、解释或 Markdown。"""

_SYSTEM_EN = """You are an academic metadata editor. Derive a submission title and keywords
only from the supplied abstract. Return JSON only:
{"title": "a concise, accurate paper title", "keywords": ["keyword 1", "keyword 2"]}.
Do not add findings absent from the abstract. Return 3-6 short keywords, with no sentences,
year ranges, numbering, explanations, or Markdown."""

_EN_STOPWORDS = {
    "about",
    "after",
    "also",
    "among",
    "based",
    "been",
    "being",
    "between",
    "both",
    "from",
    "have",
    "into",
    "more",
    "most",
    "paper",
    "results",
    "review",
    "shows",
    "study",
    "that",
    "their",
    "these",
    "this",
    "through",
    "using",
    "were",
    "which",
    "with",
    "within",
}


@dataclass(frozen=True)
class PublicationMetadataDraft:
    title: str
    keywords: list[str]
    generator: str


async def generate_publication_metadata(
    *,
    abstract: str,
    project_title: str,
    scope: dict[str, Any] | None,
    language: str,
    runner: LLMRunner | None,
) -> PublicationMetadataDraft:
    """生成投稿元数据；模型失败时仍返回确定性结果。"""
    fallback = deterministic_publication_metadata(
        abstract=abstract,
        project_title=project_title,
        scope=scope,
        language=language,
    )
    if runner is None or not runner.enabled:
        return fallback

    result = await runner.agenerate_json(
        "writer",
        system_prompt=_SYSTEM_ZH if language == "zh" else _SYSTEM_EN,
        user_prompt=(
            f"项目工作题名：{project_title}\n最终摘要：\n{abstract[:5000]}"
            if language == "zh"
            else f"Working title: {project_title}\nFinal abstract:\n{abstract[:5000]}"
        ),
        max_output_tokens=800,
        temperature=0.2,
        metadata={"stage": "publication_metadata"},
    )
    if not result.ok or not isinstance(result.value, dict):
        return PublicationMetadataDraft(
            title=fallback.title,
            keywords=fallback.keywords,
            generator="deterministic_fallback",
        )

    title = _clean_title(result.value.get("title")) or fallback.title
    keywords = _clean_keywords(result.value.get("keywords")) or fallback.keywords
    return PublicationMetadataDraft(
        title=title,
        keywords=keywords,
        generator=f"llm:{result.model or runner.model_for('writer')}",
    )


def deterministic_publication_metadata(
    *,
    abstract: str,
    project_title: str,
    scope: dict[str, Any] | None,
    language: str,
) -> PublicationMetadataDraft:
    """仅提取摘要中实际出现的研究范围词；英文再以高频实词补足。"""
    cleaned_abstract = " ".join(abstract.split())
    candidates = _scope_candidates(scope or {})
    keywords: list[str] = []
    searchable = cleaned_abstract.casefold()
    for candidate in candidates:
        if candidate.casefold() in searchable and candidate.casefold() not in {
            item.casefold() for item in keywords
        }:
            keywords.append(candidate)
        if len(keywords) >= 6:
            break

    if language != "zh" and len(keywords) < 3:
        counts: dict[str, int] = {}
        order: list[str] = []
        for token in re.findall(r"[A-Za-z][A-Za-z-]{3,}", cleaned_abstract):
            key = token.casefold()
            if key in _EN_STOPWORDS:
                continue
            if key not in counts:
                order.append(key)
                counts[key] = 0
            counts[key] += 1
        for token in sorted(order, key=lambda item: (-counts[item], order.index(item))):
            if token not in {item.casefold() for item in keywords}:
                keywords.append(token)
            if len(keywords) >= 5:
                break

    return PublicationMetadataDraft(
        title=_clean_title(project_title)
        or ("未命名论文" if language == "zh" else "Untitled paper"),
        keywords=keywords,
        generator="deterministic",
    )


def _scope_candidates(scope: dict[str, Any]) -> list[str]:
    raw: list[Any] = [scope.get("topic")]
    raw.extend(scope.get("subtopics") or [])
    raw.extend(scope.get("contribution_points") or [])
    raw.extend(
        section.get("title")
        for section in (scope.get("sections") or [])
        if isinstance(section, dict)
    )
    for group in scope.get("keyword_groups") or []:
        if isinstance(group, dict):
            raw.append(group.get("name"))
            raw.extend(group.get("keywords") or [])
    cleaned: list[str] = []
    seen: set[str] = set()
    for value in raw:
        item = _clean_keyword(value)
        key = item.casefold()
        if item and key not in seen:
            seen.add(key)
            cleaned.append(item)
    return cleaned


def _clean_title(value: Any) -> str:
    title = " ".join(str(value or "").strip().strip("\"'“”").split())
    return title[:240]


def _clean_keyword(value: Any) -> str:
    item = re.sub(r"^[\s\d.、)）(\-–—]+", "", str(value or "").strip())
    item = item.strip(" ,，;；。:：\"'“”")
    return " ".join(item.split())[:64]


def _clean_keywords(value: Any) -> list[str]:
    values = value if isinstance(value, list) else re.split(r"[,，;；、]", str(value or ""))
    keywords: list[str] = []
    seen: set[str] = set()
    for raw in values:
        if re.search(r"\b(?:19|20)\d{2}\b", str(raw)):
            continue
        item = _clean_keyword(raw)
        key = item.casefold()
        if len(item) < 2 or len(item) > 32 or key in seen:
            continue
        seen.add(key)
        keywords.append(item)
        if len(keywords) >= 8:
            break
    return keywords


__all__ = [
    "PublicationMetadataDraft",
    "deterministic_publication_metadata",
    "generate_publication_metadata",
]
