"""WRITE 阶段：分节结构化生成（段落携带 cite_keys）+ 连贯性 pass。

改造自 DeepSearch literature_review/llm_review_writer.py：
段落携带 cite_keys（而非 claim_ids）；校验从「每段绑定已验证 claim」放宽为
「cite_keys ⊆ 文献库白名单」（R2）；新增连贯性重写 pass。

R2 三道防线（设计 §4.4.3）：
1. prompt 里只给白名单内的 cite key；
2. 首轮以 ``report`` 模式解析，越权 key 触发**带错误信息的重写一次**；
3. 二次违规以 ``strip`` 模式删除，并把 ``citation_warnings`` 持久化进 Section IR；
   解析成 PaperIR 后再执行一次 typed-IR 白名单检查（``enforce_cite_key_whitelist``）。

上下文构造（长文一致性关键，设计 §4.4.1）：
全局大纲 + 本章文献卡片 + 相邻章节滚动摘要 + 术语表（首次出现登记，后续强制沿用）。

红线：正文数字只能来自用户素材的确定性注入（M4）；本阶段 prompt 明令禁止编造实验数值。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from llm_runtime import LLMRunner
from paper_ir import (
    CiteRun,
    GroundingRun,
    ParagraphBlock,
    Section,
    TableBlock,
    TableSource,
    TextRun,
)

MAX_PARAGRAPHS_PER_SECTION = 8
MAX_ROLLING_SUMMARY_CHARS = 600
TARGET_WORDS_PER_SECTION_ZH = 1200
TARGET_WORDS_PER_SECTION_EN = 800
WRITING_CARD_CONTEXT_CHAR_BUDGET = 36_000
MAX_CARD_FIELD_ITEMS = 8
MAX_CARD_EVIDENCE_POINTS = 12

_NO_FABRICATION_ZH = (
    "绝对禁止：编造实验数值、样本量、准确率等任何数字；"
    "凡是给定材料里没有的数字一律不要写，需要时写「结果待补充」。"
)
_NO_FABRICATION_EN = (
    "NEVER fabricate experimental numbers, sample sizes, or scores. "
    "If a number is not in the given material, do not write one; say results are pending."
)

_SYSTEM_PROMPT_ZH = f"""你是学术综述写作助手。根据大纲与文献卡片撰写指定章节。
只输出 JSON：
{{
  "paragraphs": [
    {{"stance_summary": "consistent|conditional|conflicting|insufficient|partial|background",
      "sentences": [
      {{"text": "一个完整论断句", "cite_keys": ["引用键"],
        "evidence_ids": ["直接支撑本句的 EVIDENCE_ID"]}}
    ]}}
  ],
  "terms": [{{"term": "术语", "translation": "译名/缩写"}}]
}}
要求：
- 按主题论证展开，不要逐篇复述文献；每段 3-6 句，观点先行、证据跟随；
- 每个句子的 cite_keys 只能列真正支撑该句的可用引用键；禁止发明或在段末堆整段引用；
- 非背景句必须填写 evidence_ids；只能使用给定 EVIDENCE_ID，数字句必须绑定页码/表格/公式证据；
- 段落按「论断→一致证据→条件差异→冲突/缺口→适用边界」展开；
- 连续“文献A提出…文献B提出…”式归因不得超过 2 句；
- 正文里不要写 [1]、(Smith 2020) 之类的标记——引用由系统按 cite_keys 渲染；
- 沿用给定术语表中的译名与缩写；新术语登记到 terms。
- 正文必须全部使用中文；英文只可作为必要的术语、缩写、数据集、模型名或引用键。
- {_NO_FABRICATION_ZH}"""

_SYSTEM_PROMPT_EN = f"""You write sections of an academic review. Follow the outline and cards.
Output JSON only:
{{
  "paragraphs": [
    {{"stance_summary": "consistent|conditional|conflicting|insufficient|partial|background",
      "sentences": [
      {{"text": "one complete claim sentence (no inline markers)",
       "cite_keys": ["keys supporting only this sentence"],
       "evidence_ids": ["EVIDENCE_ID values directly supporting this sentence"]}}
    ]}}
  ],
  "terms": [{{"term": "term", "translation": "abbreviation or gloss"}}]
}}
Rules:
- argue by theme, never paper-by-paper; 3-6 sentences per paragraph, claim first, evidence after;
- each sentence's cite_keys MUST support that exact sentence and come from the provided list;
  never invent or pile paragraph-wide citations at the end; use [] when unsupported;
- every non-background sentence must declare supplied evidence_ids; numeric claims require a
  page-, table-, or equation-located evidence unit;
