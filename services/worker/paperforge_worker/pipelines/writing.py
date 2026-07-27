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
from paper_ir import CiteRun, ParagraphBlock, Section, TableBlock, TableSource, TextRun

MAX_PARAGRAPHS_PER_SECTION = 8
MAX_ROLLING_SUMMARY_CHARS = 600
TARGET_WORDS_PER_SECTION_ZH = 1200
TARGET_WORDS_PER_SECTION_EN = 800

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
    {{"sentences": [
      {{"text": "一个完整论断句（不要写引用标记）", "cite_keys": ["只支撑本句的引用键"]}}
    ]}}
  ],
  "terms": [{{"term": "术语", "translation": "译名/缩写"}}]
}}
要求：
- 按主题论证展开，不要逐篇复述文献；每段 3-6 句，观点先行、证据跟随；
- 每个句子的 cite_keys 只能列真正支撑该句的可用引用键；禁止发明或在段末堆整段引用；
- 正文里不要写 [1]、(Smith 2020) 之类的标记——引用由系统按 cite_keys 渲染；
- 沿用给定术语表中的译名与缩写；新术语登记到 terms。
- {_NO_FABRICATION_ZH}"""

_SYSTEM_PROMPT_EN = f"""You write sections of an academic review. Follow the outline and cards.
Output JSON only:
{{
  "paragraphs": [
    {{"sentences": [
      {{"text": "one complete claim sentence (no inline markers)",
       "cite_keys": ["keys supporting only this sentence"]}}
    ]}}
  ],
  "terms": [{{"term": "term", "translation": "abbreviation or gloss"}}]
}}
Rules:
- argue by theme, never paper-by-paper; 3-6 sentences per paragraph, claim first, evidence after;
- each sentence's cite_keys MUST support that exact sentence and come from the provided list;
  never invent or pile paragraph-wide citations at the end; use [] when unsupported;
