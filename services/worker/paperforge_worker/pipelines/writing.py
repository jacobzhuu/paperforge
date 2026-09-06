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

from paperforge_worker.comparability import comparison_admissible
from paperforge_worker.locators import (
    evidence_locator as _evidence_locator,
)
from paperforge_worker.locators import (
    is_located,
    locator_display,
)

MAX_PARAGRAPHS_PER_SECTION = 8
MAX_ROLLING_SUMMARY_CHARS = 600
# 一节正文（目标 ~1400 字）外加逐句回抄的 evidence_ids，实测要 4000–8000 输出 token，
# 而推理型模型的思维链和正文抢的是同一份 max_tokens。8000 这个数原本是被
# `clamp_max_output_tokens()` 对 deepseek 系的 8192 上限逼出来的：加倍重试只能涨到
# 8192（+2.4%），等于没有第二次机会，所以只能一次要满。GLM-5.3 系的上限是 128K，
# 这个约束没有了——16000 让正文和推理各有余量，而且加倍重试（32000）也仍在上限内，
# 于是截断真的有一次补救，紧凑档退居第三道防线。
# max_tokens 是上限不是计费量（按实际生成计费），抬高它不产生成本。
SECTION_MAX_OUTPUT_TOKENS = 16000
# 一篇中文综述正文 8 节 × 1200 字的设计上限约 1 万字，实测交付 5,250 字——投稿级
# 中文综述通常要 8000-15000 字。上调目标，但保持
# `目标 × MIN_TARGET_RATIO == COMPACT_TARGET_WORDS_*`：紧凑档是截断后的退路，
# 它的目标一旦低于验收线，被截断的那一节就会陷进永远过不了的重试。
TARGET_WORDS_PER_SECTION_ZH = 1400
TARGET_WORDS_PER_SECTION_EN = 900
# 紧凑档砍掉目标长度与段落数，让同一节的输出结构性地装得进同一个上限。它是**加预算
# 重试之后**的退路：在 deepseek 上供给侧顶在 8192、重试无效，降低需求是唯一方向；在
# GLM 上 runner 会先把预算加倍再试一次，这一档只在那次也截断时才生效。
COMPACT_TARGET_WORDS_ZH = 700
COMPACT_TARGET_WORDS_EN = 450
COMPACT_MAX_PARAGRAPHS = 4
# 一节正文低于这个字数就不算写出来了——模型偶尔会返回一两句话就收尾。
MIN_BODY_WORDS_ZH = 180
MIN_BODY_WORDS_EN = 120
# 框架章节（摘要/引言/结论）本来就短，用更低的下限，否则会把正常摘要判成失败。
#: Abstract-only evidence gets rewritten into an explicit attribution rather
#: than dropped — but past a couple of them a section stops reading as a review
#: and starts reading as a log of what could not be verified.  One measured
#: section had every substantive sentence in this form.  Beyond the cap the
#: sentence follows the normal R4 path: out of the prose, into the audit trail.
MAX_ABSTRACT_ATTRIBUTIONS_PER_SECTION = 2
MIN_FRAME_WORDS_ZH = 80
MIN_FRAME_WORDS_EN = 60
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
- 按主题论证展开，不要逐篇复述文献；观点先行、证据跟随；
- **每条论证要点写成一个独立段落**，顺序与给出的要点一致；每段 4-7 句，
  每句都要是完整论述（交代机制、条件或数值），不要用一句话带过一条要点；
- 每个句子的 cite_keys 只能列真正支撑该句的可用引用键；禁止发明或在段末堆整段引用；
- 非背景句必须填写 evidence_ids；只能使用给定 EVIDENCE_ID，数字句必须绑定页码/表格/公式证据；
- 段落按「论断→一致证据→条件差异→适用边界」展开；
- 证据缺口不是每段的固定环节：只在确实影响本节结论时写，且全节最多一句，
  用作者口吻写（「现有研究尚未在统一基准上比较这些方法」），不要复述系统的
  证据评级或检索过程（不写「现有证据未提供」「仅有摘要级证据」这类话）；
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
- argue by theme, never paper-by-paper; claim first, evidence after;
- **write one paragraph per argument point**, in the order given; 4-7 sentences each,
  every sentence carrying a real step of the argument (mechanism, condition, or figure)
  rather than disposing of a point in a single line;
- each sentence's cite_keys MUST support that exact sentence and come from the provided list;
  never invent or pile paragraph-wide citations at the end; use [] when unsupported;
- every non-background sentence must declare supplied evidence_ids; numeric claims require a
  page-, table-, or equation-located evidence unit;