- structure paragraphs as claim → agreement → conditional difference → conflict/gap → boundary;
- never write more than two consecutive paper-by-paper attribution sentences;
- do not write inline markers like [1] or (Smith 2020) — the system renders citations;
- reuse the given glossary terms consistently; register new terms in `terms`.
- write all prose in English; retain another language only for essential proper names or quotations.
- {_NO_FABRICATION_EN}"""

_ORIGINAL_SYSTEM_PROMPT_ZH = f"""你是原创研究论文写作助手。只依据提供的方法与结果素材撰写指定章节。
只输出 JSON：{{"paragraphs":[{{"stance_summary":"background|partial|conditional",
"sentences":[{{"text":"完整句子","cite_keys":[],"evidence_ids":[],
"source_refs":["直接支撑本句的 SOURCE_REF"]}}]}}],"terms":[]}}。
要求：方法、实验设置、结果、讨论和结论中的事实句必须填写给定 SOURCE_REF；相关工作使用给定
cite_keys/evidence_ids；不得发明来源标识、实验步骤、数据或数字；数字只能逐字取自绑定素材。
无法由素材支撑的内容不要写。正文必须使用中文。{_NO_FABRICATION_ZH}"""

_ORIGINAL_SYSTEM_PROMPT_EN = f"""You write an original research paper using only the supplied
method and result assets. Return JSON only with paragraphs/sentences; every sentence has text,
cite_keys, evidence_ids, and source_refs. Method, setup, result, discussion, and conclusion facts
must bind supplied SOURCE_REF values. Related work uses supplied cite_keys/evidence_ids. Never
invent a source, procedure, result, or number; numbers must be copied exactly from a bound asset.
Omit claims that the sources cannot support. Write all prose in English. {_NO_FABRICATION_EN}"""

_COHERENCE_PROMPT_ZH = """你是学术论文的连贯性编辑。给定相邻章节的正文，改写目标章节，使其：
过渡自然、术语一致、不与其他章节重复论述。只输出 JSON，结构与输入相同：
{"paragraphs": [{"stance_summary": "...", "sentences":
[{"text": "...", "cite_keys": [...], "evidence_ids": [...],
"source_refs": [...]}]}]}
不得新增、删除或调换引用键、evidence_ids 与 source_refs，不得改动或新增任何数字。"""

_COHERENCE_PROMPT_EN = """You are a coherence editor. Rewrite the target section so that it
transitions smoothly, uses consistent terminology, and does not repeat neighbouring sections.
Output JSON with the same sentence-level shape:
{"paragraphs": [{"stance_summary": "...", "sentences":
[{"text": "...", "cite_keys": [...], "evidence_ids": [...],
"source_refs": [...]}]}]}
Never add, remove, or swap cite keys, evidence_ids, or source_refs. Never add or change any
number."""


@dataclass
class SectionDraft:
    section_key: str
    title: str
    appendix: bool = False
    paragraphs: list[dict[str, Any]] = field(default_factory=list)
    inline_tables: list[dict[str, Any]] = field(default_factory=list)
    citation_warnings: list[dict[str, Any]] = field(default_factory=list)
    terms: dict[str, str] = field(default_factory=dict)
    model: str | None = None
    rewrite_count: int = 0
    generator: str = "deterministic"

    @property
    def word_count(self) -> int:
        return sum(count_words(p.get("text", "")) for p in self.paragraphs)

    def to_ir_section(self, *, level: int = 1) -> Section:
        """转成 PaperIR Section：cite 是原子节点，不是正文里的字符串。"""
        blocks: list[Any] = []
        for paragraph in self.paragraphs:
            sentence_rows = [
                sentence
                for sentence in (paragraph.get("sentences") or [])
                if str(sentence.get("text") or "").strip()
            ]
            runs: list[Any] = []
            if sentence_rows:
                for index, sentence in enumerate(sentence_rows):
                    text = str(sentence.get("text") or "").strip()
                    if not text:
                        continue
                    runs.append(TextRun(v=text))
                    keys = [key for key in sentence.get("cite_keys", []) if key]
                    if keys:
                        runs.append(
                            CiteRun(
                                keys=list(dict.fromkeys(keys)),
                                evidence_ids=list(
                                    dict.fromkeys(
                                        str(value)
                                        for value in sentence.get("evidence_ids", [])
                                        if value
                                    )
                                ),
                            )
                        )
                    source_refs = [str(value) for value in sentence.get("source_refs", []) if value]
                    if source_refs:
                        runs.append(GroundingRun(source_refs=list(dict.fromkeys(source_refs))))
                    if index < len(sentence_rows) - 1:
                        runs.append(TextRun(v=" "))
            else:
                text = str(paragraph.get("text") or "").strip()
                if text:
                    runs.append(TextRun(v=text))
                    keys = [key for key in paragraph.get("cite_keys", []) if key]
                    if keys:
                        runs.append(CiteRun(keys=list(dict.fromkeys(keys))))
            if not runs:
                continue
            blocks.append(
                ParagraphBlock(
                    runs=runs,
                    stance_summary=paragraph.get("stance_summary"),
                )
            )
        for table in self.inline_tables:
            headers = table.get("headers") or []
            rows = table.get("rows") or []
            if headers and rows:
                blocks.append(
                    TableBlock(
                        source=TableSource(
                            kind="inline",
                            data={"headers": headers, "rows": rows},
                        ),
                        caption=str(table.get("caption") or ""),
                        label=str(table.get("label") or "") or None,
                    )
                )
        section = Section(
            key=self.section_key,
            level=level,
            title=self.title,
            appendix=self.appendix,
            blocks=blocks,
        )
        for warning in self.citation_warnings:
            section.citation_warnings.append(
                {  # type: ignore[arg-type]
                    "path": warning.get("path", ""),
                    "rejected_keys": tuple(warning.get("rejected_keys", ())),
                    "message": warning.get("message", "引用已移除：引用键不在项目写作白名单中"),
                }
            )
        return section


@dataclass
class WritingContext:
    """一次写作运行内共享的滚动上下文（长文一致性的关键）。"""

    outline: dict[str, Any]
    language: str = "en"
    paper_type: str = "review"
    glossary: dict[str, str] = field(default_factory=dict)
    rolling_summaries: dict[str, str] = field(default_factory=dict)

    def preceding_summary(self, section_key: str) -> str:
        sections = self.outline.get("sections") or []
        keys = [s.get("key") for s in sections]
        if section_key not in keys:
            return ""
        index = keys.index(section_key)
        previous = [
            self.rolling_summaries.get(key, "")
            for key in keys[max(0, index - 2) : index]
            if self.rolling_summaries.get(key)
        ]
        return "\n".join(previous)[-MAX_ROLLING_SUMMARY_CHARS:]

    def register(self, section_key: str, draft: SectionDraft) -> None:
        self.rolling_summaries[section_key] = summarize_paragraphs(draft.paragraphs)
        for term, translation in draft.terms.items():
            self.glossary.setdefault(term, translation)


async def write_section(
    *,
    section: dict[str, Any],
    cards: dict[str, dict[str, Any]],
    whitelist: set[str],
    context: WritingContext,
    runner: LLMRunner | None = None,
    assets: list[dict[str, Any]] | None = None,
) -> SectionDraft:
    """生成单个章节。R2 违规先重写一次，再违规则 strip 并留告警。"""
    section_key = str(section.get("key") or "section")
    title = str(section.get("title") or section_key)
    allowed = {key for key in section.get("cite_keys", []) if key in whitelist}
    section_evidence = _section_evidence(section, context.outline)
    evidence_by_id = {
        str(item.get("evidence_id")): item for item in section_evidence if item.get("evidence_id")
    }
    allowed_evidence_ids = set(evidence_by_id)
    source_assets = _source_assets_for_section(
        assets or [],
        section=section,
        paper_type=context.paper_type,
    )
    assets_by_ref = {
        str(item.get("_asset_ref")): item for item in source_assets if item.get("_asset_ref")
    }
    allowed_source_refs = set(assets_by_ref)

    draft = SectionDraft(
        section_key=section_key,
        title=title,
        appendix=bool(section.get("appendix")),
        inline_tables=[
            dict(table) for table in section.get("inline_tables") or [] if isinstance(table, dict)
        ],
    )
    if section.get("deterministic_text"):
        draft.paragraphs = [{"text": str(section["deterministic_text"]), "cite_keys": []}]
        draft.generator = "deterministic_search_log"
        return draft
    # R15 / N0-5: never call the LLM to freely write a body section with an empty
    # cite-key whitelist. That path produced fluent, unsourced prose.
    body_without_cites = (
        section.get("kind", "body") == "body"
        and not allowed
        and not section_evidence
        and not allowed_source_refs
    )
    if body_without_cites or runner is None or not runner.enabled:
        if context.paper_type == "original" and allowed_source_refs:
            draft.paragraphs = deterministic_asset_paragraphs(
                section,
                source_assets,
                language=context.language,
            )
        else:
            draft.paragraphs = deterministic_paragraphs(
                section,
                cards,
                allowed,
                evidence=section_evidence,
                language=context.language,
            )
            draft.paragraphs = enforce_sentence_evidence_rules(
                draft.paragraphs,
                evidence_by_id=evidence_by_id,
                language=context.language,
            )
        draft.generator = "evidence_gap_skeleton" if body_without_cites else "deterministic"
        return draft

    user_prompt = _build_prompt(
        section=section,
        cards=cards,
        allowed=allowed,
        context=context,
        assets=assets or [],
    )
    system_prompt = (
        _ORIGINAL_SYSTEM_PROMPT_ZH
        if context.paper_type == "original" and context.language == "zh"
        else _ORIGINAL_SYSTEM_PROMPT_EN
        if context.paper_type == "original"
        else _SYSTEM_PROMPT_ZH
        if context.language == "zh"
        else _SYSTEM_PROMPT_EN
    )

    # 第一轮：report 模式——不改动内容，只报告越权 key。
    result = await runner.agenerate_json(
        "writer",
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        max_output_tokens=4000,
        temperature=0.4,
        allowed_cite_keys=allowed,
        mode="report",
        metadata={"stage": "write", "section": section_key},
    )
    if result.ok and result.violations:
        # R2 第二道：把越权 key 回传给模型，要求重写一次。
        draft.rewrite_count = 1
        rejected = sorted({key for v in result.violations for key in v.rejected_keys})
        retry_prompt = (
            f"{user_prompt}\n\n---\n上一版使用了不在白名单中的引用键：{', '.join(rejected)}。"
            if context.language == "zh"
            else (
                f"{user_prompt}\n\n---\n"
                f"Your previous answer used cite keys outside the whitelist: "
                f"{', '.join(rejected)}."
            )
        ) + (
            f"\n只能使用：{', '.join(sorted(allowed)) or '（无可用引用键，请给空数组）'}"
            if context.language == "zh"
            else f"\nUse only: {', '.join(sorted(allowed)) or '(none — return empty arrays)'}"
        )
        result = await runner.agenerate_json(
            "writer",
            system_prompt=system_prompt,
            user_prompt=retry_prompt,
            max_output_tokens=4000,
            temperature=0.2,
            # 第三道：二次违规直接 strip，并把告警留给编辑器显示。
            allowed_cite_keys=allowed,
            mode="strip",
            metadata={"stage": "write_retry", "section": section_key},
        )
        for violation in result.violations:
            draft.citation_warnings.append(
                {
                    "path": violation.path,
                    "rejected_keys": tuple(violation.rejected_keys),
                    "message": "引用已移除：引用键不在项目写作白名单中",
                }
            )

    if not result.ok or not isinstance(result.value, dict):
        if context.paper_type == "original" and allowed_source_refs:
            draft.paragraphs = deterministic_asset_paragraphs(
                section,
                source_assets,
                language=context.language,
            )
        else:
            draft.paragraphs = deterministic_paragraphs(
                section,
                cards,
                allowed,
                evidence=section_evidence,
                language=context.language,
            )
            draft.paragraphs = enforce_sentence_evidence_rules(
                draft.paragraphs,
                evidence_by_id=evidence_by_id,
                language=context.language,
            )
        draft.generator = "deterministic_fallback"
        return draft

    draft.paragraphs = normalize_paragraphs(
        result.value.get("paragraphs"),
        allowed=allowed,
        allowed_evidence_ids=allowed_evidence_ids,
        allowed_source_refs=allowed_source_refs,
    )
    if context.paper_type == "original" and allowed_source_refs:
        draft.paragraphs = enforce_sentence_grounding_rules(
            draft.paragraphs,
            assets_by_ref=assets_by_ref,
            require_grounding=section.get("grounding") != "library",
            require_numeric=section_key == "s4",
        )
    elif context.paper_type != "original":
        draft.paragraphs = enforce_sentence_evidence_rules(
            draft.paragraphs,
            evidence_by_id=evidence_by_id,
            language=context.language,
        )
    draft.terms = normalize_terms(result.value.get("terms"))
    draft.model = result.model
    draft.generator = f"llm:{result.model}"
    if not draft.paragraphs:
        if context.paper_type == "original" and allowed_source_refs:
            draft.paragraphs = deterministic_asset_paragraphs(
                section,
                source_assets,
                language=context.language,
            )
        else:
            draft.paragraphs = deterministic_paragraphs(
                section,
                cards,
                allowed,
                evidence=section_evidence,
                language=context.language,
            )
            draft.paragraphs = enforce_sentence_evidence_rules(
                draft.paragraphs,
                evidence_by_id=evidence_by_id,
                language=context.language,
            )
        draft.generator = "deterministic_fallback"
    return draft


async def coherence_pass(
    *,
    draft: SectionDraft,
    context: WritingContext,
    whitelist: set[str],
    runner: LLMRunner | None = None,
) -> SectionDraft:
    """连贯性重写：只调整行文，不新增引用、不改数字。"""
    if runner is None or not runner.enabled or not draft.paragraphs:
        return draft
    allowed = {key for p in draft.paragraphs for key in p.get("cite_keys", [])} & whitelist
    body = "\n\n".join(p.get("text", "") for p in draft.paragraphs)
    numbers_before = extract_numbers(body)

    result = await runner.agenerate_json(
        "writer",
        system_prompt=_COHERENCE_PROMPT_ZH if context.language == "zh" else _COHERENCE_PROMPT_EN,
        user_prompt=(
            f"Preceding sections summary:\n{context.preceding_summary(draft.section_key)}\n\n"
            f"Glossary: {_format_glossary(context.glossary)}\n\n"
            f"Target section: {draft.title}\n"
            f"Allowed cite keys: {', '.join(sorted(allowed)) or '(none)'}\n\n"
            f"Current paragraphs JSON:\n{_paragraphs_json(draft.paragraphs)}"
        ),
        max_output_tokens=4000,
        temperature=0.2,
        allowed_cite_keys=allowed,
        mode="strip",
        metadata={"stage": "coherence", "section": draft.section_key},
    )
    if not result.ok or not isinstance(result.value, dict):
        return draft
    allowed_evidence_ids = {
        str(evidence_id)
        for paragraph in draft.paragraphs
        for sentence in paragraph.get("sentences") or []
        for evidence_id in sentence.get("evidence_ids") or []
    }
    allowed_source_refs = {
        str(source_ref)
        for paragraph in draft.paragraphs
        for sentence in paragraph.get("sentences") or []
        for source_ref in sentence.get("source_refs") or []
    }
    rewritten = normalize_paragraphs(
        result.value.get("paragraphs"),
        allowed=allowed,
        allowed_evidence_ids=allowed_evidence_ids,
        allowed_source_refs=allowed_source_refs,
    )
    if not rewritten:
        return draft
    evidence_ids_after = {
        str(evidence_id)
        for paragraph in rewritten
        for sentence in paragraph.get("sentences") or []
        for evidence_id in sentence.get("evidence_ids") or []
    }
    # 连贯性编辑只能改措辞，不能悄悄增删句子的证据绑定。
    if evidence_ids_after != allowed_evidence_ids:
        return draft
    source_refs_after = {
        str(source_ref)
        for paragraph in rewritten
        for sentence in paragraph.get("sentences") or []
        for source_ref in sentence.get("source_refs") or []
    }
    if source_refs_after != allowed_source_refs:
        return draft
    # 连贯性 pass 不得引入新数字：一旦发现新数值，放弃改写保留原稿（红线优先于文采）。
    numbers_after = extract_numbers("\n\n".join(p.get("text", "") for p in rewritten))
    if numbers_after - numbers_before:
        return draft
    draft.paragraphs = rewritten
    return draft


def _build_prompt(
    *,
    section: dict[str, Any],
    cards: dict[str, dict[str, Any]],
    allowed: set[str],
    context: WritingContext,
    assets: list[dict[str, Any]] | None = None,
) -> str:
    zh = context.language == "zh"
    target = TARGET_WORDS_PER_SECTION_ZH if zh else TARGET_WORDS_PER_SECTION_EN
    outline_titles = [
        str(s.get("title")) for s in (context.outline.get("sections") or []) if s.get("title")
    ]
    card_lines: list[str] = []
    remaining_context = WRITING_CARD_CONTEXT_CHAR_BUDGET
    per_card_budget = max(1_200, remaining_context // max(1, len(allowed)))
    for key in sorted(allowed):
        card = cards.get(key) or {}
        parts = [f"[{key}] {card.get('title', '')} ({card.get('year') or 'n.d.'})"]
        if card.get("summary"):
            parts.append(f"  summary: {str(card['summary'])[:600]}")
        for field_name in ("contributions", "methods", "results", "limitations"):
            values = card.get(field_name) or []
            if values:
                parts.append(
                    f"  {field_name}: {'; '.join(str(v) for v in values[:MAX_CARD_FIELD_ITEMS])}"
                )
        if card.get("fulltext_used"):
            points = card.get("quotable_points") or []
            located = [point for point in points if isinstance(point, dict) and point.get("text")]
            located.sort(
                key=lambda point: bool(
                    point.get("page") or point.get("section") or point.get("paragraph")
                ),
                reverse=True,
            )
            for point in located[:MAX_CARD_EVIDENCE_POINTS]:
                locator = ", ".join(
                    item
                    for item in (
                        f"p.{point['page']}" if point.get("page") else "",
                        str(point.get("section") or ""),
                        f"para.{point['paragraph']}" if point.get("paragraph") else "",
                    )
                    if item
                )
                parts.append(f"  fulltext evidence ({locator or 'unlocated'}): {point['text']}")
        # 不再对所有文献机械 [:3]；按章节总 token 预算和文献数动态分配，
        # 同时保证每篇至少保留题名行。
        rendered = _truncate_card_context(parts, min(per_card_budget, remaining_context))
        card_lines.append(rendered)
        remaining_context = max(0, remaining_context - len(rendered))

    points = section.get("argument_points") or []
    section_evidence = _section_evidence(section, context.outline)
    evidence_block = _evidence_context_block(
        section=section,
        evidence=section_evidence,
        language=context.language,
    )
    return "\n".join(
        [
            f"Paper topic: {context.outline.get('topic', '')}",
            f"Research question: {context.outline.get('research_question', '')}",
            f"Full outline: {' | '.join(outline_titles)}",
            f"Preceding sections summary:\n{context.preceding_summary(str(section.get('key')))}",
            f"Glossary (reuse these): {_format_glossary(context.glossary)}",
            "",
            f"Write section: {section.get('title')}",
            f"Section goal: {section.get('summary') or ''}",
            f"Argument points: {'; '.join(str(p) for p in points) or '(derive from cards)'}",
            f"Target length: about {target} {'字' if zh else 'words'}",
            f"Available cite keys: {', '.join(sorted(allowed)) or '(none)'}",
            _evidence_limitation_line(section, language=context.language),
            "",
            ("Question-aligned evidence clusters:" if section_evidence else "Literature cards:"),
            evidence_block
            if section_evidence
            else ("\n\n".join(card_lines) or "(no cards assigned to this section)"),
            "",
            _asset_block(
                assets or [],
                language=context.language,
                section=section,
                paper_type=context.paper_type,
                cards=[cards.get(key) or {} for key in sorted(allowed)],
                evidence=section_evidence,
            ),
        ]
    )


def _evidence_limitation_line(section: dict[str, Any], *, language: str) -> str:
    """证据基础不足两个独立来源时，正文必须显式声明限度。

    分级门禁允许覆盖率不达标的稿子继续产出，代价必须是「说清楚证据有多窄」，
    而不是让 LLM 用一篇文献写出一段听起来像综述结论的话。
    """
    if not section.get("evidence_limited"):
        return ""
    count = int(section.get("distinct_source_count") or 0)
    if language == "zh":
        return (
            f"证据限度（必须遵守）：本节可用证据仅来自 {count} 篇独立文献。"
            "只能陈述该证据直接支持的内容，明确指出这是单一来源的初步发现，"
            "不得推广为一般结论，并在结尾用一句话点明缺口。"
        )
    return (
        f"Evidence limitation (mandatory): only {count} independent source(s) support this "
        "section. State only what that evidence directly supports, mark it explicitly as a "
        "single-source preliminary finding, do not generalise, and close with one sentence "
        "naming the gap."
    )


def _truncate_card_context(parts: list[str], char_budget: int) -> str:
    if not parts:
        return ""
    if char_budget <= 0:
        return parts[0]
    kept = [parts[0]]
    used = len(parts[0])
    for part in parts[1:]:
        remaining = char_budget - used - 1
        if remaining <= 0:
            break
        kept.append(part if len(part) <= remaining else part[:remaining])
        used += len(kept[-1]) + 1
        if len(part) > remaining:
            break
    return "\n".join(kept)


def _section_evidence(
    section: dict[str, Any],
    outline: dict[str, Any],
) -> list[dict[str, Any]]:
    question_id = str(section.get("question_id") or "")
    if not question_id:
        return []
    bundle: dict[str, Any] = next(
        (
            item
            for item in outline.get("sub_question_bundles") or []
            if str(item.get("question_id") or "") == question_id
        ),
        {},
    )
    return [item for item in bundle.get("evidence") or [] if isinstance(item, dict)]


def _evidence_context_block(
    *,
    section: dict[str, Any],
    evidence: list[dict[str, Any]],
    language: str,
) -> str:
    if not evidence:
        return "(no question-aligned evidence)"
    lines = [
        f"Sub-question: {section.get('title')}",
        f"SYNTH stance: {section.get('stance_summary') or 'insufficient'}",
    ]
    for cluster in section.get("comparison_clusters") or []:
        lines.append(
            f"[COMPARISON CLUSTER {cluster.get('comparability_key')}] "
            f"classification={cluster.get('classification')} "
            f"evidence_ids={','.join(cluster.get('evidence_ids') or [])}"
        )
    if section.get("not_comparable_groups"):
        lines.append(
            "[NOT COMPARABLE] The following comparability keys must be reported separately: "
            + ", ".join(
                str(item.get("comparability_key")) for item in section["not_comparable_groups"]
            )
        )
    grade_order = {
        "A_located_structured": 0,
        "B_located_prose": 1,
        "C_fulltext_unlocated": 2,
        "D_abstract_only": 3,
    }
    used = len("\n".join(lines))
    for item in sorted(evidence, key=lambda row: grade_order.get(str(row.get("grade")), 9)):
        grade = str(item.get("grade") or "")
        locator = (
            ", ".join(
                value
                for value in (
                    f"p.{item.get('page')}" if item.get("page") else "",
                    str(item.get("section_path") or ""),
                    str(item.get("object_ref") or ""),
                )
                if value
            )
            or "unlocated"
        )
        measurements = "; ".join(
            f"{row.get('metric_name')}={row.get('value')}{row.get('unit') or ''} "
            f"dataset={row.get('dataset') or 'unknown'} "
            f"comparability_key={row.get('comparability_key')}"
            for row in item.get("measurements") or []
        )
        restriction = (
            " [ABSTRACT-ONLY — 只可用于背景或“该文献报告”式转述]"
            if language == "zh" and grade == "D_abstract_only"
            else " [ABSTRACT-ONLY — background or attribution only]"
            if grade == "D_abstract_only"
            else ""
        )
        rendered = (
            f"EVIDENCE_ID={item.get('evidence_id')} cite_key={item.get('cite_key')} "
            f"grade={grade} kind={item.get('kind')} stance={item.get('stance')}"
            f"{restriction}\n"
            f"  locator={locator}\n"
            f"  text={str(item.get('text') or '')[:1600]}\n"
            f"  measurements={measurements or '(none)'}"
        )
        if used + len(rendered) > WRITING_CARD_CONTEXT_CHAR_BUDGET:
            break
        lines.append(rendered)
        used += len(rendered)
    if section.get("evidence_gap"):
        lines.append(f"[EVIDENCE GAP] {section['evidence_gap']}")
    return "\n".join(lines)


def _asset_block(
    assets: list[dict[str, Any]],
    *,
    language: str,
    section: dict[str, Any],
    paper_type: str,
    cards: list[dict[str, Any]],
    evidence: list[dict[str, Any]] | None = None,
) -> str:
    """素材接地块。

    数字红线（设计 §4.4.2）：正文数字必须来自 parsed_json 的确定性注入。
    这里把可用数值原样列给模型作为**唯一允许写出的数字**；一个素材都没有时，
    明确要求写占位符而不是编数字——表格与图另由渲染器确定性展开，模型只写 caption。
    """
    zh = language == "zh"
    assets = _source_assets_for_section(
        assets,
        section=section,
        paper_type=paper_type,
    )
    grounding = section.get("grounding")
    located_fulltext = any(
        card.get("fulltext_used")
        and any(
            isinstance(point, dict)
            and point.get("text")
            and (point.get("page") or point.get("section") or point.get("paragraph"))
            for point in card.get("quotable_points") or []
        )
        for card in cards
    )
    if paper_type == "review":
        if evidence:
            grades = sorted(
                {str(item.get("grade") or "") for item in evidence if item.get("grade")}
            )
            return (
                "综述证据规则：严格按每条 EVIDENCE_ID 的 grade 与定位写作；A/B 可支撑"
                "核心论断，C 只能在明确不确定性时支撑效果/结论，D 只能支撑背景或归因。"
                f"本节证据等级：{', '.join(grades)}。跨研究效果比较只能发生在相同 "
                "comparability_key 内。"
                if zh
                else (
                    "Review evidence rule: obey each EVIDENCE_ID's grade and locator. "
                    "A/B may support core claims; C supports effect/conclusion only with "
                    "explicit uncertainty; D supports background or attribution only. "
                    f"Available grades: {', '.join(grades)}. Cross-study effect comparison "
                    "is allowed only within one comparability_key."
                )
            )
        if located_fulltext:
            return (
                "综述证据规则：数字、因果、比较、效果和结论性论断只能使用上方标注为 "
                "fulltext evidence 的原文证据，并必须在同一句 cite_keys 中引用对应文献；"
                "摘要只用于背景。"
                if zh
                else (
                    "Review evidence rule: numeric, causal, comparative, effect, and "
                    "conclusion claims may only use located fulltext evidence shown above "
                    "and must cite its key in the same sentence; abstracts support "
                    "background only."
                )
            )
        return (
            "综述证据规则：本节没有可定位全文证据；只写背景，不得写数字、因果、比较、"
            "效果或结论性论断。"
            if zh
            else (
                "Review evidence rule: no located fulltext evidence is available; write "
                "background only, with no numeric, causal, comparative, effect, or "
                "conclusion claims."
            )
        )
    if not assets:
        if grounding == "user_asset" or section.get("kind") == "body":
            return (
                "素材：无。本节涉及实验结果处**不要写任何数字**，"
                "改写「结果待实验补充」，系统会渲染 \\todo{待补充实验数据} 占位。"
                if zh
                else "Assets: none. Do not write ANY experimental number in this section; "
                "state that results are pending — the system renders a TODO placeholder."
            )
        return "Assets: none."

    header = (
        "可用素材（正文里只允许出现这些数字）："
        if zh
        else "Assets (the ONLY numbers you may write):"
    )
    lines = [header]
    for asset in assets[:6]:
        name = asset.get("filename") or asset.get("type") or "asset"
        source_ref = str(asset.get("_asset_ref") or "")
        prefix = f"SOURCE_REF={source_ref} " if source_ref else ""
        if asset.get("type") == "table":
            cells = asset.get("numeric_cells") or {}
            preview = "; ".join(f"{k}={v}" for k, v in list(cells.items())[:12])
            lines.append(f"- {prefix}表 {name}: {preview}")
            lines.append(
                "  （表格本身由系统按 booktabs 渲染，你只写分析文字与 caption，不要复述整张表）"
                if zh
                else "  (the table itself is rendered by the system; write analysis, not the cells)"
            )
        elif asset.get("type") == "note":
            lines.append(f"- {prefix}笔记 {name}: {str(asset.get('text') or '')[:1200]}")
        elif asset.get("type") == "code":
            lines.append(f"- {prefix}代码 {name}: {str(asset.get('text') or '')[:1200]}")
        elif asset.get("type") == "figure":
            lines.append(
                f"- {prefix}图 {name}（由系统 \\includegraphics 插入，你只写 caption 与分析）"
            )
    return "\n".join(lines)


def _source_assets_for_section(
    assets: list[dict[str, Any]],
    *,
    section: dict[str, Any],
    paper_type: str,
) -> list[dict[str, Any]]:
    if paper_type != "original" or section.get("grounding") == "library":
        return []
    key = str(section.get("key") or "")
    kinds = {
        "s2": {"method_note", "code"},
        "s3": {"method_note", "code", "dataset", "result_table"},
        "s4": {"dataset", "result_table"},
        "s5": {"method_note", "code", "dataset", "result_table"},
    }.get(key)
    return [
        item
        for item in assets
        if item.get("_asset_ref") and (kinds is None or item.get("_asset_kind") in kinds)
    ]


def deterministic_asset_paragraphs(
    section: dict[str, Any],
    assets: list[dict[str, Any]],
    *,
    language: str,
) -> list[dict[str, Any]]:
    """Conservative fallback that only restates exact, bound asset content."""
    sentences: list[dict[str, Any]] = []
    for asset in assets:
        source_ref = str(asset.get("_asset_ref") or "")
        if not source_ref:
            continue
        if asset.get("type") in {"note", "code"}:
            raw = " ".join(str(asset.get("text") or "").split())
            text = next(
                (
                    part.strip()
                    for part in re.split(r"(?<=[.!?。！？])\s*", raw)
                    if len(part.strip()) >= 12
                ),
                "",
            )
            if text:
                sentences.append(
                    {"text": text, "cite_keys": [], "evidence_ids": [], "source_refs": [source_ref]}
                )
        elif asset.get("type") == "table":
            cells = list((asset.get("numeric_cells") or {}).items())
            if cells:
                cell, value = cells[0]
                text = (
                    f"结果素材“{asset.get('filename') or source_ref}”记录了 {cell}={value}。"
                    if language == "zh"
                    else (
                        f"The result asset {asset.get('filename') or source_ref} "
                        f"records {cell}={value}."
                    )
                )
                sentences.append(
                    {"text": text, "cite_keys": [], "evidence_ids": [], "source_refs": [source_ref]}
                )
        if len(sentences) >= MAX_PARAGRAPHS_PER_SECTION:
            break
    return [
        {
            "text": sentence["text"],
            "cite_keys": [],
            "sentences": [sentence],
            "stance_summary": "partial",
        }
        for sentence in sentences
    ]


def enforce_sentence_grounding_rules(
    paragraphs: list[dict[str, Any]],
    *,
    assets_by_ref: dict[str, dict[str, Any]],
    require_grounding: bool,
    require_numeric: bool = False,
) -> list[dict[str, Any]]:
    """Strip original-paper claims whose bound asset cannot support their numbers."""
    from ingest.assets import normalize_number

    from paperforge_worker.pipelines.quality import (
        asset_numeric_support_score,
        asset_text_support_score,
        classify_claim,
    )

    for paragraph in paragraphs:
        kept: list[dict[str, Any]] = []
        for sentence in paragraph.get("sentences") or []:
            refs = [ref for ref in sentence.get("source_refs") or [] if ref in assets_by_ref]
            claim_kind = classify_claim(str(sentence.get("text") or ""))
            must_bind = require_grounding or claim_kind not in {"background", "attribution"}
            values = {
                normalize_number(str(value))
                for ref in refs
                for value in (assets_by_ref[ref].get("numbers") or [])
            }
            numbers = {
                normalize_number(value)
                for value in extract_numbers(str(sentence.get("text") or ""))
            }
            prose_sources = [
                str(
                    assets_by_ref[ref].get("text")
                    or assets_by_ref[ref].get("_asset_description")
                    or ""
                )
                for ref in refs
                if assets_by_ref[ref].get("type") in {"note", "code"}
            ]
            lexical_support = max(
                (
                    asset_text_support_score(str(sentence.get("text") or ""), source)
                    for source in prose_sources
                ),
                default=0.0,
            )
            numeric_support = asset_numeric_support_score(
                str(sentence.get("text") or ""),
                numbers,
                [assets_by_ref[ref] for ref in refs],
            )
            content_missing = not numbers and (not prose_sources or lexical_support < 0.15)
            if (
                (must_bind and not refs)
                or (require_numeric and not numbers)
                or (must_bind and content_missing)
                or not numbers.issubset(values)
                or (bool(numbers) and numeric_support < 0.1)
            ):
                sentence["downgraded_reason"] = (
                    "asset_grounding_missing"
                    if not refs
                    else "asset_result_value_missing"
                    if require_numeric and not numbers
                    else "asset_content_mismatch"
                    if content_missing
                    else "asset_numeric_context_mismatch"
                    if numbers.issubset(values) and numeric_support < 0.1
                    else "asset_number_mismatch"
                )
                continue
            sentence["source_refs"] = refs
            kept.append(sentence)
        if paragraph.get("sentences") is not None:
            paragraph["sentences"] = kept
            paragraph["text"] = " ".join(str(item.get("text") or "") for item in kept).strip()
    return [paragraph for paragraph in paragraphs if str(paragraph.get("text") or "").strip()]


def normalize_paragraphs(
    raw: Any,
    *,
    allowed: set[str],
    allowed_evidence_ids: set[str] | None = None,
    allowed_source_refs: set[str] | None = None,
) -> list[dict[str, Any]]:
    """净化段落结构；cite_keys 再次按白名单过滤（纵深防御）。"""
    if not isinstance(raw, list):
        return []
    paragraphs: list[dict[str, Any]] = []
    for item in raw[:MAX_PARAGRAPHS_PER_SECTION]:
        keys: list[Any]
        if isinstance(item, str):
            text, keys = item, []
        elif isinstance(item, dict):
            sentence_rows = _normalize_sentences(
                item.get("sentences"),
                allowed=allowed,
                allowed_evidence_ids=allowed_evidence_ids or set(),
                allowed_source_refs=allowed_source_refs or set(),
            )
            if sentence_rows:
                paragraphs.append(
                    {
                        "text": " ".join(sentence["text"] for sentence in sentence_rows),
                        "cite_keys": list(
                            dict.fromkeys(
                                key for sentence in sentence_rows for key in sentence["cite_keys"]
                            )
                        ),
                        "sentences": sentence_rows,
                        "stance_summary": _normalize_stance(item.get("stance_summary")),
                    }
                )
                continue
            text = item.get("text") or item.get("content") or ""
            keys = item.get("cite_keys") or item.get("citations") or []
        else:
            continue
        text = _clean_paragraph(text)
        if not text:
            continue
        cite_keys: list[str] = [
            key
            for key in dict.fromkeys(str(k).strip() for k in keys if str(k).strip())
            if key in allowed
        ]
        paragraphs.append({"text": text, "cite_keys": cite_keys})
    return paragraphs


def _normalize_sentences(
    raw: Any,
    *,
    allowed: set[str],
    allowed_evidence_ids: set[str],
    allowed_source_refs: set[str],
) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    sentences: list[dict[str, Any]] = []
    for item in raw[:24]:
        if not isinstance(item, dict):
            continue
        text = _clean_paragraph(item.get("text") or item.get("content") or "")
        if not text:
            continue
        keys = item.get("cite_keys") or item.get("citations") or []
        cite_keys = [
            key
            for key in dict.fromkeys(str(value).strip() for value in keys if str(value).strip())
            if key in allowed
        ]
        evidence_ids = [
            evidence_id
            for evidence_id in dict.fromkeys(
                str(value).strip() for value in item.get("evidence_ids") or [] if str(value).strip()
            )
            if evidence_id in allowed_evidence_ids
        ]
        source_refs = [
            source_ref
            for source_ref in dict.fromkeys(
                str(value).strip() for value in item.get("source_refs") or [] if str(value).strip()
            )
            if source_ref in allowed_source_refs
        ]
        sentences.append(
            {
                "text": text,
                "cite_keys": cite_keys,
                "evidence_ids": evidence_ids,
                "source_refs": source_refs,
            }
        )
    return sentences


def _normalize_stance(value: Any) -> str | None:
    normalized = str(value or "").strip().casefold()
    allowed = {
        "consistent",
        "conditional",
        "conflicting",
        "insufficient",
        "partial",
        "background",
    }
    return normalized if normalized in allowed else None


def enforce_sentence_evidence_rules(
    paragraphs: list[dict[str, Any]],
    *,
    evidence_by_id: dict[str, dict[str, Any]],
    language: str,
) -> list[dict[str, Any]]:
    """写作后处理的 R4/R5/R6：违规句降级改写，不让摘要冒充全文。"""
    from paperforge_worker.pipelines.quality import classify_claim

    for paragraph in paragraphs:
        for sentence in paragraph.get("sentences") or []:
            claim_kind = classify_claim(str(sentence.get("text") or ""))
            if claim_kind in {"background", "attribution"}:
                continue
            units = [
                evidence_by_id[evidence_id]
                for evidence_id in sentence.get("evidence_ids") or []
                if evidence_id in evidence_by_id
            ]
            grade_ok = _sentence_grade_ok(
                claim_kind,
                str(sentence.get("text") or ""),
                units,
            )
            comparable = claim_kind != "comparison" or _units_comparable(units)
            located = claim_kind != "numeric" or any(
                unit.get("page") or unit.get("object_ref") for unit in units
            )
            if units and grade_ok and comparable and located:
                continue
            if claim_kind == "comparison" and units and not comparable:
                sentence["text"] = (
                    "这些证据采用不同的任务、数据集、指标或划分，结果应分别陈述。"
                    if language == "zh"
                    else (
                        "These evidence units use different tasks, datasets, metrics, "
                        "or splits; their results must be reported separately."
                    )
                )
                sentence["downgraded_reason"] = "R5_not_comparable"
            elif units and all(unit.get("grade") == "D_abstract_only" for unit in units):
                original = str(sentence.get("text") or "")
                sentence["text"] = (
                    f"摘要层面的作者表述是：{original}"
                    if language == "zh"
                    else (f"The cited work reports in its abstract that {original.rstrip('.')}.")
                )
                sentence["downgraded_reason"] = "R4_abstract_attribution"
            else:
                # Do not spray an internal quality warning into the prose. The
                # discarded sentence and reason remain in claim/evidence audit
                # records and the evidence appendix.
                sentence["text"] = ""
                sentence["cite_keys"] = []
                sentence["evidence_ids"] = []
                sentence["downgraded_reason"] = (
                    "R6_locator_missing" if not located else "R4_grade_missing"
                )
        # R18 / N0-6: physically drop blanked sentences so IR never keeps empty
        # runs or dangling connective adverbs without antecedents.  Keep the
        # audit trail on the paragraph for quality/claim review.
        if paragraph.get("sentences"):
            kept: list[dict[str, Any]] = []
            downgraded: list[dict[str, Any]] = []
            for sentence in paragraph["sentences"]:
                if str(sentence.get("text") or "").strip():
                    kept.append(sentence)
                else:
                    downgraded.append(sentence)
            paragraph["sentences"] = kept
            if downgraded:
                paragraph["downgraded_sentences"] = (
                    list(paragraph.get("downgraded_sentences") or []) + downgraded
                )
            paragraph["text"] = " ".join(
                str(sentence.get("text") or "").strip() for sentence in kept
            )
    return [
        paragraph
        for paragraph in paragraphs
        if str(paragraph.get("text") or "").strip()
        or any(str(s.get("text") or "").strip() for s in paragraph.get("sentences") or [])
        or paragraph.get("downgraded_sentences")
    ]


def _sentence_grade_ok(
    claim_kind: str,
    text: str,
    units: list[dict[str, Any]],
) -> bool:
    grades = {str(unit.get("grade") or "") for unit in units}
    if grades & {"A_located_structured", "B_located_prose"}:
        return True
    uncertain = bool(
        re.search(
            r"(?:可能|或许|尚不确定|may|might|could|suggests?|uncertain)",
            text,
            re.IGNORECASE,
        )
    )
    return claim_kind in {"effect", "conclusion"} and "C_fulltext_unlocated" in grades and uncertain


def _units_comparable(units: list[dict[str, Any]]) -> bool:
    if len({str(unit.get("work_id")) for unit in units if unit.get("work_id")}) < 2:
        return False
    key_sets = [
        {
            str(measurement.get("comparability_key"))
            for measurement in unit.get("measurements") or []
            if measurement.get("comparability_key")
        }
        for unit in units
    ]
    return bool(key_sets) and all(key_sets) and bool(set.intersection(*key_sets))


def normalize_terms(raw: Any) -> dict[str, str]:
    terms: dict[str, str] = {}
    if not isinstance(raw, list):
        return terms
    for item in raw[:30]:
        if isinstance(item, dict):
            term = str(item.get("term") or "").strip()
            translation = str(item.get("translation") or item.get("gloss") or "").strip()
            if term:
                terms[term] = translation
    return terms


def deterministic_paragraphs(
    section: dict[str, Any],
    cards: dict[str, dict[str, Any]],
    allowed: set[str],
    *,
    evidence: list[dict[str, Any]] | None = None,
    language: str = "en",
) -> list[dict[str, Any]]:
    """确定性回退：用卡片摘要拼出可读的占位正文，绝不产生卡片之外的断言。"""
    paragraphs: list[dict[str, Any]] = []
    summary = str(section.get("summary") or "").strip()
    if summary:
        paragraphs.append({"text": summary, "cite_keys": []})
    if evidence:
        for item in evidence[: MAX_PARAGRAPHS_PER_SECTION - len(paragraphs)]:
            text = str(item.get("text") or "").strip()
            cite_key = str(item.get("cite_key") or "")
            evidence_id = str(item.get("evidence_id") or "")
            if not text or cite_key not in allowed or not evidence_id:
                continue
            if item.get("grade") == "D_abstract_only":
                text = (
                    f"相关文献仅在摘要中报告：{text.rstrip('。')}。"
                    if language == "zh"
                    else f"The cited work reports in its abstract that {text.rstrip('.')}."
                )
            sentence = {
                "text": text,
                "cite_keys": [cite_key],
                "evidence_ids": [evidence_id],
            }
            paragraphs.append(
                {
                    "text": text,
                    "cite_keys": [cite_key],
                    "sentences": [sentence],
                    "stance_summary": section.get("stance_summary") or "partial",
                }
            )
        return paragraphs[:MAX_PARAGRAPHS_PER_SECTION]
    # Never turn an empty evidence contract into an alphabetical dump of every
    # library card.  That looks like a review while silently admitting papers
    # outside the section's scope.  Keep the gap visible for the author instead.
    title = str(section.get("title") or "").strip()
    gap = (
        f"本节“{title}”尚无满足定位与可比性要求的证据；待补充可核验来源后再展开论述。"
        if language == "zh"
        else (
            f"This section ({title}) has no evidence meeting the required provenance "
            "and comparability criteria; add verifiable sources before drafting the discussion."
        )
    )
    paragraphs.append({"text": gap, "cite_keys": [], "evidence_gap": True})
    return paragraphs[:MAX_PARAGRAPHS_PER_SECTION]


def summarize_paragraphs(paragraphs: list[dict[str, Any]]) -> str:
    """滚动摘要：取每段首句，控制在预算内。"""
    sentences: list[str] = []
    for paragraph in paragraphs:
        text = str(paragraph.get("text") or "").strip()
        if not text:
            continue
        first = re.split(r"(?<=[.!?。！？])\s*", text)[0]
        sentences.append(first.strip())
    return " ".join(sentences)[:MAX_ROLLING_SUMMARY_CHARS]


_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?%?")
_INLINE_AUTHOR_YEAR_RE = re.compile(
    r"\((?:[A-Z][A-Za-z\-]+(?:\s+et\s+al\.)?,?\s*\d{4}[a-z]?;?\s*)+\)"
)


def extract_numbers(text: str) -> set[str]:
    """抽出正文中的数值 token（数字红线的比对基准）。"""
    return set(_NUMBER_RE.findall(text or ""))


def count_words(text: str) -> int:
    """中文按字计、西文按词计——中文综述的字数目标才有意义。"""
    if not text:
        return 0
    cjk = len(re.findall(r"[一-鿿]", text))
    latin = len(re.findall(r"[A-Za-z][A-Za-z'-]*", text))
    return cjk + latin


def _clean_paragraph(text: Any) -> str:
    if not isinstance(text, str):
        return ""
    # 模型偶尔仍会写 [1] / (Smith, 2020)：引用由 cite_keys 渲染，正文里一律清掉。
    cleaned = re.sub(r"\[(?:\d+\s*[,;]?\s*)+\]", "", text)
    cleaned = _INLINE_AUTHOR_YEAR_RE.sub("", cleaned)
    return " ".join(cleaned.split())


def _format_glossary(glossary: dict[str, str]) -> str:
    if not glossary:
        return "(empty)"
    return "; ".join(f"{term}={translation or term}" for term, translation in glossary.items())


def _paragraphs_json(paragraphs: list[dict[str, Any]]) -> str:
    import json

    return json.dumps({"paragraphs": paragraphs}, ensure_ascii=False)
