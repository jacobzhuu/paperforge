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
    assigned = {key for section in sections for key in section.get("cite_keys", [])}
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
        sections.append(
            {
                "key": f"q{index + 1}",
                "level": 1,
                "title": str(bundle.get("question") or ("子问题" if zh else "Sub-question")),
                "summary": (
                    f"回答该子问题；当前证据状态：{_stance_label(stance, language)}。"
                    if zh
                    else (
                        "Answer this sub-question; current evidence status: "
                        f"{_stance_label(stance, language)}."
                    )
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
                "kind": "body",
            }
        )
    return sections


def _synthesis_argument_points(bundle: dict[str, Any], *, language: str) -> list[str]:
    zh = language == "zh"
    points: list[str] = []
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
    if bundle.get("evidence_gap"):
        points.append(
            "明确说明现有全文证据不足以回答该子问题"
            if zh
            else "State explicitly that current full-text evidence is insufficient"
        )
    return points or (
        ["按证据等级陈述现有发现与适用边界"]
        if zh
        else ["State current findings and boundaries according to evidence grade"]
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
        "evidence_gap": not bool(rows),
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
        # Preserve stable keys for appendix / ledger sections so downstream
        # writers and templates can address them (N6).
        if section.get("appendix") or section.get("kind") == "appendix":
            key = str(section.get("key") or f"appendix{index + 1}")
        else:
            key = f"s{index + 1}"
        sections.append({**section, "key": key, "order": index})
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