- structure paragraphs as claim → agreement → conditional difference → boundary;
- an evidence gap is not a required slot in every paragraph: mention one only where it
  actually bears on this section's conclusion, at most once per section, and in the
  author's voice ("no study has yet compared these methods on a shared benchmark").
  Never narrate the retrieval or grading process itself;
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
    #: 1 = \section，2 = \subsection。渲染器（latex_render）早就支持 1-3，
    #: 但在引入二级标题之前没有任何调用方传过 1 以外的值。
    level: int = 1
    parent_key: str | None = None
    paragraphs: list[dict[str, Any]] = field(default_factory=list)
    inline_tables: list[dict[str, Any]] = field(default_factory=list)
    citation_warnings: list[dict[str, Any]] = field(default_factory=list)
    terms: dict[str, str] = field(default_factory=dict)
    model: str | None = None
    rewrite_count: int = 0
    generator: str = "deterministic"
    #: 为什么这一节没有正文。编排层据此决定下一次用哪种重试策略，而不是
    #: 把所有失败都当成同一件事重试同一遍。
    failure_reason: str | None = None
    #: 这一节实际经历了几次写作调用（含重试），落进 outcome 供成本核对。
    attempts: int = 0

    @property
    def word_count(self) -> int:
        return sum(count_words(p.get("text", "")) for p in self.paragraphs)

    @property
    def has_body(self) -> bool:
        """这一节是不是真的有正文——降级占位不算。"""
        return self.generator.startswith("llm") or self.generator in {
            "resumed",
            "deterministic_search_log",
        }

    def to_ir_section(self, *, level: int | None = None) -> Section:
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
            level=self.level if level is None else level,
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
    compact: bool = False,
    corrections: list[str] | None = None,
) -> SectionDraft:
    """生成单个章节。R2 违规先重写一次，再违规则 strip 并留告警。

    ``compact`` 走紧凑档：目标长度与段落数都压下来，让需求装进供给侧。这是加预算重试
    之后的退路——模型上限够高时 runner 会先加倍预算再试一次（见
    ``llm_runtime.runner._retry_can_help``），上限顶死时它是唯一方向。
    ``corrections`` 是上一版的具体问题（照抄原文、语种不对、太短），
    直接回灌给模型，重试才有理由产生不同的结果。
    """
    section_key = str(section.get("key") or "section")
    title = str(section.get("title") or section_key)
    allowed = {key for key in section.get("cite_keys", []) if key in whitelist}
    section_evidence = section_evidence_for(section, context.outline)
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
        level=int(section.get("level") or 1),
        parent_key=(str(section["parent_key"]) if section.get("parent_key") else None),
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
                language=context.language,
                # 没有引用契约是「证据不够」；runner 关着是「没人写」。
                reason="evidence_gap" if body_without_cites else "write_failed",
            )
        draft.generator = "evidence_gap_skeleton" if body_without_cites else "deterministic"
        draft.failure_reason = "no_evidence_contract" if body_without_cites else "writer_disabled"
        return draft

    user_prompt = _build_prompt(
        section=section,
        cards=cards,
        allowed=allowed,
        context=context,
        assets=assets or [],
        compact=compact,
        corrections=corrections,
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
        max_output_tokens=SECTION_MAX_OUTPUT_TOKENS,
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
            max_output_tokens=SECTION_MAX_OUTPUT_TOKENS,
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
                language=context.language,
                reason="write_failed",
            )
        draft.generator = "deterministic_fallback"
        draft.failure_reason = result.error or "no_model_output"
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
            # 模型答了，但每一句都被证据规则剥掉了——留下的不是正文，同样按未生成处理。
            draft.paragraphs = deterministic_paragraphs(
                section,
                language=context.language,
                reason="write_failed",
            )
        draft.generator = "deterministic_fallback"
        draft.failure_reason = "evidence_rules_stripped_all_sentences"
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
        max_output_tokens=SECTION_MAX_OUTPUT_TOKENS,
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
    compact: bool = False,
    corrections: list[str] | None = None,
) -> str:
    zh = context.language == "zh"
    if compact:
        target = COMPACT_TARGET_WORDS_ZH if zh else COMPACT_TARGET_WORDS_EN
    else:
        # 大纲可以给某一节指定自己的篇幅：摘要不该按正文小节的目标写，引言和结论也
        # 各有各的量。没有指定就沿用正文小节的目标。
        target = int(
            section.get("target_words")
            or (TARGET_WORDS_PER_SECTION_ZH if zh else TARGET_WORDS_PER_SECTION_EN)
        )
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
                # 卡片证据点用的是 `section`/`paragraph` 拼写，显式传字段而不是
                # 让适配器去猜别名。
                locator = (
                    _locator_text(
                        page=point.get("page"),
                        section_path=point.get("section"),
                        paragraph_index=point.get("paragraph"),
                    )
                    or ""
                )
                parts.append(f"  fulltext evidence ({locator or 'unlocated'}): {point['text']}")
        # 不再对所有文献机械 [:3]；按章节总 token 预算和文献数动态分配，
        # 同时保证每篇至少保留题名行。
        rendered = _truncate_card_context(parts, min(per_card_budget, remaining_context))
        card_lines.append(rendered)
        remaining_context = max(0, remaining_context - len(rendered))

    points = section.get("argument_points") or []
    section_evidence = section_evidence_for(section, context.outline)
    evidence_block = _evidence_context_block(
        section=section,
        evidence=section_evidence,
        language=context.language,
    )
    # 纠正指令放在最前面：这是重试与首轮唯一的差别，埋在两千行上下文中间等于没写。
    correction_block = (
        "\n".join(
            [
                (
                    "必须修正上一版的以下问题："
                    if zh
                    else "You MUST fix these problems from your last attempt:"
                ),
                *(f"- {item}" for item in corrections),
                "",
            ]
        )
        if corrections
        else ""
    )
    compact_block = (
        (
            f"输出预算有限：最多写 {COMPACT_MAX_PARAGRAPHS} 段，terms 可以留空数组。"
            "宁可少写一个论点，也要把写下的部分写完整——截断的半句话没有任何价值。"
            if zh
            else (
                f"Output budget is tight: at most {COMPACT_MAX_PARAGRAPHS} paragraphs, and `terms` "
                "may be an empty array. Drop an argument rather than getting cut off mid-sentence."
            )
        )
        if compact
        else ""
    )
    return "\n".join(
        [
            correction_block,
            compact_block,
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
            "只能陈述该证据直接支持的内容，把它写成单一来源的初步发现，"
            "不得推广为一般结论。用作者口吻交代这一点即可，不要描述本文的检索过程。"
        )
    return (
        f"Evidence limitation (mandatory): only {count} independent source(s) support this "
        "section. State only what that evidence directly supports, present it as a "
        "single-source preliminary finding, and do not generalise. Say so in the author's "
        "voice; never describe this review's own retrieval process."
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


def section_evidence_for(
    section: dict[str, Any],
    outline: dict[str, Any],
) -> list[dict[str, Any]]:
    bundles = [
        item for item in outline.get("sub_question_bundles") or [] if isinstance(item, dict)
    ]
    question_id = str(section.get("question_id") or "")
    if question_id:
        bundle: dict[str, Any] = next(
            (item for item in bundles if str(item.get("question_id") or "") == question_id),
            {},
        )
        return [item for item in bundle.get("evidence") or [] if isinstance(item, dict)]
    # Cross-study sections carry their own `evidence_ids` instead of a question.
    # Returning [] for them meant the writer built an empty evidence ledger, so
    # `_normalize_sentences` dropped every binding the model produced and
    # `enforce_sentence_evidence_rules` blanked everything that was not
    # background prose.  The section that makes a review's sharpest comparative
    # claims shipped with naked cite keys and `evidence_ids: []`.
    wanted = {str(value) for value in section.get("evidence_ids") or [] if value}
    if not wanted:
        return []
    seen: set[str] = set()
    evidence: list[dict[str, Any]] = []
    for item in bundles:
        for unit in item.get("evidence") or []:
            if not isinstance(unit, dict):
                continue
            unit_id = str(unit.get("evidence_id") or unit.get("id") or "")
            if unit_id in wanted and unit_id not in seen:
                seen.add(unit_id)
                evidence.append(unit)
    return evidence


def _locator_text(**fields: object) -> str | None:
    """按显式字段名求定位串（卡片证据点的键名与证据单元不同）。"""
    locator = _evidence_locator(**fields)  # type: ignore[arg-type]
    return locator.display if locator else None


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
    lines.extend(_synthesis_block(section.get("synthesis")))
    grade_order = {
        "A_located_structured": 0,
        "B_located_prose": 1,
        "C_fulltext_unlocated": 2,
        "D_abstract_only": 3,
    }
    used = len("\n".join(lines))
    for item in dedupe_evidence_by_text(
        sorted(evidence, key=lambda row: grade_order.get(str(row.get("grade")), 9))
    ):
        grade = str(item.get("grade") or "")
        locator = locator_display(item) or "unlocated"
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


def dedupe_evidence_by_text(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """同一段文字只让写作器看一遍。

    同一篇文献被切出的证据条目会有完全相同的正文（实测项目 6a6bbf18：s3/s5/s6 每节的
    证据池里各有 3 条是重复文本，s1/s2 各 2 条）。重复条目既白占上下文预算，又让
    「这一节有多少证据没用上」这个统计虚高。保留先出现的那条——上游已按证据等级排过序，
    先出现的等级不低于后面的。
    """
    seen: set[str] = set()
    kept: list[dict[str, Any]] = []
    for item in evidence:
        fingerprint = "".join(str(item.get("text") or "").split())
        if fingerprint and fingerprint in seen:
            continue
        if fingerprint:
            seen.add(fingerprint)
        kept.append(item)
    return kept


def _synthesis_block(synthesis: Any) -> list[str]:
    """SYNTH 的叙述性结论（Phase 4）。

    这是**怎么论证**的指引，不是可以少写绑定的许可：句子级 evidence_ids 仍然强制，
    `enforce_sentence_evidence_rules` 照跑。关闭该特性时 section 里没有这个键，
    这里返回空列表，提示词逐字节不变。
    """
    if not isinstance(synthesis, dict):
        return []
    lines: list[str] = []
    if synthesis.get("claim"):
        lines.append(f"[SYNTHESIS claim] {synthesis['claim']}")
    for entry in synthesis.get("agreement") or []:
        lines.append(f"[SYNTHESIS agreement] {_synthesis_entry(entry)}")
    for entry in synthesis.get("conditional") or []:
        dimension = str(entry.get("dimension") or "unspecified")
        lines.append(f"[SYNTHESIS conditional dimension={dimension}] {_synthesis_entry(entry)}")
    for entry in synthesis.get("conflict") or []:
        key = str(entry.get("comparability_key") or "")
        lines.append(f"[SYNTHESIS conflict key={key}] {_synthesis_entry(entry)}")
    for entry in synthesis.get("gap") or []:
        lines.append(f"[SYNTHESIS gap] {_synthesis_entry(entry)}")
    return lines


def _synthesis_entry(entry: Any) -> str:
    if not isinstance(entry, dict):
        return ""
    statement = str(entry.get("statement") or "")
    ids = [str(value) for value in entry.get("evidence_ids") or []]
    if not ids:
        return statement
    return f"{statement} (EVIDENCE_ID={', '.join(ids)})"


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
        dropped: list[dict[str, Any]] = []
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
                record_downgrade(
                    sentence,
                    rule=(
                        "asset_grounding_missing"
                        if not refs
                        else "asset_result_value_missing"
                        if require_numeric and not numbers
                        else "asset_content_mismatch"
                        if content_missing
                        else "asset_numeric_context_mismatch"
                        if numbers.issubset(values) and numeric_support < 0.1
                        else "asset_number_mismatch"
                    ),
                )
                # 此前这里直接 `continue`，句子连 `downgraded_sentences` 都没进——
                # 原创论文模式下被资产接地规则删掉的句子，在任何地方都查不到。
                dropped.append(sentence)
                continue
            sentence["source_refs"] = refs
            kept.append(sentence)
        if paragraph.get("sentences") is not None:
            paragraph["sentences"] = kept
            if dropped:
                paragraph["downgraded_sentences"] = (
                    list(paragraph.get("downgraded_sentences") or []) + dropped
                )
            paragraph["text"] = " ".join(str(item.get("text") or "") for item in kept).strip()
    return [
        paragraph
        for paragraph in paragraphs
        if str(paragraph.get("text") or "").strip() or paragraph.get("downgraded_sentences")
    ]


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


def sentence_downgrade_events(draft: SectionDraft) -> list[dict[str, Any]]:
    """把一份草稿里所有被规则拿掉/改写的句子摊成可落库的事件。

    这是 Draft→IR 那道坎的解法：``to_ir_section()`` 只读 ``paragraph["sentences"]``，
    审计线索到那一步就没了，所以在**草稿**上把它取出来，与正文同一事务落库。

    :returns: 每句一条，带规则、原句、段落/句序、引用与证据绑定、locator 状态。
    """
    events: list[dict[str, Any]] = []
    for paragraph_index, paragraph in enumerate(draft.paragraphs, start=1):
        entries = list(paragraph.get("downgraded_sentences") or [])
        # R4 归因是**改写**：句子仍在正文里，但同样是规则动过的，要能查。
        entries += [
            sentence
            for sentence in paragraph.get("sentences") or []
            if sentence.get("downgraded_reason")
        ]
        for sentence_index, sentence in enumerate(entries, start=1):
            rule = str(sentence.get("downgraded_reason") or "")
            if not rule:
                continue
            locators = list(sentence.get("downgraded_locators") or [])
            if not locators:
                locator_status = "no_evidence"
            elif any(item.get("located") for item in locators):
                locator_status = "located"
            else:
                locator_status = "unlocated"
            events.append(
                {
                    "section_key": draft.section_key,
                    "paragraph_index": paragraph_index,
                    "sentence_index": sentence_index,
                    "rule": rule,
                    # 正文里还有内容 = 被改写；空 = 被整句拿掉。
                    "outcome": (
                        "rewritten" if str(sentence.get("text") or "").strip() else "removed"
                    ),
                    "text": str(sentence.get("downgraded_text") or sentence.get("text") or ""),
                    "cite_keys": list(sentence.get("downgraded_cite_keys") or []),
                    "evidence_ids": list(sentence.get("downgraded_evidence_ids") or []),
                    "locator_status": locator_status,
                    "locators": locators,
                }
            )
    return events


def record_downgrade(
    sentence: dict[str, Any],
    *,
    rule: str,
    units: list[dict[str, Any]] | None = None,
) -> None:
    """把一句从正文里拿掉之前，先留下追责需要的一切。

    降级分支紧接着就会清空 ``text`` / ``cite_keys`` / ``evidence_ids``，所以审计副本
    必须在清空**之前**取——此前 `downgraded_sentences` 收集到的是清空后的空壳，
    连原句都没有，而它又在 `to_ir_section()` 那一步整个丢掉了。

    ``setdefault`` 是有意的：一句可能先被一条规则降级、再被另一条改写，最早那一次
    看到的才是模型真正写出来的原文。

    :param sentence: 正在被降级的句子（原地修改）。
    :param rule: 触发的规则代号，例如 ``R6_locator_missing``。
    :param units: 该句绑定的证据单元，用来记录当时的 locator 状态。
    """
    sentence["downgraded_reason"] = rule
    sentence.setdefault("downgraded_text", str(sentence.get("text") or ""))
    sentence.setdefault("downgraded_cite_keys", list(sentence.get("cite_keys") or []))
    sentence.setdefault("downgraded_evidence_ids", list(sentence.get("evidence_ids") or []))
    sentence.setdefault(
        "downgraded_locators",
        [
            {
                "evidence_id": unit.get("evidence_id"),
                "grade": unit.get("grade"),
                "located": is_located(unit),
                "display": locator_display(unit),
            }
            for unit in units or []
        ],
    )


def enforce_sentence_evidence_rules(
    paragraphs: list[dict[str, Any]],
    *,
    evidence_by_id: dict[str, dict[str, Any]],
    language: str,
) -> list[dict[str, Any]]:
    """写作后处理的 R4/R5/R6：违规句降级改写，不让摘要冒充全文。"""
    from paperforge_worker.pipelines.quality import classify_claim

    attributions = 0
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
            comparable = claim_kind != "comparison" or comparison_admissible(units)
            # 数字句要能被查证。判定与证据定级、质检 R6、定位显示共用同一个谓词
            # （paperforge_worker.locators）：此前这里只认 page/object_ref，而定级
            # 认 page/section/paragraph，于是本函数把 5,256 条被定级为「已定位」的
            # 单元当成未定位，把引用它们的句子整句删掉——包括提示词里明明标着
            # 「§Results」的那些。
            located = claim_kind != "numeric" or any(is_located(unit) for unit in units)
            if units and grade_ok and comparable and located:
                continue
            if claim_kind == "comparison" and units and not comparable:
                # 不可比的跨研究比较必须从正文里拿掉，而不是换成一句「结果应分别陈述」。
                # 那句话是写给系统看的判定说明，不是作者的论述：它出现在成稿里读起来
                # 就是一行模板（实测出现在项目 6a6bbf18 的 s4 正文中段）。下面
                # R6/R4 分支早就确立了正确做法——句子留在审计记录里，正文里删掉。
                record_downgrade(sentence, rule="R5_not_comparable", units=units)
                sentence["text"] = ""
                sentence["cite_keys"] = []
                sentence["evidence_ids"] = []
            elif (
                units
                and all(unit.get("grade") == "D_abstract_only" for unit in units)
                and attributions < MAX_ABSTRACT_ATTRIBUTIONS_PER_SECTION
            ):
                attributions += 1
                original = str(sentence.get("text") or "")
                record_downgrade(sentence, rule="R4_abstract_attribution", units=units)
                # Reads as authorial attribution rather than as a machine note.
                # The old Chinese wording ("摘要层面的作者表述是：") was a visible
                # template artifact, and — unlike the English branch — it matched
                # none of `_ATTRIBUTION_RE`'s verbs, so the downgraded sentence
                # was not even recognised as attribution downstream.
                sentence["text"] = (
                    f"该文献在摘要中报告，{original.lstrip()}"
                    if language == "zh"
                    else (f"The cited work reports in its abstract that {original.rstrip('.')}.")
                )
            else:
                # Do not spray an internal quality warning into the prose. The
                # discarded sentence and reason remain in claim/evidence audit
                # records and the evidence appendix.
                record_downgrade(
                    sentence,
                    rule="R6_locator_missing" if not located else "R4_grade_missing",
                    units=units,
                )
                sentence["text"] = ""
                sentence["cite_keys"] = []
                sentence["evidence_ids"] = []
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
    *,
    language: str = "en",
    reason: str = "evidence_gap",
) -> list[dict[str, Any]]:
    """确定性回退：说明这一节为什么还没有正文，**绝不**替模型写正文。

    此前这里做的是「用现成材料拼出可读的占位」，实际拼出来的东西不能交付：

    - ``section["summary"]`` 是大纲写给**写作模型**的指令（「回答该子问题；当前证据
      状态：一致。」），当成正文段落输出，读者看到的是一句工单；
    - 证据段落是 ``evidence_unit.text`` **逐字**照搬——那是第三方论文的原文，带着
      它自己的 ``(Zipfel, 2014)``、``[ 94 , 95 ]`` 标注和双栏 PDF 抽取的粘连乱码，
      而且不会翻译（中文综述里整段英文）。生产实测（项目 6a6bbf18，2026-08-14）
      11 节中 5 节走了这条路，s3/s4/s6 就是这样把别人的正文原样交了出去。

    降级的正确含义是「这一节没写出来」，不是「用原文顶上」。所以这里只留一句诚实的
    说明并交给 ``needs_rewrite`` 流程，正文缺口对作者始终可见。

    ``reason="evidence_gap"`` 是「证据不够，写不了」；``"write_failed"`` 是「有证据，
    但没拿到模型输出」。两者对作者的下一步动作完全不同——补来源，还是重跑这一节。
    """
    title = str(section.get("title") or "").strip()
    if reason == "write_failed":
        text = (
            f"本节“{title}”尚未生成：没有可用的写作模型输出，待重写。"
            if language == "zh"
            else (
                f"This section ({title}) was not generated: no usable writer-model output. "
                "It needs to be rewritten."
            )
        )
        return [{"text": text, "cite_keys": [], "needs_rewrite": True}]
    # Never turn an empty evidence contract into an alphabetical dump of every
    # library card.  That looks like a review while silently admitting papers
    # outside the section's scope.  Keep the gap visible for the author instead.
    gap = (
        f"本节“{title}”尚无满足定位与可比性要求的证据；待补充可核验来源后再展开论述。"
        if language == "zh"
        else (
            f"This section ({title}) has no evidence meeting the required provenance "
            "and comparability criteria; add verifiable sources before drafting the discussion."
        )
    )
    return [{"text": gap, "cite_keys": [], "evidence_gap": True}]


_NUMBER_REPAIR_PROMPT_ZH = """你是学术论文的事实校订编辑。给定一节正文与一份「无法溯源的数值」清单，
逐句改写**只含这些数值的句子**，让论断不再依赖查不到出处的具体数字。

只输出 JSON，结构与输入相同：
{"sentences": [{"index": 0, "text": "改写后的句子"}]}

硬性要求：
- 优先保留论断本身，只把无出处的数字改成定性表述（如「显著增加」「多数研究报告」）；
- 改写后的句子里**不得出现任何新的数字**，也不得保留清单里的那些数值；
- 不得新增、删除或调换引用；不在正文里写 [1]、(Smith 2020) 之类的标记；
- 如果去掉数字后这句话就没有内容了，把 text 设为空字符串——删掉好过留一句空话。"""

_NUMBER_REPAIR_PROMPT_EN = """You are a fact-checking editor. Given one section and a list of
figures that cannot be traced to any evidence, rewrite only the sentences containing them so the
claim no longer rests on an unverifiable number.

Output JSON only, same shape as the input:
{"sentences": [{"index": 0, "text": "rewritten sentence"}]}

Requirements:
- keep the claim, restate the untraceable figure qualitatively (e.g. "substantially increased");
- the rewritten sentence must contain NO digits at all, and must not keep the listed values;
- never add, drop, or swap citations; never write inline markers like [1] or (Smith 2020);
- if the sentence has nothing left once the number goes, return an empty string — deleting it
  beats leaving an empty assertion."""


@dataclass(frozen=True)
class SectionDefect:
    """一节稿子没达到「成熟正文」的一个具体原因。"""

    code: str
    detail: str
    #: 这条缺陷能不能靠重写这一节修好。不可恢复的（证据不足）重写多少次都一样。
    recoverable: bool = True

    def correction(self, *, language: str) -> str:
        """回灌给模型的纠正指令。空串表示这条缺陷没法靠改提示词修。"""
        table = _SECTION_CORRECTIONS.get(self.code, {})
        return table.get("zh" if language == "zh" else "en", "")


_SECTION_CORRECTIONS: dict[str, dict[str, str]] = {
    "verbatim_evidence_copy": {
        "zh": (
            "上一版把证据原文整段照抄进了正文。必须用你自己的话重写：先给出论断，"
            "再说明证据支持它的哪一部分；可以引用具体数值和结论，但不得成段复制原文措辞。"
        ),
        "en": (
            "The previous draft copied evidence text verbatim. Rewrite it in your own words: "
            "state the claim first, then say what the evidence supports. Cite specific values "
            "and findings, but never reproduce whole passages of the source wording."
        ),
    },
    "language_mismatch": {
        "zh": (
            "上一版有整段英文。正文必须全部用中文写作，"
            "英文只允许出现在术语、缩写、模型或数据集名里。"
        ),
        "en": "The previous draft contained non-English paragraphs. Write all prose in English.",
    },
    "too_short": {
        "zh": "上一版篇幅明显不足，只写了个开头。请按目标长度完整展开本节论证。",
        "en": (
            "The previous draft was far too short. Develop the full argument to the target length."
        ),
    },
    "below_target": {
        "zh": (
            "上一版没写够本节的目标篇幅。用手上已有的证据把论证展开：把同一条论断下的"
            "多篇证据放到一起比较，说明条件差异，并把还没答上的方面明说出来。"
            "**不要**为了凑长度重复已经说过的话或加空泛的过渡句。"
        ),
        "en": (
            "The previous draft fell short of this section's target length. Develop the argument "
            "with the evidence already provided: group multiple sources under one claim and "
            "compare them, state the conditions that differ, and name the aspects still "
            "unanswered. Do NOT pad with restatement or filler transitions."
        ),
    },
}


#: 一节至少要写到自己篇幅目标的这个比例，否则退回重写一次。
#: 此前的验收线是与目标完全脱钩的绝对值（正文 180 字 / 框架 80 字），而目标是
#: 1200 / 350–800——一节只写到目标的四分之一也算合格。实测（项目 ff6b9983 第 3 版）
#: 十节里有五节首稿在 264–583 字之间，全部一次通过，没有任何一次 too_short 重写。
MIN_TARGET_RATIO = 0.5


def section_absolute_minimum(*, language: str, is_frame: bool) -> int:
    """低于这个字数就不是「写薄了」，是没写出来。与篇幅目标无关。"""
    zh = language == "zh"
    if is_frame:
        return MIN_FRAME_WORDS_ZH if zh else MIN_FRAME_WORDS_EN
    return MIN_BODY_WORDS_ZH if zh else MIN_BODY_WORDS_EN


def section_minimum_words(
    section: dict[str, Any] | None,
    *,
    language: str,
    is_frame: bool,
) -> int:
    """这一节至少要写多少字才算写完。

    与本节自己的篇幅目标挂钩，但**证据本来就窄的小节不抬线**：那种情况下逼长度
    就是逼灌水，而灌水正是要防的东西（``evidence_limited`` 的小节另有「说清证据
    有多窄」的写作要求，见 ``_evidence_limitation_line``）。证据薄的真正修法在
    上游——把文献库从 9 篇修到 36 篇之后，多数小节根本不会再是 evidence_limited。
    """
    zh = language == "zh"
    floor = (MIN_FRAME_WORDS_ZH if zh else MIN_FRAME_WORDS_EN) if is_frame else (
        MIN_BODY_WORDS_ZH if zh else MIN_BODY_WORDS_EN
    )
    section = section or {}
    if section.get("evidence_limited"):
        return floor
    declared = section.get("target_words")
    if is_frame and not declared:
        # 没有声明目标的框架章节（早于框架写作交代的旧大纲）沿用绝对下限：拿正文小节
        # 的 1200 字目标去量一篇摘要，会把一份正常的摘要判成没写完。
        return floor
    target = int(declared or (TARGET_WORDS_PER_SECTION_ZH if zh else TARGET_WORDS_PER_SECTION_EN))
    return max(floor, int(target * MIN_TARGET_RATIO))


def inspect_section_draft(
    draft: SectionDraft,
    *,
    language: str,
    evidence: list[dict[str, Any]] | None = None,
    is_frame: bool = False,
    section: dict[str, Any] | None = None,
) -> list[SectionDefect]:
    """判定一节稿子是否够格进入成稿——写作循环与交付门用的是同一个判据。

    这是**写作时**的验收，不是事后体检：同一组判据在生成回路里驱动重写，在交付
    门里决定能不能算完整。两处若各写一套，最终必然出现「质量门说有问题、写作层
    却认为已经写完」的稳定分歧。
    """
    if not draft.has_body:
        return [
            SectionDefect(
                code="not_generated",
                detail=draft.failure_reason or "no model output",
            )
        ]
    if draft.generator == "deterministic_search_log":
        # 检索方法节是**故意**确定性生成的：它是一份检索日志，不是论证。拿正文的
        # 篇幅线去量它，会把一节正确的产物判成没写成，然后反复重写——而重写只会
        # 再确定性地生成同一段文字。
        return (
            []
            if any(str(p.get("text") or "").strip() for p in draft.paragraphs)
            else [SectionDefect(code="not_generated", detail="empty search log")]
        )

    defects: list[SectionDefect] = []
    # 「没写出来」和「写薄了」是两件事，必须分开报。
    #
    # ``too_short`` 是完整性缺陷：低于绝对下限的东西是个残句，交付时该拦。
    # ``below_target`` 只是没写够本节的篇幅目标——它有正文、有引用，只是短。把它也
    # 算成完整性缺陷，交付判定就会说出「6 个章节在重试与自动修复之后仍然没有正文」
    # 这种与事实相反的话（实测：那 6 节里引言 515 字、s6 649 字）。两者都驱动写作
    # 回路里的重写，但只有前者能否决交付。
    #
    # 附录（证据台账）不量篇幅：它的正文是一张表加两句说明，不是论证。拿正文的篇幅线
    # 去量它，会把一节**正确的**产物判成没写成——和上面检索方法节那条豁免同理。实测
    # 台账 192 字被报成「1 个章节仍然没有正文」，而它的 block 是 paragraph、paragraph、
    # table，表就是它的内容。语种、逐字照抄这些检查照旧对附录生效。
    # 有子节的母节只写一段引入，子节各自成篇并各自计长。拿正文小节的篇幅线去量
    # 一段引入，会把一节**正确的**产物判成没写成——和附录、检索方法节同理。
    has_children = bool((section or {}).get("has_children"))
    if not (draft.appendix or (section or {}).get("appendix") or has_children):
        absolute_floor = section_absolute_minimum(language=language, is_frame=is_frame)
        target_floor = section_minimum_words(section, language=language, is_frame=is_frame)
        if draft.word_count < absolute_floor:
            defects.append(
                SectionDefect(
                    code="too_short",
                    detail=f"{draft.word_count} words < {absolute_floor}",
                )
            )
        elif draft.word_count < target_floor:
            defects.append(
                SectionDefect(
                    code="below_target",
                    detail=f"{draft.word_count} words < {target_floor}",
                )
            )

    from paperforge_worker.pipelines.quality import (
        verbatim_evidence_copies,
        zh_language_mismatches,
    )

    row = _draft_as_section_row(draft)
    if language == "zh" and zh_language_mismatches([row]):
        defects.append(SectionDefect(code="language_mismatch", detail="latin-script paragraphs"))

    evidence_units = {
        str(item.get("evidence_id")): {"text": str(item.get("text") or "")}
        for item in evidence or []
        if item.get("evidence_id")
    }
    copies = verbatim_evidence_copies([row], evidence_units)
    if copies:
        defects.append(
            SectionDefect(
                code="verbatim_evidence_copy",
                detail=f"{len(copies)} paragraph(s) overlap >= {copies[0]['overlap']}",
            )
        )
    return defects


async def repair_unsourced_numbers(
    *,
    draft: SectionDraft,
    values: set[str],
    language: str,
    runner: LLMRunner | None = None,
) -> tuple[SectionDraft, dict[str, int]]:
    """把查不到出处的数值从正文里清掉：先改写，改不动就删句。

    此前 NUMLINT 只报数（``unsourced_number_count``），稿子照常交付——一个没有出处的
    「超过 200 种化合物」读起来和有出处的一模一样，而审稿人只会当它是编的。

    阶梯与写作恢复同构：先让模型把论断改成定性表述（保留论点、去掉数字），改写要过
    三道校验——数值真的没了、没引入新数字、引用绑定逐字未变；过不了就删掉那一句，
    审计信息留在段落上。绝不「保留原句只加个警告」。
    """
    stats = {"rewritten": 0, "removed": 0, "kept": 0}
    if not values:
        return draft, stats

    from ingest.assets import normalize_number
    from ingest.numlint import numbers_in

    wanted = {normalize_number(value) for value in values}
    targets: list[dict[str, Any]] = []
    for paragraph in draft.paragraphs:
        for sentence in paragraph.get("sentences") or []:
            found = {normalize_number(item) for item in numbers_in(str(sentence.get("text") or ""))}
            if found & wanted:
                targets.append(sentence)
    if not targets:
        return draft, stats

    rewritten: dict[int, str] = {}
    if runner is not None and runner.enabled:
        listing = "\n".join(
            f"{index}. {str(sentence.get('text') or '')}" for index, sentence in enumerate(targets)
        )
        result = await runner.agenerate_json(
            "writer",
            system_prompt=(
                _NUMBER_REPAIR_PROMPT_ZH if language == "zh" else _NUMBER_REPAIR_PROMPT_EN
            ),
            user_prompt=(
                f"无法溯源的数值：{', '.join(sorted(values))}\n\n需要改写的句子：\n{listing}"
                if language == "zh"
                else (
                    f"Untraceable figures: {', '.join(sorted(values))}\n\n"
                    f"Sentences to rewrite:\n{listing}"
                )
            ),
            max_output_tokens=SECTION_MAX_OUTPUT_TOKENS,
            temperature=0.0,
            metadata={"stage": "numlint_repair", "section": draft.section_key},
        )
        if result.ok and isinstance(result.value, dict):
            for item in result.value.get("sentences") or []:
                if not isinstance(item, dict):
                    continue
                try:
                    index = int(str(item.get("index")))
                except (TypeError, ValueError):
                    continue
                if 0 <= index < len(targets):
                    rewritten[index] = _clean_paragraph(item.get("text"))

    for index, sentence in enumerate(targets):
        candidate = rewritten.get(index)
        # 校验：改写后一个数字都不许剩（既清掉了目标值，也堵住「换一个数字」）。
        # 引用绑定不动——这里只改 text，cite_keys/evidence_ids 逐字保留。
        if candidate and not numbers_in(candidate):
            sentence["text"] = candidate
            sentence["repaired_reason"] = "numlint_unsourced_number"
            stats["rewritten"] += 1
            continue
        record_downgrade(sentence, rule="numlint_unsourced_number")
        sentence["text"] = ""
        stats["removed"] += 1

    draft.paragraphs = _drop_blanked_sentences(draft.paragraphs)
    return draft, stats


def _drop_blanked_sentences(paragraphs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """删掉被清空的句子并重算段落文本；审计条目留在段落上。"""
    for paragraph in paragraphs:
        sentences = paragraph.get("sentences") or []
        if not sentences:
            continue
        kept = [item for item in sentences if str(item.get("text") or "").strip()]
        dropped = [item for item in sentences if not str(item.get("text") or "").strip()]
        paragraph["sentences"] = kept
        if dropped:
            paragraph["downgraded_sentences"] = (
                list(paragraph.get("downgraded_sentences") or []) + dropped
            )
        paragraph["text"] = " ".join(str(item.get("text") or "").strip() for item in kept)
    return [
        paragraph
        for paragraph in paragraphs
        if str(paragraph.get("text") or "").strip()
        or any(str(item.get("text") or "").strip() for item in paragraph.get("sentences") or [])
    ]


def _draft_as_section_row(draft: SectionDraft) -> Any:
    """把草稿包成质量检查认得的行对象，复用同一批检测函数而不是另写一遍。"""
    from types import SimpleNamespace

    return SimpleNamespace(
        id=None,
        section_key=draft.section_key,
        title=draft.title,
        status="generated",
        cite_keys_json=sorted({key for p in draft.paragraphs for key in p.get("cite_keys", [])}),
        body_ir_json=draft.to_ir_section().model_dump(mode="json"),
    )


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
