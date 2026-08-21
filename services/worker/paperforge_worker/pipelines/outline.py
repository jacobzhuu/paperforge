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
# 8 条要点对齐 `MAX_PARAGRAPHS_PER_SECTION`（writing.py，现为 8）：一条要点写成
# 一个段落，议程的长度就直接决定了章节的厚度。
MAX_POINTS_PER_SECTION = 8

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

# 框架章节此前的 summary 是空字符串，于是写作提示词里那一行就是「Section goal:」后面
# 什么都没有，argument_points 也是空的。模型没有任何交代，写出来的引言只有 200 字左右；
# 更糟的是质量修复每一轮都会重写这三节，而修复指令是纯减法（「删掉没有证据支撑的论断」），
# 于是每修一轮就短一截——实测（项目 ff6b9983 第 2 版）结论 351 → 136 → 137 字，
# 引言 274 → 179 → 171 字。给它们一份正经的写作交代和各自的篇幅目标，这两件事一起解决。
FRAME_SECTION_BRIEFS: dict[str, dict[str, Any]] = {
    "abstract": {
        "zh": (
            "写这篇综述的摘要：一句话交代研究背景与为什么现在值得综述；"
            "说明综述覆盖的范围与取证方式；概括正文各节得出的主要结论（要具体到机制、"
            "对象或结果，不要只说「进行了讨论」）；点出证据仍然不足之处；最后给出展望。"
            "不分段，不使用引用标记。"
        ),
        "en": (
            "Write the abstract of this review: one sentence of background and why the topic "
            "warrants a review now; the scope covered and how evidence was gathered; the "
            "substantive conclusions of the body sections (name mechanisms, organisms or "
            "results — never just 'is discussed'); where evidence remains insufficient; and a "
            "closing outlook. One paragraph, no citation markers."
        ),
        "target_zh": 350,
        "target_en": 220,
    },
    "introduction": {
        "zh": (
            "写这篇综述的引言，至少三段：第一段交代研究领域的背景与重要性；"
            "第二段说明目前的研究现状与尚未解决的问题——这里要引用正文用到的文献；"
            "第三段说明本综述要回答哪些子问题、如何组织各节。"
            "不要罗列各节标题，要写成连贯的论证。"
        ),
        "en": (
            "Write the introduction of this review in at least three paragraphs: the field and "
            "why it matters; the current state of the art and what remains unresolved, citing "
            "the works used in the body; and the sub-questions this review answers together "
            "with how the sections are organised. Argue in prose; do not list section titles."
        ),
        "target_zh": 1000,
        "target_en": 620,
    },
    "conclusion": {
        "zh": (
            "写这篇综述的结论，至少两段：第一段综合正文各节的发现，给出跨节的判断"
            "（哪些结论证据充分、哪些仍是初步的、不同研究之间在哪里不一致）；"
            "第二段说明本领域下一步最需要什么样的证据或方法。"
            "不要逐节复述，要给出综合判断。"
        ),
        "en": (
            "Write the conclusion of this review in at least two paragraphs: a cross-section "
            "judgement (which conclusions are well supported, which remain preliminary, where "
            "studies disagree), then what evidence or methods the field most needs next. "
            "Synthesise; do not restate the sections one by one."
        ),
        "target_zh": 800,
        "target_en": 500,
    },
}