- do not write inline markers like [1] or (Smith 2020) — the system renders citations;
- reuse the given glossary terms consistently; register new terms in `terms`.
- {_NO_FABRICATION_EN}"""

_COHERENCE_PROMPT_ZH = """你是学术论文的连贯性编辑。给定相邻章节的正文，改写目标章节，使其：
过渡自然、术语一致、不与其他章节重复论述。只输出 JSON，结构与输入相同：
{"paragraphs": [{"sentences": [{"text": "...", "cite_keys": [...]}]}]}
不得新增引用键，不得改动或新增任何数字。"""

_COHERENCE_PROMPT_EN = """You are a coherence editor. Rewrite the target section so that it
transitions smoothly, uses consistent terminology, and does not repeat neighbouring sections.
Output JSON with the same sentence-level shape:
{"paragraphs": [{"sentences": [{"text": "...", "cite_keys": [...]}]}]}
Never add cite keys and never add or change any number."""


@dataclass
class SectionDraft:
    section_key: str
    title: str
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
            sentence_rows = paragraph.get("sentences") or []
            runs: list[Any] = []
            if sentence_rows:
                for index, sentence in enumerate(sentence_rows):
                    runs.append(TextRun(v=str(sentence.get("text") or "")))
                    keys = [key for key in sentence.get("cite_keys", []) if key]
                    if keys:
                        runs.append(CiteRun(keys=list(dict.fromkeys(keys))))
                    if index < len(sentence_rows) - 1:
                        runs.append(TextRun(v=" "))
            else:
                runs.append(TextRun(v=paragraph.get("text", "")))
                keys = [key for key in paragraph.get("cite_keys", []) if key]
                if keys:
                    runs.append(CiteRun(keys=list(dict.fromkeys(keys))))
            blocks.append(ParagraphBlock(runs=runs))
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
        section = Section(key=self.section_key, level=level, title=self.title, blocks=blocks)
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

    draft = SectionDraft(
        section_key=section_key,
        title=title,
        inline_tables=[
            dict(table) for table in section.get("inline_tables") or [] if isinstance(table, dict)
        ],
    )
    if section.get("deterministic_text"):
        draft.paragraphs = [{"text": str(section["deterministic_text"]), "cite_keys": []}]
        draft.generator = "deterministic_search_log"
        return draft
    if runner is None or not runner.enabled:
        draft.paragraphs = deterministic_paragraphs(section, cards, allowed)
        draft.generator = "deterministic"
        return draft

    user_prompt = _build_prompt(
        section=section,
        cards=cards,
        allowed=allowed,
        context=context,
        assets=assets or [],
    )
    system_prompt = _SYSTEM_PROMPT_ZH if context.language == "zh" else _SYSTEM_PROMPT_EN

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
        draft.paragraphs = deterministic_paragraphs(section, cards, allowed)
        draft.generator = "deterministic_fallback"
        return draft

    draft.paragraphs = normalize_paragraphs(result.value.get("paragraphs"), allowed=allowed)
    draft.terms = normalize_terms(result.value.get("terms"))
    draft.model = result.model
    draft.generator = f"llm:{result.model}"
    if not draft.paragraphs:
        draft.paragraphs = deterministic_paragraphs(section, cards, allowed)
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
    rewritten = normalize_paragraphs(result.value.get("paragraphs"), allowed=allowed)
    if not rewritten:
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
    card_lines = []
    for key in sorted(allowed):
        card = cards.get(key) or {}
        parts = [f"[{key}] {card.get('title', '')} ({card.get('year') or 'n.d.'})"]
        if card.get("summary"):
            parts.append(f"  summary: {str(card['summary'])[:300]}")
        for field_name in ("contributions", "methods", "results", "limitations"):
            values = card.get(field_name) or []
            if values:
                parts.append(f"  {field_name}: {'; '.join(str(v) for v in values[:3])}")
        if card.get("fulltext_used"):
            points = card.get("quotable_points") or []
            located = [point for point in points if isinstance(point, dict) and point.get("text")]
            for point in located[:3]:
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
        card_lines.append("\n".join(parts))

    points = section.get("argument_points") or []
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
            "",
            "Literature cards:",
            "\n\n".join(card_lines) or "(no cards assigned to this section)",
            "",
            _asset_block(
                assets or [],
                language=context.language,
                section=section,
                paper_type=context.paper_type,
                cards=[cards.get(key) or {} for key in sorted(allowed)],
            ),
        ]
    )


def _asset_block(
    assets: list[dict[str, Any]],
    *,
    language: str,
    section: dict[str, Any],
    paper_type: str,
    cards: list[dict[str, Any]],
) -> str:
    """素材接地块。

    数字红线（设计 §4.4.2）：正文数字必须来自 parsed_json 的确定性注入。
    这里把可用数值原样列给模型作为**唯一允许写出的数字**；一个素材都没有时，
    明确要求写占位符而不是编数字——表格与图另由渲染器确定性展开，模型只写 caption。
    """
    zh = language == "zh"
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
        if asset.get("type") == "table":
            cells = asset.get("numeric_cells") or {}
            preview = "; ".join(f"{k}={v}" for k, v in list(cells.items())[:12])
            lines.append(f"- 表 {name}: {preview}")
            lines.append(
                "  （表格本身由系统按 booktabs 渲染，你只写分析文字与 caption，不要复述整张表）"
                if zh
                else "  (the table itself is rendered by the system; write analysis, not the cells)"
            )
        elif asset.get("type") == "note":
            lines.append(f"- 笔记 {name}: {str(asset.get('text') or '')[:400]}")
        elif asset.get("type") == "figure":
            lines.append(f"- 图 {name}（由系统 \\includegraphics 插入，你只写 caption 与分析）")
    return "\n".join(lines)


def normalize_paragraphs(raw: Any, *, allowed: set[str]) -> list[dict[str, Any]]:
    """净化段落结构；cite_keys 再次按白名单过滤（纵深防御）。"""
    if not isinstance(raw, list):
        return []
    paragraphs: list[dict[str, Any]] = []
    for item in raw[:MAX_PARAGRAPHS_PER_SECTION]:
        if isinstance(item, str):
            text, keys = item, []
        elif isinstance(item, dict):
            sentence_rows = _normalize_sentences(item.get("sentences"), allowed=allowed)
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
        cite_keys = [
            key
            for key in dict.fromkeys(str(k).strip() for k in keys if str(k).strip())
            if key in allowed
        ]
        paragraphs.append({"text": text, "cite_keys": cite_keys})
    return paragraphs


def _normalize_sentences(raw: Any, *, allowed: set[str]) -> list[dict[str, Any]]:
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
        sentences.append({"text": text, "cite_keys": cite_keys})
    return sentences


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
) -> list[dict[str, Any]]:
    """确定性回退：用卡片摘要拼出可读的占位正文，绝不产生卡片之外的断言。"""
    paragraphs: list[dict[str, Any]] = []
    summary = str(section.get("summary") or "").strip()
    if summary:
        paragraphs.append({"text": summary, "cite_keys": []})
    for key in sorted(allowed):
        card = cards.get(key) or {}
        text = str(card.get("summary") or card.get("title") or "").strip()
        if text:
            paragraphs.append({"text": text, "cite_keys": [key]})
    if not paragraphs:
        paragraphs.append(
            {
                "text": str(section.get("title") or ""),
                "cite_keys": [],
            }
        )
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
