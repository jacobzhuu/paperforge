"""OUTLINE 阶段：文献卡片聚类 → 章节树（标题 + 分配文献集合 + 论证要点）。

改造自 DeepSearch literature_review/llm_synthesis.py 的 ThemeBundle 聚类框架
（主题提议→合并→孤儿分配→重分区）；去掉 synthesis_claim 类型与证据数量规则（设计 §3.2）。

不变量：
- 章节分配到的 ``cite_keys`` 必须 ⊆ 写作白名单——大纲阶段就把幻觉引用挡在外面；
- 每篇入库文献至少出现在一个章节里（孤儿回收），否则检索来的文献白花力气；
- LLM 不可用时确定性回退（按年份/主题词分组），大纲永远有产物（draft-first）。
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from typing import Any

from llm_runtime import LLMRunner

MAX_SECTIONS = 8
MIN_SECTIONS = 3
MAX_POINTS_PER_SECTION = 6

_SYSTEM_PROMPT_ZH = """你是综述论文的大纲规划助手。根据研究问题与文献卡片列表，输出章节树。
只输出 JSON：
{
  "sections": [
    {
      "title": "章节标题",
      "summary": "本章要论证什么（1-2 句）",
      "argument_points": ["论证要点1", "论证要点2"],
      "cite_keys": ["白名单里的引用键"]
    }
  ]
}
要求：
- 主体章节按**主题**组织，不要按文献逐篇罗列；
- 至少有一节跨文献综合，明确比较研究方法、证据一致与冲突、以及方法局限；
- cite_keys 只能从给定的引用键清单中选，**禁止**发明新的引用键；
- 每篇文献尽量被分配到某一章；确实无关的可以不分配；
- 不要包含摘要/引言/结论章节，它们由系统单独生成。"""

_SYSTEM_PROMPT_EN = """You plan the outline of a review paper. Given a research question and
literature cards, output a section tree. Output JSON only:
{
  "sections": [
    {
      "title": "section title",
      "summary": "what this section argues (1-2 sentences)",
      "argument_points": ["point 1", "point 2"],
      "cite_keys": ["keys from the given whitelist"]
    }
  ]
}
Rules:
- organize body sections by THEME, never as a paper-by-paper list;
- include cross-study synthesis that compares methods, agreements/conflicts, and limitations;
- cite_keys must come from the given key list; never invent a key;
- try to assign every work to some section; genuinely irrelevant ones may be left out;
- do not include abstract/introduction/conclusion — the system generates those separately."""

# 固定框架章节：由系统在正文写完后生成（设计 §4.4.1 「摘要/引言/结论后写」）。
FRONT_SECTION_KEYS = ("abstract", "introduction")
BACK_SECTION_KEYS = ("conclusion",)


@dataclass
class OutlineOutcome:
    tree: dict[str, Any] = field(default_factory=dict)
    generator: str = "deterministic"
    section_count: int = 0
    assigned_key_count: int = 0
    orphan_key_count: int = 0
    warnings: list[dict[str, Any]] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        return {
            "generator": self.generator,
            "section_count": self.section_count,
            "assigned_key_count": self.assigned_key_count,
            "orphan_key_count": self.orphan_key_count,
            "warnings": self.warnings,
        }


@dataclass(frozen=True)
class CardBrief:
    """送进大纲聚类的最小卡片信息。"""

    cite_key: str
    title: str
    year: int | None = None
    summary: str | None = None
    contributions: tuple[str, ...] = ()
    methods: tuple[str, ...] = ()
    results: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    fulltext_used: bool = False


async def generate_outline(
    *,
    topic: str,
    research_question: str,
    cards: list[CardBrief],
    whitelist: set[str],
    language: str = "en",
    paper_type: str = "review",
    runner: LLMRunner | None = None,
    review_style: str = "narrative",
    search_method: dict[str, Any] | None = None,
) -> OutlineOutcome:
    """产出章节树。永远返回合法大纲。"""
    allowed_cards = [card for card in cards if card.cite_key in whitelist]
    allowed = {card.cite_key for card in allowed_cards}
    if paper_type == "original":
        body = imrad_body_sections(sorted(allowed), language=language)
        generator = "imrad_template"
    else:
        body, generator = await _body_sections(
            topic=topic,
            research_question=research_question,
            cards=allowed_cards,
            allowed=allowed,
            language=language,
            runner=runner,
        )

    body = _reclaim_orphans(body, allowed)
    if paper_type == "review" and len(allowed_cards) >= 2:
        body.append(review_synthesis_section(allowed_cards, language=language))
    if paper_type == "review" and review_style == "systematic" and search_method:
        body.insert(0, systematic_method_section(search_method, language=language))
    sections = _with_frame_sections(body, language=language, paper_type=paper_type)
    assigned = {key for section in sections for key in section.get("cite_keys", [])}
    outcome = OutlineOutcome(
        tree={
            "topic": topic,
            "research_question": research_question,
            "language": language,
            "paper_type": paper_type,
            "review_style": review_style if search_method else "narrative",
            "search_method": search_method,
            "sections": sections,
        },
        generator=generator,
        section_count=len(sections),
        assigned_key_count=len(assigned),
        orphan_key_count=len(allowed - assigned),
    )
    return outcome


def systematic_method_section(search_method: dict[str, Any], *, language: str) -> dict[str, Any]:
    providers = ", ".join(search_method.get("databases") or [])
    queries = "; ".join(search_method.get("queries") or [])
    dates = ", ".join(search_method.get("dates") or [])
    retrieved = int(search_method.get("retrieved_count") or 0)
    selected = int(search_method.get("selected_count") or 0)
    criteria = "; ".join(search_method.get("inclusion_criteria") or [])
    if language == "zh":
        title = "检索方法与纳入标准"
        text = (
            f"本综述检索了 {providers}。检索式为：{queries}。检索执行日期为 {dates}。"
            f"共获取 {retrieved} 条记录，最终纳入 {selected} 篇文献。"
            f"纳入标准为：{criteria}。所有数量均直接来自本项目检索日志。"
        )
    else:
        title = "Search Methods and Eligibility Criteria"
        text = (
            f"The review searched {providers}. Queries were: {queries}. Searches were run on "
            f"{dates}. The searches retrieved {retrieved} records and {selected} works were "
            f"included. Inclusion criteria were: {criteria}. All counts come directly from the "
            "project search log."
        )
    return {
        "key": "search_methods",
        "level": 1,
        "title": title,
        "summary": title,
        "argument_points": [],
        "cite_keys": [],
        "kind": "body",
        "deterministic_text": text,
    }


async def _body_sections(
    *,
    topic: str,
    research_question: str,
    cards: list[CardBrief],
    allowed: set[str],
    language: str,
    runner: LLMRunner | None,
) -> tuple[list[dict[str, Any]], str]:
    if runner is None or not runner.enabled or not cards:
        return deterministic_body_sections(cards, language=language), "deterministic"

    lines = []
    for card in cards:
        bits = [f"- [{card.cite_key}] ({card.year or 'n.d.'}) {card.title}"]
        if card.summary:
            bits.append(f"  {card.summary[:220]}")
        if card.contributions:
            bits.append(f"  contributions: {'; '.join(card.contributions[:3])}")
        if card.methods:
            bits.append(f"  methods: {'; '.join(card.methods[:3])}")
        if card.results:
            bits.append(f"  results: {'; '.join(card.results[:3])}")
        if card.limitations:
            bits.append(f"  limitations: {'; '.join(card.limitations[:3])}")
        lines.append("\n".join(bits))

    result = await runner.agenerate_json(
        "planner",
        system_prompt=_SYSTEM_PROMPT_ZH if language == "zh" else _SYSTEM_PROMPT_EN,
        user_prompt=(
            f"Topic: {topic}\nResearch question: {research_question}\n"
            f"Allowed cite keys: {', '.join(sorted(allowed))}\n\n"
            f"Literature cards:\n" + "\n".join(lines)
        ),
        # 推理型模型把思维链算进预算：大纲一次要吐完整章节树，给足额度，
        # 否则每次都要先撞一次 output_truncated 再重试（白花一次调用）。
        max_output_tokens=6000,
        temperature=0.2,
        # R2 的第一道防线就设在大纲：越权 key 直接剔除，不进入写作上下文。
        allowed_cite_keys=allowed,
        mode="strip",
        metadata={"stage": "outline"},
    )
    if not result.ok or not isinstance(result.value, dict):
        return deterministic_body_sections(cards, language=language), "deterministic_fallback"

    sections = _normalize_sections(result.value.get("sections"), allowed=allowed)
    if not sections:
        return deterministic_body_sections(cards, language=language), "deterministic_fallback"
    return sections, f"llm:{result.model}"


def _normalize_sections(raw: Any, *, allowed: set[str]) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    sections: list[dict[str, Any]] = []
    for index, item in enumerate(raw[:MAX_SECTIONS]):
        if not isinstance(item, dict):
            continue
        title = _clean(item.get("title"))
        if not title:
            continue
        cite_keys = [
            key
            for key in (_clean(k) for k in _as_list(item.get("cite_keys")))
            # R2：任何不在白名单里的 key 在此静默剔除并计入告警上游。
            if key in allowed
        ]
        sections.append(
            {
                "key": f"s{index + 1}",
                "level": 1,
                "title": title,
                "summary": _clean(item.get("summary")),
                "argument_points": [
                    point
                    for point in (_clean(p) for p in _as_list(item.get("argument_points")))
                    if point
                ][:MAX_POINTS_PER_SECTION],
                "cite_keys": cite_keys,
                "kind": "body",
            }
        )
    return sections


def deterministic_body_sections(
    cards: list[CardBrief],
    *,
    language: str = "en",
) -> list[dict[str, Any]]:
    """确定性回退：按发表年代分组（近期进展 / 早期工作），保证每篇都有归属。"""
    if not cards:
        return []
    years = [card.year for card in cards if card.year]
    pivot = max(years) - 2 if years else None

    recent = [c for c in cards if pivot is not None and (c.year or 0) >= pivot]
    earlier = [c for c in cards if c not in recent]
    groups: list[tuple[str, list[CardBrief]]] = []
    if language == "zh":
        if earlier:
            groups.append(("研究背景与早期工作", earlier))
        if recent:
            groups.append(("近期研究进展", recent))
        if not groups:
            groups.append(("相关工作", cards))
    else:
        if earlier:
            groups.append(("Background and Earlier Work", earlier))
        if recent:
            groups.append(("Recent Advances", recent))
        if not groups:
            groups.append(("Related Work", cards))

    sections: list[dict[str, Any]] = []
    for index, (title, members) in enumerate(groups):
        sections.append(
            {
                "key": f"s{index + 1}",
                "level": 1,
                "title": title,
                "summary": "",
                # 确定性回退不编造论证要点，只列出可引用文献。
                "argument_points": [],
                "cite_keys": [card.cite_key for card in members],
                "kind": "body",
            }
        )
    return sections


def review_synthesis_section(cards: list[CardBrief], *, language: str = "en") -> dict[str, Any]:
    """Create a deterministic synthesis section and a compact literature matrix.

    The table only contains bibliographic metadata, evidence availability, and card-level
    method descriptions. Findings and conflicts remain prose claims so the sentence-level
    evidence gate can verify every substantive conclusion.
    """
    zh = language == "zh"
    headers = (
        ["研究", "年份", "方法/对象", "证据基础"]
        if zh
        else ["Study", "Year", "Method / setting", "Evidence basis"]
    )
    rows: list[list[str]] = []
    for card in cards[:20]:
        method = _clean(card.methods[0]) if card.methods else ("未报告" if zh else "Not reported")
        evidence = (
            "已定位全文"
            if zh and card.fulltext_used
            else "摘要/元数据"
            if zh
            else "Located full text"
            if card.fulltext_used
            else "Abstract / metadata"
        )
        rows.append(
            [
                _clean(card.title),
                str(card.year) if card.year else ("未注明" if zh else "n.d."),
                method,
                evidence,
            ]
        )
    return {
        "key": "review_synthesis",
        "level": 1,
        "title": "跨研究比较、局限与证据冲突"
        if zh
        else "Cross-study Comparison, Limitations, and Evidence Conflicts",
        "summary": (
            "跨文献比较研究方法与证据基础，区分一致结论、相互冲突的发现和仍未解决的问题。"
            if zh
            else (
                "Compare methods and evidence bases across studies, separating agreements, "
                "conflicting findings, and unresolved questions."
            )
        ),
        "argument_points": (
            [
                "比较研究方法、样本或分析框架，而非逐篇复述",
                "只基于可定位全文证据判断结果一致或冲突",
                "综合文献明确报告的方法局限与证据缺口",
            ]
            if zh
            else [
                "Compare methods, samples, or analytical frameworks rather than listing papers",
                "Judge agreement or conflict only from located full-text evidence",
                "Synthesize explicitly reported methodological limitations and evidence gaps",
            ]
        ),
        "cite_keys": [card.cite_key for card in cards],
        "kind": "body",
        "synthesis_kind": "comparison_limitations_conflicts",
        "inline_tables": [
            {
                "caption": "纳入研究的方法与证据基础比较"
                if zh
                else "Methods and evidence basis of included studies",
                "label": "tab:literature-matrix",
                "headers": headers,
                "rows": rows,
            }
        ],
    }


def imrad_body_sections(cite_keys: list[str], *, language: str = "en") -> list[dict[str, Any]]:
    """研究型论文的 IMRaD 主体骨架（设计 §4.4.2）。

    Related Work 承接文献库；Method/Experiments/Results 由用户素材接地（M4），
    这里只建结构，不产生任何实验数值。
    """
    titles_zh = ["相关工作", "方法", "实验设置", "结果与分析", "讨论"]
    titles_en = ["Related Work", "Method", "Experimental Setup", "Results", "Discussion"]
    titles = titles_zh if language == "zh" else titles_en
    sections: list[dict[str, Any]] = []
    for index, title in enumerate(titles):
        sections.append(
            {
                "key": f"s{index + 1}",
                "level": 1,
                "title": title,
                "summary": "",
                "argument_points": [],
                # 只有 Related Work 默认带文献；其余章节靠用户素材。
                "cite_keys": cite_keys if index == 0 else [],
                "kind": "body",
                "grounding": "library" if index == 0 else "user_asset",
            }
        )
    return sections


def _reclaim_orphans(
    sections: list[dict[str, Any]],
    allowed: set[str],
) -> list[dict[str, Any]]:
    """把没被分配的文献补进最后一个主体章节——检索到的文献不该白白丢掉。"""
    if not sections:
        return sections
    assigned = {key for section in sections for key in section.get("cite_keys", [])}
    orphans = [key for key in sorted(allowed) if key not in assigned]
    if not orphans:
        return sections
    body_sections = [s for s in sections if s.get("kind", "body") == "body"]
    target = body_sections[-1] if body_sections else sections[-1]
    target["cite_keys"] = [*target.get("cite_keys", []), *orphans]
    target["orphans_reclaimed"] = len(orphans)
    return sections


def _with_frame_sections(
    body: list[dict[str, Any]],
    *,
    language: str,
    paper_type: str,
) -> list[dict[str, Any]]:
    """补上摘要/引言/结论：内容在正文写完后生成（设计 §4.4.1）。"""
    zh = language == "zh"
    frame_titles = {
        "abstract": "摘要" if zh else "Abstract",
        "introduction": "引言" if zh else "Introduction",
        "conclusion": "结论" if zh else "Conclusion",
    }
    sections: list[dict[str, Any]] = []
    for key in FRONT_SECTION_KEYS:
        sections.append(
            {
                "key": key,
                "level": 1,
                "title": frame_titles[key],
                "summary": "",
                "argument_points": [],
                "cite_keys": [],
                "kind": "frame",
            }
        )
    for index, section in enumerate(body):
        sections.append({**section, "key": f"s{index + 1}", "order": index})
    for key in BACK_SECTION_KEYS:
        sections.append(
            {
                "key": key,
                "level": 1,
                "title": frame_titles[key],
                "summary": "",
                "argument_points": [],
                "cite_keys": [],
                "kind": "frame",
            }
        )
    del paper_type  # 目前两种论文类型共用同一框架章节集合
    return sections


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value in (None, ""):
        return []
    return [value]


def _clean(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    # Crossref 等来源的题名可能带 <i>/<sub> 一类轻量标记。它们不是 PaperIR
    # 的结构化富文本，既不应原样印进 PDF，也不能为了控制表格高度截断题名。
    without_tags = re.sub(r"<[^>]+>", "", html.unescape(value))
    return " ".join(without_tags.split())