@dataclass
class OutlineOutcome:
    outline_id: str | None = None
    tree: dict[str, Any] = field(default_factory=dict)
    generator: str = "deterministic"
    section_count: int = 0
    assigned_key_count: int = 0
    orphan_key_count: int = 0
    warnings: list[dict[str, Any]] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        return {
            "outline_id": self.outline_id,
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
    sub_question_bundles: list[dict[str, Any]] | None = None,
) -> OutlineOutcome:
    """产出章节树。永远返回合法大纲。"""
    allowed_cards = [card for card in cards if card.cite_key in whitelist]
    allowed = {card.cite_key for card in allowed_cards}
    if paper_type == "original":
        body = imrad_body_sections(sorted(allowed), language=language)
        generator = "imrad_template"
    elif sub_question_bundles:
        body = question_driven_sections(
            sub_question_bundles,
            language=language,
            allowed=allowed,
        )
        body = await _headline_sections(
            body, topic=topic, language=language, runner=runner
        )
        body = await _headline_subsections(body, language=language, runner=runner)
        generator = "question_evidence_matrix"
    else:
        body, generator = await _body_sections(
            topic=topic,
            research_question=research_question,
            cards=allowed_cards,
            allowed=allowed,
            language=language,
            runner=runner,
        )

    # 问题驱动模式不把“不相关孤儿文献”硬塞进最后一个问题；只有旧兼容路径与
    # original/IMRaD 仍做孤儿回收。
    if not sub_question_bundles:
        body = _reclaim_orphans(body, allowed)
    if paper_type == "review" and len(allowed_cards) >= 2:
        body.append(
            review_synthesis_section(
                allowed_cards,
                language=language,
                sub_question_bundles=sub_question_bundles or [],
            )
        )
        ledger = evidence_ledger_section(
            allowed_cards,
            language=language,
            sub_question_bundles=sub_question_bundles or [],
        )
        if ledger is not None:
            body.append(ledger)
    if paper_type == "review" and review_style == "systematic" and search_method:
        body.insert(0, systematic_method_section(search_method, language=language))
    sections = _with_frame_sections(body, language=language, paper_type=paper_type)
    # The ledger inherits every key in the project, so counting it here made
    # `orphan_key_count` read 0 no matter how many works the body actually
    # ignored.  Frames only ever mirror body keys, so excluding appendices is
    # enough to make the count mean what it says.
    assigned = {
        key
        for section in sections
        if not (section.get("appendix") or section.get("kind") == "appendix")
        for key in section.get("cite_keys", [])
    }
    outcome = OutlineOutcome(
        tree={
            "topic": topic,
            "research_question": research_question,
            "language": language,
            "paper_type": paper_type,
            "review_style": review_style if search_method else "narrative",
            "search_method": search_method,
            "sub_question_bundles": sub_question_bundles or [],
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
    """无问题树的兼容回退：围绕核心证据作单节综合，绝不再按年代分组。"""
    if not cards:
        return []
    return [
        {
            "key": "s1",
            "level": 1,
            "title": (
                "围绕研究问题的现有证据"
                if language == "zh"
                else "Evidence for the Research Question"
            ),
            "summary": (
                "按论断、证据差异与适用边界综合现有研究。"
                if language == "zh"
                else "Synthesize existing studies by claims, evidence differences, and boundaries."
            ),
            "argument_points": [],
            "cite_keys": [card.cite_key for card in cards],
            "kind": "body",
        }
    ]


def question_driven_sections(
    bundles: list[dict[str, Any]],
    *,
    language: str,
    allowed: set[str],
) -> list[dict[str, Any]]:
    """一个子问题对应一个正文论证单元，论证点直接来自 SYNTH 判定。"""
    zh = language == "zh"
    sections: list[dict[str, Any]] = []
    for index, bundle in enumerate(bundles[:MAX_SECTIONS]):
        question_text = str(bundle.get("question") or ("子问题" if zh else "Sub-question"))
        evidence = bundle.get("evidence") or []
        cite_keys = list(
            dict.fromkeys(
                str(item.get("cite_key")) for item in evidence if item.get("cite_key") in allowed
            )
        )
        stance = str(bundle.get("stance_summary") or "insufficient")
        points = _synthesis_argument_points(bundle, language=language)
        # 分级门禁放行的稿子里会有「只有一个来源」的子问题。综述结论需要两个独立
        # 来源，所以这种小节必须显式受限地写：说明证据基础有多窄，不得推广。
        source_count = len({str(item.get("work_id")) for item in evidence if item.get("work_id")})
        evidence_limited = source_count < 2
        section_key = f"q{index + 1}"
        section = {
                "key": section_key,
                "level": 1,
                # 小标题不是子问题原文。子问题是**给写作器和评审器的输入**，写成标题
                # 就成了「CRISPR-Cas 在作物抗病性改良中面临哪些技术挑战（如脱靶效应、
                # 递送方法、多基因编辑）？」这样一行——带问号、带举例括号，导出的 PDF
                # 一眼看去不像论文。问题原文移进 summary，写作器照样知道要答什么。
                "title": _section_heading(question_text, language=language),
                "question": question_text,
                "summary": _section_goal(
                    question_text,
                    stance=stance,
                    synthesis=bundle.get("synthesis"),
                    language=language,
                ),
                "evidence_limited": evidence_limited,
                "distinct_source_count": source_count,
                "argument_points": points[:MAX_POINTS_PER_SECTION],
                "cite_keys": cite_keys,
                "evidence_ids": [
                    str(item.get("evidence_id")) for item in evidence if item.get("evidence_id")
                ],
                "question_id": bundle.get("question_id"),
                "answer_status": bundle.get("answer_status"),
                "stance_summary": stance,
                "comparison_clusters": bundle.get("comparison_clusters") or [],
                "not_comparable_groups": bundle.get("not_comparable_groups") or [],
                "evidence_gap": bundle.get("evidence_gap"),
                # Phase 4：叙述性综合只是随小节透传给写作器的指引，不参与
                # argument_points 的确定性构造，关闭时为 None。
                "synthesis": bundle.get("synthesis"),
                "kind": "body",
        }
        # 有足够的论证要点才拆二级标题；不够就保持扁平，为一两条要点造一个孤立小节
        # 读起来比不拆更糟。母节此时只写引入段，篇幅目标随之下调。
        subsections = _subsections_for(
            _synthesis_content_items(bundle.get("synthesis"), language=language),
            parent_key=section_key,
            parent=section,
            language=language,
        )
        if subsections:
            section["has_children"] = True
            section["target_words"] = (
                PARENT_TARGET_WORDS_ZH if zh else PARENT_TARGET_WORDS_EN
            )
        sections.append(section)
        sections.extend(subsections)
    return sections


#: 少于这个数就不拆小节：为一两条要点造一个孤立的二级标题，读起来比不拆更糟。
MIN_ITEMS_FOR_SUBSECTIONS = 4
#: 每个小节承载 2-3 条要点，最多 4 个小节——再多标题就碎了。
MAX_SUBSECTIONS = 4
ITEMS_PER_SUBSECTION = 2
#: 有小节的母节只写引入段；小节各自成篇。
PARENT_TARGET_WORDS_ZH = 250
PARENT_TARGET_WORDS_EN = 160
SUBSECTION_TARGET_WORDS_ZH = 450
SUBSECTION_TARGET_WORDS_EN = 280


def _subsections_for(
    items: list[dict[str, Any]],
    *,
    parent_key: str,
    parent: dict[str, Any],
    language: str,
) -> list[dict[str, Any]]:
    """Split a section's agenda into level-2 units, or return [] to stay flat."""
    if len(items) < MIN_ITEMS_FOR_SUBSECTIONS:
        return []
    groups: list[list[dict[str, Any]]] = []
    for start in range(0, len(items), ITEMS_PER_SUBSECTION):
        groups.append(items[start : start + ITEMS_PER_SUBSECTION])
    if len(groups) > MAX_SUBSECTIONS:
        # Fold the overflow into the last subsection rather than dropping it.
        head, tail = groups[: MAX_SUBSECTIONS - 1], groups[MAX_SUBSECTIONS - 1 :]
        groups = [*head, [item for group in tail for item in group]]
    zh = language == "zh"
    subsections: list[dict[str, Any]] = []
    for ordinal, group in enumerate(groups, start=1):
        points = [str(item["text"]) for item in group]
        evidence_ids = list(
            dict.fromkeys(value for item in group for value in item["evidence_ids"])
        )
        subsections.append(
            {
                **{
                    key: parent[key]
                    for key in (
                        "question_id",
                        "answer_status",
                        "stance_summary",
                        "evidence_limited",
                        "distinct_source_count",
                        "cite_keys",
                    )
                    if key in parent
                },
                "key": f"{parent_key}s{ordinal}",
                "level": 2,
                "parent_key": parent_key,
                # 占位：`_headline_subsections` 要么给它一个真标题，要么把整个
                # 小节撤掉。从要点原句截断出来的标题试过，全是半截短语。
                "title": "",
                "summary": (
                    "在本小节里把下列要点展开成连贯论证，不要重复上级小节已经说过的话。"
                    if zh
                    else "Develop the points below into a connected argument; do not repeat "
                    "what the parent section already said."
                ),
                "argument_points": points,
                # Fall back to the parent's pool when SYNTH gave an entry no ids,
                # so the writer is never handed an empty evidence ledger.
                "evidence_ids": evidence_ids or list(parent.get("evidence_ids") or []),
                "target_words": SUBSECTION_TARGET_WORDS_ZH if zh else SUBSECTION_TARGET_WORDS_EN,
                "kind": "body",
            }
        )
    return subsections


def _section_goal(
    question_text: str,
    *,
    stance: str,
    synthesis: Any,
    language: str,
) -> str:
    """What this section must establish, not merely which question it answers.

    SYNTH's `claim` is the cross-study conclusion for this sub-question — the
    section's thesis. Leaving it out of `Section goal:` meant the writer was
    told only "answer this question" and had to rediscover the point.
    """
    zh = language == "zh"
    claim = " ".join(str((synthesis or {}).get("claim") or "").split()) if synthesis else ""
    lines = (
        [
            f"回答这个子问题：{question_text}",
            f"当前证据状态：{_stance_label(stance, language)}。",
        ]
        if zh
        else [
            f"Answer this sub-question: {question_text}",
            f"Current evidence status: {_stance_label(stance, language)}.",
        ]
    )
    if claim:
        lines.append(
            f"本节要确立的论点：{claim}" if zh else f"The claim this section establishes: {claim}"
        )
    return "\n".join(lines)


def _synthesis_statement(entry: Any) -> str:
    """The prose statement carried by one synthesis entry."""
    if not isinstance(entry, dict):
        return ""
    return " ".join(str(entry.get("statement") or "").split())


def _synthesis_entry_ids(entry: Any) -> list[str]:
    if not isinstance(entry, dict):
        return []
    return [str(value) for value in entry.get("evidence_ids") or [] if value]


def _synthesis_content_items(synthesis: Any, *, language: str) -> list[dict[str, Any]]:
    """Content points paired with the evidence each one rests on.

    Subsections need the pairing: a subsection that carries three findings must
    also carry those findings' evidence, or `section_evidence_for` hands the
    writer an empty ledger and the sentence rules blank the whole thing.
    """
    items: list[dict[str, Any]] = []
    texts = _synthesis_content_points(synthesis, language=language)
    if not isinstance(synthesis, dict):
        return items
    entries = [
        *(synthesis.get("agreement") or []),
        *(synthesis.get("conditional") or []),
        *(synthesis.get("conflict") or []),
    ]
    # `_synthesis_content_points` walks the same three lists in the same order
    # and skips entries with no statement, so pairing by position is exact.
    kept = [entry for entry in entries if _synthesis_statement(entry)]
    for text, entry in zip(texts, kept, strict=False):
        items.append({"text": text, "evidence_ids": _synthesis_entry_ids(entry)})
    return items


def _synthesis_content_points(synthesis: Any, *, language: str) -> list[str]:
    """Turn SYNTH's findings into the section's actual agenda.

    Each agreement/conditional/conflict is a *finding* — something the section
    has to argue — as opposed to the methodological instructions below, which
    only say how to argue. Measured on a real run: every body section received
    exactly one point, the generic fallback, because `comparison_clusters` was
    empty; the section then came back at 500-700 characters and three retries
    could not fix it, while 8-13 concrete findings sat unused in
    `question_synthesis`.
    """
    if not isinstance(synthesis, dict):
        return []
    zh = language == "zh"
    points: list[str] = []
    for entry in synthesis.get("agreement") or []:
        if statement := _synthesis_statement(entry):
            points.append(statement)
    for entry in synthesis.get("conditional") or []:
        statement = _synthesis_statement(entry)
        if not statement:
            continue
        dimension = " ".join(str((entry or {}).get("dimension") or "").split())
        if dimension:
            points.append(
                f"说明随「{dimension}」变化的条件差异：{statement}"
                if zh
                else f"Explain how this varies with {dimension}: {statement}"
            )
        else:
            points.append(statement)
    for entry in synthesis.get("conflict") or []:
        if statement := _synthesis_statement(entry):
            points.append(
                f"显式呈现这一冲突而不要调和：{statement}"
                if zh
                else f"State this conflict explicitly rather than reconciling it: {statement}"
            )
    return points


def _synthesis_gap_point(synthesis: Any, *, language: str) -> str:
    """At most one gap, in the author's voice.

    SYNTH routinely returns 1-4 gaps per question. Letting all of them become
    agenda items is how a review turns into a report on its own evidence.
    """
    if not isinstance(synthesis, dict):
        return ""
    for entry in synthesis.get("gap") or []:
        if statement := _synthesis_statement(entry):
            # SYNTH 用的是**本文证据**的口吻（"现有证据未提供…"）。原样送进议程就是
            # 把上一轮刚清掉的审计腔重新发给模型。这里不改写它的文字——试过按前缀
            # 剥离，剥出来的是「报告攻击前后 HR 的具体数值变化，无法量化…」这种断句
            # ——而是把改写要求写进指令，让模型把它转成领域现状。
            return (
                f"用一句话交代该问题在文献中尚未解决。"
                f"把下面这条改写成对领域现状的判断，不要出现「证据」「本文」「现有研究未提供」"
                f"这类说法：{statement}"
                if language == "zh"
                else (
                    "Note in one sentence that the literature has not settled this. Recast the "
                    "following as a statement about the field, never about this review's own "
                    f"evidence: {statement}"
                )
            )
    return ""


def _synthesis_argument_points(bundle: dict[str, Any], *, language: str) -> list[str]:
    zh = language == "zh"
    # Content first: these are what give the section something to say.  The
    # methodological points below are modifiers on top of them.
    synthesis = bundle.get("synthesis")
    points: list[str] = _synthesis_content_points(synthesis, language=language)
    gap_point = _synthesis_gap_point(synthesis, language=language)
    for cluster in bundle.get("comparison_clusters") or []:
        classification = str(cluster.get("classification") or "mixed")
        if zh:
            text = {
                "consistent": "综合同一可比条件下方向一致的证据",
                "conditional": "说明同一可比条件下由边界条件造成的差异",
                "conflicting": "显式呈现同一可比条件下无法消解的冲突",
            }.get(classification, "说明混合证据及其不确定性")
        else:
            text = {
                "consistent": "Synthesize evidence that agrees under the same comparable setting",
                "conditional": "Explain differences attributable to boundary conditions",
                "conflicting": "State unresolved conflict under the same comparable setting",
            }.get(classification, "Describe mixed evidence and its uncertainty")
        points.append(text)
    if bundle.get("not_comparable_groups"):
        points.append(
            "分开报告数据集、任务或指标不同的证据，不比较效果量"
            if zh
            else "Report evidence with different datasets, tasks, or metrics separately"
        )
    # 用作者口吻交代领域的空白，而不是复述本系统的检索结果。前者是综述该有的
    # 判断，后者读起来像审计记录——实测 s4/s5 整节都是后者。缺口全节只留一条：
    # SYNTH 每个问题能给出 1-4 条，全放进议程等于把综述写成检索报告。
    if gap_point:
        points.append(gap_point)
    elif bundle.get("evidence_gap"):
        points.append(
            "用一句话指出该问题在文献中尚未被系统研究，不要描述本文的检索或证据评级过程"
            if zh
            else "Note in one sentence that the literature has not yet settled this question; "
            "do not describe this review's own retrieval or evidence grading"
        )
    return points or (
        ["按证据等级陈述现有发现与适用边界"]
        if zh
        else ["State current findings and boundaries according to evidence grade"]
    )


#: 中文疑问式小标题里，把句子拉成问题的那些词。去掉它们剩下的就是主题短语。
_ZH_INTERROGATIVES = (
    "有哪些",
    "哪些",
    "如何",
    "怎样",
    "是什么",
    "为什么",
    "能否",
    "是否",
    "多大程度上",
)
_EN_INTERROGATIVES = (
    "what are the",
    "what is the",
    "what are",
    "what is",
    "how do",
    "how does",
    "how has",
    "how can",
    "which",
    "why do",
    "why does",
    "to what extent",
)


_HEADING_SYSTEM_ZH = """你在给一篇综述论文拟正文小标题。
输入是若干研究子问题，按顺序给每个子问题拟一个小标题。
只输出 JSON：{"headings": ["小标题1", "小标题2", ...]}
要求：
- 数量与顺序必须与输入的子问题一一对应；
- 每条是**名词性短语**，不是问句：不带问号、不带「哪些/如何/是否」这类疑问词；
- 不超过 18 个汉字，不加编号、不加括号举例；
- 各条之间风格一致、彼此可区分，读起来像一篇论文的目录。"""

_HEADING_SYSTEM_EN = """You are naming the body sections of a review paper.
Given a list of research sub-questions, produce one heading per sub-question, in order.
Output JSON only: {"headings": ["heading 1", "heading 2", ...]}
Rules:
- exactly one heading per input question, same order;
- each heading is a NOUN PHRASE, never a question: no question mark, no "what/how/which";
- at most 8 words, no numbering, no parenthetical examples;
- headings must be parallel in style and mutually distinguishable."""


async def _headline_sections(
    sections: list[dict[str, Any]],
    *,
    topic: str,
    language: str,
    runner: LLMRunner | None,
) -> list[dict[str, Any]]:
    """把问题驱动小节的标题换成像论文目录的短语。

    ``_section_heading`` 的确定性结果准确但读着仍像半截句子（「CRISPR-Cas 编辑作物在
    抗病性改良方面取得了具体成果」）。这里花一次 planner 调用把整组标题一起拟出来，
    整组一起拟才能保证风格一致、彼此可区分。模型不可用或返回不合规就沿用确定性结果——
    标题拟不好是遗憾，拟错或者数量对不上是事故。
    """
    targets = [item for item in sections if item.get("question")]
    if not targets or runner is None:
        return sections
    questions = [str(item.get("question") or "") for item in targets]
    result = await runner.agenerate_json(
        "planner",
        system_prompt=(_HEADING_SYSTEM_ZH if language == "zh" else _HEADING_SYSTEM_EN),
        user_prompt="\n".join(
            [f"论文主题：{topic}" if language == "zh" else f"Paper topic: {topic}", "", *(
                f"{index + 1}. {text}" for index, text in enumerate(questions)
            )]
        ),
        max_output_tokens=1200,
        temperature=0.2,
        metadata={"stage": "outline_headings"},
    )
    headings = (result.value or {}).get("headings") if result.ok else None
    if not isinstance(headings, list) or len(headings) != len(targets):
        return sections
    cleaned = [" ".join(str(item).split()).strip("：: ") for item in headings]
    if any(not item or "？" in item or "?" in item for item in cleaned):
        return sections
    if len({item for item in cleaned}) != len(cleaned):
        return sections
    for section, heading in zip(targets, cleaned, strict=True):
        section["title"] = heading
    return sections


_SUBHEADING_SYSTEM_ZH = """你在给一篇综述论文的某一节拟二级小标题。
输入是这一节的标题，以及若干组论证要点（每组对应一个二级小节）。
只输出 JSON：{"headings": ["小标题1", "小标题2", ...]}
要求：
- 数量与顺序必须与输入的要点组一一对应；
- 每条是**名词性短语**，概括该组要点讲的是什么，不是把要点原句截断；
- 不超过 14 个汉字，不带问号、不加编号；
- 同一节内各条彼此可区分，且都比上级标题更具体。"""

_SUBHEADING_SYSTEM_EN = """You are naming the subsections of one section of a review paper.
You get the section title and several groups of argument points, one group per subsection.
Output JSON only: {"headings": ["heading 1", "heading 2", ...]}
Rules:
- exactly one heading per group, same order;
- each is a NOUN PHRASE summarising what that group argues, never a truncated sentence;
- at most 6 words, no question mark, no numbering;
- headings within a section must be mutually distinguishable and more specific than the parent."""


async def _headline_subsections(
    sections: list[dict[str, Any]],
    *,
    language: str,
    runner: LLMRunner | None,
) -> list[dict[str, Any]]:
    """给二级小节拟标题；拟不出来就**把小节撤掉**，退回扁平结构。

    确定性标题在这里行不通——从要点原句截断出来的是「在联邦学习领域」「说明随
    「threat_model」变」这种半截短语，而二级标题是 PDF 里最显眼的东西之一，
    宁可没有也不能是这样。撤掉小节不损失任何内容：母节的 ``argument_points``
    本来就是全部要点的超集，扁平结构照样比改造前厚得多。
    """
    parents = [item for item in sections if item.get("has_children")]
    if not parents:
        return sections
    named: set[str] = set()
    for parent in parents:
        children = [
            item
            for item in sections
            if item.get("parent_key") == parent.get("key") and item.get("level") == 2
        ]
        headings = await _subsection_headings(
            parent, children, language=language, runner=runner
        )
        if headings is None or any(heading in named for heading in headings):
            _drop_children(parent, children)
            continue
        named.update(headings)
        for child, heading in zip(children, headings, strict=True):
            child["title"] = heading
    return [item for item in sections if not item.get("_dropped")]


async def _subsection_headings(
    parent: dict[str, Any],
    children: list[dict[str, Any]],
    *,
    language: str,
    runner: LLMRunner | None,
) -> list[str] | None:
    if not children or runner is None:
        return None
    zh = language == "zh"
    groups = [
        "\n".join(
            [
                f"第 {index + 1} 组：" if zh else f"Group {index + 1}:",
                *(f"  - {point}" for point in child.get("argument_points") or []),
            ]
        )
        for index, child in enumerate(children)
    ]
    result = await runner.agenerate_json(
        "planner",
        system_prompt=(_SUBHEADING_SYSTEM_ZH if zh else _SUBHEADING_SYSTEM_EN),
        user_prompt="\n\n".join(
            [
                (f"本节标题：{parent.get('title')}" if zh else f"Section: {parent.get('title')}"),
                *groups,
            ]
        ),
        max_output_tokens=800,
        temperature=0.2,
        metadata={"stage": "outline_subheadings"},
    )
    headings = (result.value or {}).get("headings") if result.ok else None
    if not isinstance(headings, list) or len(headings) != len(children):
        return None
    cleaned = [" ".join(str(item).split()).strip("：: ") for item in headings]
    limit = 14 if zh else 60
    if any(not item or "？" in item or "?" in item or len(item) > limit for item in cleaned):
        return None
    if len(set(cleaned)) != len(cleaned):
        return None
    return cleaned


def _drop_children(parent: dict[str, Any], children: list[dict[str, Any]]) -> None:
    """Fold subsections back into their parent, restoring the flat contract."""
    for child in children:
        child["_dropped"] = True
    parent.pop("has_children", None)
    # 去掉母节的引入段目标，写作器就回到它自己的正文小节默认篇幅。
    parent.pop("target_words", None)


def _section_heading(question: str, *, language: str) -> str:
    """把一句子问题改写成能印在论文上的小标题。

    确定性做法，不额外调模型：删掉举例括号（「（如脱靶效应、递送方法）」这种在标题里
    只会把一行撑到三十多字）、删掉问号、删掉疑问词。剩下的是主题短语。

    实测（项目 ff6b9983 第 2 版）导出的 PDF 里，五个正文小标题全是子问题原文，最长的
    一条 44 个字并且带着括号和问号。这个函数把它变成「CRISPR-Cas 在作物抗病性改良中
    面临的技术挑战」。删不动时**保留原文**——一个啰嗦但准确的标题，好过一个被截断到
    看不懂的标题。
    """
    text = " ".join(str(question or "").split())
    if not text:
        return "子问题" if language == "zh" else "Sub-question"
    # 举例括号（中英文两种）只在标题里碍事，问题原文仍完整留在 summary 里。
    text = re.sub(r"[（(](?:如|e\.g\.|例如|包括)[^）)]*[）)]", "", text)
    text = text.strip().rstrip("?？.。").strip()
    if language == "zh":
        for word in _ZH_INTERROGATIVES:
            text = text.replace(word, "")
        text = text.replace("  ", " ").strip("，,、 ")
    else:
        lowered = text.lower()
        for word in _EN_INTERROGATIVES:
            if lowered.startswith(word):
                text = text[len(word) :].strip()
                break
    text = " ".join(text.split())
    if not text:
        return " ".join(str(question).split()).rstrip("?？")
    return text


def _cross_study_gap_note(language: str) -> str:
    return (
        "没有可用于跨研究比较的结构化证据。"
        if language == "zh"
        else "No structured evidence is available for cross-study comparison."
    )


def _stance_label(value: str, language: str) -> str:
    if language != "zh":
        return value
    return {
        "consistent": "一致",
        "conditional": "有条件成立",
        "conflicting": "存在冲突",
        "partial": "部分回答",
        "insufficient": "证据不足",
    }.get(value, value)


def review_synthesis_section(
    cards: list[CardBrief],
    *,
    language: str = "en",
    sub_question_bundles: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Create a deterministic synthesis section and a compact literature matrix.

    The table only contains bibliographic metadata, evidence availability, and card-level
    method descriptions. Findings and conflicts remain prose claims so the sentence-level
    evidence gate can verify every substantive conclusion.
    """
    zh = language == "zh"
    headers = (
        ["研究", "任务/数据集", "方法", "关键指标与数值", "证据等级", "定位"]
        if zh
        else ["Study", "Task / dataset", "Method", "Metric and value", "Grade", "Locator"]
    )
    rows: list[list[str]] = []
    card_by_key = {card.cite_key: card for card in cards}
    evidence_rows = [
        item for bundle in sub_question_bundles or [] for item in bundle.get("evidence") or []
    ]
    # R8/R9: main comparison rows are canonical papers, never raw evidence
    # rows.  Its citation/evidence contract is precisely the linked closure.
    by_cite: dict[str, list[dict[str, Any]]] = {}
    for evidence in evidence_rows:
        cite_key = str(evidence.get("cite_key") or "")
        evidence_id = str(evidence.get("evidence_id") or "")
        if cite_key in card_by_key and evidence_id:
            by_cite.setdefault(cite_key, []).append(evidence)
    for cite_key, work_evidence in by_cite.items():
        evidence = work_evidence[0]
        card = card_by_key.get(cite_key)
        measurements = [
            measurement for item in work_evidence for measurement in item.get("measurements") or []
        ]
        measurement = measurements[0] if measurements else {}
        task_dataset = " / ".join(
            value for value in (measurement.get("task"), measurement.get("dataset")) if value
        ) or ("未报告" if zh else "Not reported")
        metric = (
            f"{measurement.get('metric_name')}={measurement.get('value')}"
            f"{measurement.get('unit') or ''}"
            if measurement
            else ("未结构化" if zh else "Not structured")
        )
        locator = ", ".join(
            value
            for value in (
                f"p.{evidence.get('page')}" if evidence.get("page") else "",
                str(evidence.get("section_path") or ""),
                str(evidence.get("object_ref") or ""),
            )
            if value
        ) or ("未定位" if zh else "Unlocated")
        rows.append(
            [
                _short_study_label(str(evidence.get("title") or ""), evidence.get("year")),
                task_dataset,
                (
                    _clean(card.methods[0])
                    if card and card.methods
                    else ("未报告" if zh else "Not reported")
                ),
                metric,
                _grade_label(str(evidence.get("grade") or ""), language=language),
                locator,
            ]
        )
        if len(rows) >= 40:
            break
    cite_keys = list(by_cite)
    evidence_ids = list(
        dict.fromkeys(
            str(item.get("evidence_id")) for item in evidence_rows if item.get("evidence_id")
        )
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
        "cite_keys": cite_keys,
        "evidence_ids": evidence_ids,
        # A bool here reached the writer prompt as the literal line
        # "[EVIDENCE GAP] True".  This field is prose or nothing.
        "evidence_gap": None if rows else _cross_study_gap_note(language),
        "kind": "body",
        "synthesis_kind": "comparison_limitations_conflicts",
        "inline_tables": (
            [
                {
                    "caption": "纳入研究的方法与证据基础比较"
                    if zh
                    else "Methods and evidence basis of included studies",
                    "label": "tab:literature-matrix",
                    "headers": headers,
                    "rows": rows,
                }
            ]
            if rows
            else []
        ),
    }


def evidence_ledger_section(
    cards: list[CardBrief],
    *,
    language: str,
    sub_question_bundles: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Build the appendix-only, evidence-level counterpart of the main table.

    The synthesis table is intentionally one row per canonical study.  This
    ledger is the only place that exposes individual evidence units, and it is
    built from the question-link closure rather than the whole card library.
    """
    zh = language == "zh"
    cards_by_key = {card.cite_key: card for card in cards}
    rows: list[list[str]] = []
    cite_keys: list[str] = []
    seen: set[str] = set()
    evidence_ids: list[str] = []
    for bundle in sub_question_bundles:
        for evidence in bundle.get("evidence") or []:
            evidence_id = str(evidence.get("evidence_id") or "")
            cite_key = str(evidence.get("cite_key") or "")
            if not evidence_id or evidence_id in seen or cite_key not in cards_by_key:
                continue
            seen.add(evidence_id)
            evidence_ids.append(evidence_id)
            cite_keys.append(cite_key)
            locator = ", ".join(
                value
                for value in (
                    str(evidence.get("locator_display") or ""),
                    f"p.{evidence.get('page')}" if evidence.get("page") else "",
                    str(evidence.get("object_ref") or ""),
                )
                if value
            ) or ("未定位" if zh else "Unlocated")
            rows.append(
                [
                    _short_study_label(
                        str(evidence.get("title") or cards_by_key[cite_key].title),
                        evidence.get("year") or cards_by_key[cite_key].year,
                    ),
                    str(evidence.get("text") or "")[:500],
                    _task_label(str(evidence.get("task_id") or ""), language=language),
                    _grade_label(str(evidence.get("grade") or ""), language=language),
                    locator,
                ]
            )
    if not rows:
        return None
    return {
        "key": "evidence_ledger",
        "level": 1,
        "title": "附录：证据台账" if zh else "Appendix: Evidence Ledger",
        "summary": "逐条证据的可追溯定位。"
        if zh
        else "Traceable locations for individual evidence units.",
        "argument_points": [],
        "cite_keys": list(dict.fromkeys(cite_keys)),
        "evidence_ids": evidence_ids,
        "appendix": True,
        "kind": "appendix",
        "synthesis_kind": "evidence_ledger",
        "inline_tables": [
            {
                "caption": "逐条证据与定位" if zh else "Evidence units and source locations",
                "label": "tab:evidence-ledger",
                "headers": (
                    ["研究", "证据摘录", "任务", "证据强度", "定位"]
                    if zh
                    else ["Study", "Evidence excerpt", "Task", "Evidence strength", "Locator"]
                ),
                "rows": rows,
            }
        ],
    }


def _grade_label(grade: str, *, language: str) -> str:
    labels = {
        "A_located_structured": ("表格/公式定位", "Structured location"),
        "B_located_prose": ("正文定位", "Located prose"),
        "C_fulltext_unlocated": ("全文未定位", "Full text, unlocated"),
        "D_abstract_only": ("仅摘要", "Abstract only"),
    }
    return labels.get(grade, ("未评定", "Unassessed"))[0 if language == "zh" else 1]


def _task_label(task_id: str, *, language: str) -> str:
    labels = {
        "bgc.identification": ("BGC 识别", "BGC identification"),
        "bgc.classification": ("BGC 分类", "BGC classification"),
        "bgc.product_structure_prediction": ("产物结构预测", "Product structure prediction"),
        "bgc.product_activity_prediction": ("产物活性预测", "Product activity prediction"),
        "benchmark.dataset_construction": (
            "基准与数据集构建",
            "Benchmark and dataset construction",
        ),
        "tool.engineering": ("工具工程与部署", "Tool engineering"),
        "validation.wetlab": ("湿实验验证", "Wet-lab validation"),
    }
    return labels.get(task_id, ("未报告", "Not reported"))[0 if language == "zh" else 1]


def _short_study_label(title: str, year: Any) -> str:
    cleaned = _clean(title)
    first = re.split(r"[:.。]", cleaned, maxsplit=1)[0][:56]
    return f"{first} ({year})" if year else first


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
    # An introduction and a conclusion that cite nothing are a desk reject, and
    # `FRAME_SECTION_BRIEFS["introduction"]` already tells the model to cite the
    # body's works — it was just handed an empty whitelist, so the prompt read
    # "Available cite keys: (none)".  Hand over what the body actually used.
    # The evidence ids come too: without them every non-background sentence the
    # model writes is blanked by the R4/R6 rules.
    frame_cite_keys, frame_evidence_ids = _body_citation_pool(body)
    sections: list[dict[str, Any]] = []
    for key in FRONT_SECTION_KEYS:
        # Abstracts do not carry citations in any venue we target.
        citable = key != "abstract"
        sections.append(
            {
                "key": key,
                "level": 1,
                "title": frame_titles[key],
                "summary": FRAME_SECTION_BRIEFS[key]["zh" if zh else "en"],
                "target_words": FRAME_SECTION_BRIEFS[key]["target_zh" if zh else "target_en"],
                "argument_points": [],
                "cite_keys": frame_cite_keys if citable else [],
                "evidence_ids": frame_evidence_ids if citable else [],
                "kind": "frame",
            }
        )
    # 母节 s1、s2…，子节 s2-1、s2-2…。此前是无条件 `s{index+1}`，二级标题一进来
    # 就会被编成同级的 s{n}，父子关系当场丢掉。
    key_map: dict[str, str] = {}
    parent_ordinal = 0
    child_ordinal = 0
    for index, section in enumerate(body):
        old_key = str(section.get("key") or "")
        parent_old = str(section.get("parent_key") or "")
        # Preserve stable keys for appendix / ledger sections so downstream
        # writers and templates can address them (N6).
        if section.get("appendix") or section.get("kind") == "appendix":
            key = str(section.get("key") or f"appendix{index + 1}")
        elif parent_old and parent_old in key_map:
            child_ordinal += 1
            key = f"{key_map[parent_old]}-{child_ordinal}"
        else:
            parent_ordinal += 1
            child_ordinal = 0
            key = f"s{parent_ordinal}"
        key_map[old_key] = key
        entry = {**section, "key": key, "order": index}
        if parent_old:
            # 孤儿子节（父节没进大纲）降回一级，而不是留一个指向不存在的 parent_key。
            resolved = key_map.get(parent_old)
            if resolved and resolved != key:
                entry["parent_key"] = resolved
            else:
                entry.pop("parent_key", None)
                entry["level"] = 1
        sections.append(entry)
    for key in BACK_SECTION_KEYS:
        sections.append(
            {
                "key": key,
                "level": 1,
                "title": frame_titles[key],
                "summary": FRAME_SECTION_BRIEFS[key]["zh" if zh else "en"],
                "target_words": FRAME_SECTION_BRIEFS[key]["target_zh" if zh else "target_en"],
                "argument_points": [],
                "cite_keys": frame_cite_keys,
                "evidence_ids": frame_evidence_ids,
                "kind": "frame",
            }
        )
    del paper_type  # 目前两种论文类型共用同一框架章节集合
    return sections


def _body_citation_pool(body: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """Every cite key and evidence id the body sections were given.

    The appendix is skipped: the ledger inherits every key in the project, and
    an introduction should be framed by what the paper argues, not by the audit
    record.  The evidence block applies its own character budget downstream, so
    handing over the full union here does not blow up the prompt.
    """
    keys: dict[str, None] = {}
    ids: dict[str, None] = {}
    for section in body:
        if section.get("appendix") or section.get("kind") == "appendix":
            continue
        for key in section.get("cite_keys") or []:
            keys.setdefault(str(key))
        for evidence_id in section.get("evidence_ids") or []:
            ids.setdefault(str(evidence_id))
    return list(keys), list(ids)


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
