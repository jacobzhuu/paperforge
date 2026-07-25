"""质量增强：语义引用软校验 + 覆盖建议 + 质量评分（设计 §4.4.3 可选软校验 / §3.4）。

三者都是**提示**，绝不阻断（draft-first）：
- 语义软校验：cheap 模型比对引用上下文与文献摘要的相关性，低分给黄色徽章；
- 覆盖建议：引用密度低 / 缺近年文献 / 某主题未覆盖 —— 由 gap_analysis 降级而来；
- 质量评分：引用密度、覆盖度、新旧文献比、连贯性，只呈现不设门槛
  （取代旧系统 formal completion 的 12 项硬门槛，见设计 §3.4）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from llm_runtime import LLMRunner

SOFT_CHECK_THRESHOLD = 0.5
MAX_SOFT_CHECKS = 40
RECENT_YEARS_WINDOW = 5

_SOFT_CHECK_PROMPT = """判断每条「引用位置的上下文」与「被引文献摘要」是否语义相关。
只输出 JSON：{"judgements": [{"index": 0, "score": 0.0-1.0, "reason": "简短理由"}]}
score 表示该文献能否支撑该处论述；无法判断时给 0.5 并说明。不要臆测摘要之外的内容。"""


@dataclass
class SoftCheckFinding:
    cite_key: str
    section_key: str
    score: float
    reason: str = ""
    context: str = ""

    @property
    def weak(self) -> bool:
        return self.score < SOFT_CHECK_THRESHOLD

    def to_payload(self) -> dict[str, Any]:
        return {
            "cite_key": self.cite_key,
            "section_key": self.section_key,
            "score": round(self.score, 3),
            "reason": self.reason,
            "context": self.context[:200],
            "weak": self.weak,
        }


@dataclass
class QualityReport:
    section_count: int = 0
    word_count: int = 0
    cite_count: int = 0
    unique_cite_count: int = 0
    whitelist_size: int = 0
    citation_density: float = 0.0
    library_coverage: float = 0.0
    recent_ratio: float = 0.0
    fulltext_coverage: float = 0.0
    sections_without_citations: list[str] = field(default_factory=list)
    soft_check: list[dict[str, Any]] = field(default_factory=list)
    hints: list[dict[str, Any]] = field(default_factory=list)
    generated_at: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "section_count": self.section_count,
            "word_count": self.word_count,
            "cite_count": self.cite_count,
            "unique_cite_count": self.unique_cite_count,
            "whitelist_size": self.whitelist_size,
            "citation_density": round(self.citation_density, 4),
            "library_coverage": round(self.library_coverage, 4),
            "recent_ratio": round(self.recent_ratio, 4),
            "fulltext_coverage": round(self.fulltext_coverage, 4),
            "sections_without_citations": self.sections_without_citations,
            "soft_check": self.soft_check,
            "hints": self.hints,
            "generated_at": self.generated_at,
        }


async def soft_check_citations(
    *,
    usages: list[dict[str, Any]],
    abstracts: dict[str, str],
    runner: LLMRunner | None,
    limit: int = MAX_SOFT_CHECKS,
) -> list[SoftCheckFinding]:
    """对引用位置做语义相关性软校验（verifier 角色，便宜档）。

    只产出提示：低分在编辑器里显示黄色徽章，**不删除引用、不阻断渲染**
    （设计 §4.4.3：执行点从「渲染前拒绝」移到「编辑器内提示」）。
    """
    checkable = [
        usage
        for usage in usages
        if usage.get("cite_key") in abstracts and usage.get("context_snippet")
    ][:limit]
    if not checkable or runner is None or not runner.enabled:
        return []

    lines = []
    for index, usage in enumerate(checkable):
        key = str(usage["cite_key"])
        lines.append(
            f"[{index}] 上下文: {str(usage['context_snippet'])[:300]}\n"
            f"     被引文献({key})摘要: {abstracts[key][:400]}"
        )
    result = await runner.agenerate_json(
        "verifier",
        system_prompt=_SOFT_CHECK_PROMPT,
        user_prompt="\n\n".join(lines),
        max_output_tokens=2000,
        temperature=0.0,
        metadata={"stage": "soft_check"},
    )
    if not result.ok or not isinstance(result.value, dict):
        return []

    findings: list[SoftCheckFinding] = []
    for item in result.value.get("judgements") or []:
        if not isinstance(item, dict):
            continue
        index = item.get("index")
        if not isinstance(index, int) or not 0 <= index < len(checkable):
            continue
        raw_score = item.get("score")
        score = float(raw_score) if isinstance(raw_score, int | float) else 0.5
        usage = checkable[index]
        findings.append(
            SoftCheckFinding(
                cite_key=str(usage["cite_key"]),
                section_key=str(usage.get("section_key") or ""),
                score=min(1.0, max(0.0, score)),
                reason=str(item.get("reason") or "")[:200],
                context=str(usage.get("context_snippet") or ""),
            )
        )
    return findings


def coverage_hints(
    *,
    sections: list[dict[str, Any]],
    whitelist_size: int,
    used_keys: set[str],
    publication_years: list[int],
    scope: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """覆盖建议器（由 gap_analysis 降级而来，设计 §3.2）：只提示，绝不阻断。"""
    clock = now or datetime.now(UTC)
    hints: list[dict[str, Any]] = []

    for section in sections:
        key = str(section.get("section_key") or "")
        words = int(section.get("word_count") or 0)
        cites = len(section.get("cite_keys") or [])
        if section.get("kind") == "frame":
            continue
        if words >= 300 and cites == 0:
            hints.append(
                {
                    "kind": "no_citations",
                    "section_key": key,
                    "message": f"「{section.get('title') or key}」有 {words} 字但没有任何引用",
                }
            )
        elif words >= 800 and cites <= 1:
            title = section.get("title") or key
            hints.append(
                {
                    "kind": "low_citation_density",
                    "section_key": key,
                    "message": f"「{title}」引用密度偏低（{cites} 条 / {words} 字）",
                }
            )

    unused = whitelist_size - len(used_keys)
    if whitelist_size and unused > whitelist_size * 0.3:
        hints.append(
            {
                "kind": "unused_library",
                "message": f"入库文献有 {unused} 篇未被正文引用，考虑补充论述或从库中移除",
            }
        )

    recent = [year for year in publication_years if year >= clock.year - RECENT_YEARS_WINDOW]
    if publication_years and len(recent) / len(publication_years) < 0.3:
        hints.append(
            {
                "kind": "stale_library",
                "message": (
                    f"近 {RECENT_YEARS_WINDOW} 年文献仅占 "
                    f"{len(recent)}/{len(publication_years)}，建议补充近期工作"
                ),
            }
        )

    section_titles = " ".join(str(s.get("title") or "") for s in sections).lower()
    topic = str((scope or {}).get("topic") or "").lower()
    for subtopic in (scope or {}).get("subtopics") or []:
        text = str(subtopic).strip()
        # 确定性回退的 subtopics 就是主题切词，逐词提示只会刷屏；
        # 只对「与主题不同的、实义的」子主题给覆盖建议。
        if len(text) < 4 or text.lower() in topic:
            continue
        if text.lower() not in section_titles:
            hints.append(
                {
                    "kind": "uncovered_subtopic",
                    "message": f"SCOPE 中的子主题「{text}」在章节标题里没有对应位置",
                }
            )
    return hints


def build_quality_report(
    *,
    sections: list[dict[str, Any]],
    whitelist_size: int,
    publication_years: list[int],
    fulltext_coverage: float = 0.0,
    soft_check: list[SoftCheckFinding] | None = None,
    scope: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> QualityReport:
    """质量评分报告：只呈现，不设门槛（取代 formal completion 的 12 项硬门槛）。"""
    clock = now or datetime.now(UTC)
    word_count = sum(int(s.get("word_count") or 0) for s in sections)
    used_keys: set[str] = set()
    cite_count = 0
    for section in sections:
        keys = section.get("cite_keys") or []
        cite_count += len(keys)
        used_keys.update(keys)

    recent = [year for year in publication_years if year >= clock.year - RECENT_YEARS_WINDOW]
    report = QualityReport(
        section_count=len(sections),
        word_count=word_count,
        cite_count=cite_count,
        unique_cite_count=len(used_keys),
        whitelist_size=whitelist_size,
        # 每千字引用数：综述通常 8–20 之间。
        citation_density=(cite_count / word_count * 1000) if word_count else 0.0,
        library_coverage=(len(used_keys) / whitelist_size) if whitelist_size else 0.0,
        recent_ratio=(len(recent) / len(publication_years)) if publication_years else 0.0,
        fulltext_coverage=fulltext_coverage,
        sections_without_citations=[
            str(s.get("section_key"))
            for s in sections
            if s.get("kind") != "frame" and not (s.get("cite_keys") or [])
        ],
        soft_check=[f.to_payload() for f in (soft_check or []) if f.weak],
        generated_at=clock.isoformat(),
    )
    report.hints = coverage_hints(
        sections=sections,
        whitelist_size=whitelist_size,
        used_keys=used_keys,
        publication_years=publication_years,
        scope=scope,
        now=clock,
    )
    return report


def count_words(text: str) -> int:
    cjk = len(re.findall(r"[一-鿿]", text or ""))
    latin = len(re.findall(r"[A-Za-z][A-Za-z'-]*", text or ""))
    return cjk + latin


__all__ = [
    "MAX_SOFT_CHECKS",
    "SOFT_CHECK_THRESHOLD",
    "QualityReport",
    "SoftCheckFinding",
    "build_quality_report",
    "count_words",
    "coverage_hints",
    "soft_check_citations",
]
