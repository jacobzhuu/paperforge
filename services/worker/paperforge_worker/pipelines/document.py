"""文稿装配：大纲 → 分节写作 → PaperIR → 持久化 → Markdown 预览。

这里是 R2 的最后一道闸：所有章节汇成 PaperIR 后，再跑一次 typed-IR 白名单检查
（``PaperIR.enforce_cite_key_whitelist``），任何漏网的 cite key 在此被 strip
并写入 ``citation_warnings``——渲染器永远拿不到白名单外的引用。

Draft-first：单章失败只影响该章（留占位 + 告警），其余章节照常交付。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from db import (
    create_document,
    get_cards,
    get_writing_whitelist,
    latest_outline,
    list_entries,
    parsed_asset_payloads,
    reference_metadata_payload,
    replace_citation_usage,
    upsert_section,
)
from ingest.numlint import lint_sections
from observability import get_logger
from paper_ir import (
    Bibliography,
    PaperIR,
    PaperMeta,
    ReferenceMetadata,
    render_markdown,
)

from paperforge_worker.context import JobContext
from paperforge_worker.pipelines.writing import (
    SectionDraft,
    WritingContext,
    coherence_pass,
    count_words,
    write_section,
)

logger = get_logger(__name__)

# 摘要/引言/结论在正文写完后生成（设计 §4.4.1）。
FRAME_ORDER = {"abstract": -2, "introduction": -1, "conclusion": 999}


@dataclass
class WriteOutcome:
    document_id: str | None = None
    section_count: int = 0
    word_count: int = 0
    cite_key_count: int = 0
    citation_warning_count: int = 0
    rewrite_count: int = 0
    numlint_consistent: bool = True
    unsourced_number_count: int = 0
    generator_mix: dict[str, int] = field(default_factory=dict)
    warnings: list[dict[str, Any]] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "section_count": self.section_count,
            "word_count": self.word_count,
            "cite_key_count": self.cite_key_count,
            "citation_warning_count": self.citation_warning_count,
            "rewrite_count": self.rewrite_count,
            "numlint_consistent": self.numlint_consistent,
            "unsourced_number_count": self.unsourced_number_count,
            "generator_mix": self.generator_mix,
            "warnings": self.warnings,
        }


async def write_document(
    context: JobContext,
    *,
    language: str = "en",
    title: str = "",
    coherence: bool = True,
) -> WriteOutcome:
    """按大纲逐章写作并落库，返回统计。"""
    outcome = WriteOutcome()
    runner = context.llm_runner()

    async with context.session() as session:
        outline_row = await latest_outline(session, context.project_id)
        if outline_row is None:
            outcome.warnings.append({"stage": "write", "reason": "no_outline"})
            return outcome
        outline = dict(outline_row.tree_json or {})
        whitelist = await get_writing_whitelist(session, context.project_id)
        cards = await _card_context(session, context.project_id)
        assets = await parsed_asset_payloads(session, context.project_id)
        outline_id = outline_row.id

    sections = [s for s in (outline.get("sections") or []) if isinstance(s, dict)]
    if not sections:
        outcome.warnings.append({"stage": "write", "reason": "empty_outline"})
        return outcome

    writing_context = WritingContext(outline=outline, language=language)
    body_sections = [s for s in sections if s.get("kind") != "frame"]
    frame_sections = [s for s in sections if s.get("kind") == "frame"]

    drafts: dict[str, SectionDraft] = {}
    for index, section in enumerate(body_sections):
        draft = await _write_one(
            section=section,
            cards=cards,
            whitelist=set(whitelist),
            writing_context=writing_context,
            runner=runner,
            context=context,
            outcome=outcome,
            assets=assets,
        )
        drafts[draft.section_key] = draft
        writing_context.register(draft.section_key, draft)
        await context.emit(
            "write.section",
            {
                "section": draft.section_key,
                "title": draft.title,
                "words": draft.word_count,
                "generator": draft.generator,
            },
            stage="write",
            progress=None,
        )
        del index

    # 框架章节后写：此时滚动摘要已覆盖全部正文。
    for section in frame_sections:
        draft = await _write_one(
            section={**section, "summary": _frame_goal(section, outline, language)},
            cards=cards,
            whitelist=set(whitelist),
            writing_context=writing_context,
            runner=runner,
            context=context,
            outcome=outcome,
            assets=assets,
        )
        drafts[draft.section_key] = draft

    if coherence:
        for key, draft in drafts.items():
            if draft.generator.startswith("llm"):
                drafts[key] = await coherence_pass(
                    draft=draft,
                    context=writing_context,
                    whitelist=set(whitelist),
                    runner=runner,
                )

    ordered = _ordered_drafts(sections, drafts)
    ir = _build_paper_ir(
        title=title or str(outline.get("topic") or ""),
        language=language,
        drafts=ordered,
    )

    # R2 最后一道：typed-IR 白名单检查，越权 key 在此 strip + 留告警。
    violations = ir.enforce_cite_key_whitelist(set(whitelist), strip=True)
    if violations:
        context.warn("citecheck", "cite_keys_stripped", {"count": len(violations)})
        outcome.warnings.append(
            {"stage": "citecheck", "reason": "cite_keys_stripped", "count": len(violations)}
        )

    async with context.session() as session:
        document = await create_document(
            session,
            project_id=context.project_id,
            outline_id=outline_id,
        )
        for order_no, (section_key, draft) in enumerate(ordered):
            ir_section = next((s for s in ir.sections if s.key == section_key), None)
            body_ir = ir_section.model_dump(mode="json") if ir_section else None
            cite_keys = sorted(
                {key for p in draft.paragraphs for key in p.get("cite_keys", [])}
            )
            row = await upsert_section(
                session,
                document_id=document.id,
                section_key=section_key,
                title=draft.title,
                order_no=order_no,
                body_ir=body_ir,
                cite_keys=cite_keys,
                model=draft.model,
            )
            usages = [
                {
                    "work_id": whitelist[key],
                    "cite_key": key,
                    "context_snippet": _snippet(draft, key),
                }
                for key in cite_keys
                if key in whitelist
            ]
            await replace_citation_usage(
                session,
                project_id=context.project_id,
                section_id=row.id,
                usages=usages,
            )
        outcome.document_id = str(document.id)

    outcome.section_count = len(ordered)
    outcome.word_count = sum(draft.word_count for _key, draft in ordered)
    outcome.cite_key_count = len(ir.collect_cite_keys())
    outcome.citation_warning_count = sum(
        len(section.citation_warnings) for section in ir.sections
    )
    outcome.rewrite_count = sum(draft.rewrite_count for _key, draft in ordered)

    # NUMLINT：正文数值 vs 素材解析值（设计 §4.4.2 红线的 lint 层）。
    report = lint_sections(
        [
            {
                "section_key": key,
                "text": " ".join(p.get("text", "") for p in draft.paragraphs),
            }
            for key, draft in ordered
        ],
        parsed_assets=assets,
    )
    outcome.numlint_consistent = report.consistent
    outcome.unsourced_number_count = len(report.unsourced)
    if not report.consistent:
        context.warn("numlint", "unsourced_numbers", {"count": len(report.unsourced)})
        outcome.warnings.append(
            {
                "stage": "numlint",
                "reason": "unsourced_numbers",
                "count": len(report.unsourced),
                "samples": [f.to_payload() for f in report.unsourced[:5]],
            }
        )
        await context.emit(
            "numlint.flagged",
            {"unsourced_count": len(report.unsourced)},
            stage="numlint",
        )
    for _key, draft in ordered:
        kind = draft.generator.split(":")[0]
        outcome.generator_mix[kind] = outcome.generator_mix.get(kind, 0) + 1
    return outcome


async def build_markdown(
    context: JobContext,
    *,
    document_id: uuid.UUID | None = None,
) -> str:
    """从持久化的 Section IR 渲染 Markdown 预览（渲染器只消费 IR）。"""
    from db import latest_document, list_sections
    from db.models.library import ScholarlyWork
    from paper_ir.schema import Section as IRSection

    async with context.session() as session:
        document = (
            await session.get(_document_model(), document_id)
            if document_id
            else await latest_document(session, context.project_id)
        )
        if document is None:
            return ""
        rows = await list_sections(session, document.id)
        whitelist = await get_writing_whitelist(session, context.project_id)
        entries = await list_entries(session, context.project_id, status="selected")
        references: list[ReferenceMetadata] = []
        used_keys = {key for row in rows for key in (row.cite_keys_json or [])}
        for entry, work in entries:
            if entry.bibtex_key in used_keys:
                payload = await reference_metadata_payload(
                    session, work, bibtex_key=entry.bibtex_key
                )
                references.append(ReferenceMetadata(**payload))
        del ScholarlyWork

    sections = [IRSection(**row.body_ir_json) for row in rows if row.body_ir_json]
    project_title = ""
    async with context.session() as session:
        from db import get_project

        project = await get_project(session, context.project_id)
        if project is not None:
            project_title = project.title
            language = project.language
            citation_style = project.citation_style
        else:
            language = "en"
            citation_style = "author_year"

    ir = PaperIR(
        meta=PaperMeta(title=project_title, language=language),
        sections=sections,
        bibliography=Bibliography(style=citation_style),  # type: ignore[arg-type]
    )
    # 双保险：预览也走一次白名单检查，绝不展示越权引用。
    ir.enforce_cite_key_whitelist(set(whitelist), strip=True)
    return render_markdown(ir, references=references, style=citation_style)


def _document_model():
    from db.models.paper import PaperDocument

    return PaperDocument


async def _write_one(
    *,
    section: dict[str, Any],
    cards: dict[str, dict[str, Any]],
    whitelist: set[str],
    writing_context: WritingContext,
    runner,
    context: JobContext,
    outcome: WriteOutcome,
    assets: list[dict[str, Any]] | None = None,
) -> SectionDraft:
    section_key = str(section.get("key") or "section")
    try:
        return await write_section(
            section=section,
            cards=cards,
            whitelist=whitelist,
            context=writing_context,
            runner=runner,
            assets=assets,
        )
    except Exception as error:  # noqa: BLE001 - 单章失败不阻断整篇（draft-first）
        logger.warning(
            "section writing failed",
            extra={"section": section_key, "error": type(error).__name__},
        )
        context.warn("write", type(error).__name__, {"section": section_key})
        outcome.warnings.append(
            {"stage": "write", "section": section_key, "reason": type(error).__name__}
        )
        draft = SectionDraft(
            section_key=section_key,
            title=str(section.get("title") or section_key),
        )
        draft.paragraphs = [{"text": "", "cite_keys": []}]
        draft.generator = "failed"
        return draft


async def _card_context(session, project_id: uuid.UUID) -> dict[str, dict[str, Any]]:
    """cite_key → 卡片内容（写作上下文）。只包含已分配 key 的入库文献。"""
    cards = await get_cards(session, project_id)
    entries = await list_entries(session, project_id, status="selected")
    context: dict[str, dict[str, Any]] = {}
    for entry, work in entries:
        if not entry.bibtex_key:
            continue
        card = cards.get(work.id)
        context[entry.bibtex_key] = {
            "title": work.canonical_title,
            "year": work.publication_year,
            "venue": work.venue_name,
            "summary": (card.summary if card else None) or work.abstract,
            "contributions": (card.contributions_json if card else None) or [],
            "methods": (card.methods_json if card else None) or [],
            "results": (card.results_json if card else None) or [],
            "limitations": (card.limitations_json if card else None) or [],
        }
    return context


def _ordered_drafts(
    sections: list[dict[str, Any]],
    drafts: dict[str, SectionDraft],
) -> list[tuple[str, SectionDraft]]:
    ordered: list[tuple[str, SectionDraft]] = []
    for index, section in enumerate(sections):
        key = str(section.get("key") or "")
        draft = drafts.get(key)
        if draft is not None:
            ordered.append((key, draft))
        del index
    return ordered


def _build_paper_ir(
    *,
    title: str,
    language: str,
    drafts: list[tuple[str, SectionDraft]],
) -> PaperIR:
    abstract = ""
    sections = []
    for key, draft in drafts:
        if key == "abstract":
            # 摘要同时进 meta（渲染用）与 sections（持久化用）——两边都要有，
            # 否则编辑器里摘要会变成空章节。
            abstract = " ".join(p.get("text", "") for p in draft.paragraphs).strip()
        sections.append(draft.to_ir_section())
    return PaperIR(
        meta=PaperMeta(title=title, abstract=abstract, language=language),  # type: ignore[arg-type]
        sections=sections,
    )


def _frame_goal(section: dict[str, Any], outline: dict[str, Any], language: str) -> str:
    key = section.get("key")
    zh = language == "zh"
    question = outline.get("research_question") or outline.get("topic") or ""
    if key == "abstract":
        return (
            f"用 150-250 字概述本综述：研究问题（{question}）、覆盖范围、主要发现与结论。"
            if zh
            else f"Summarize the review in 150-250 words: question ({question}), scope, findings."
        )
    if key == "introduction":
        return (
            "交代研究背景与重要性、已有工作的局限、本综述的范围与组织结构。"
            if zh
            else "Motivate the topic, state gaps in prior work, and lay out the review's scope."
        )
    if key == "conclusion":
        return (
            "总结主要发现、指出开放问题与未来方向；不要引入正文未讨论的新内容。"
            if zh
            else "Summarize findings and open problems; introduce nothing new."
        )
    return str(section.get("summary") or "")


def _snippet(draft: SectionDraft, cite_key: str) -> str | None:
    for paragraph in draft.paragraphs:
        if cite_key in paragraph.get("cite_keys", []):
            return str(paragraph.get("text", ""))[:300]
    return None


def total_words(drafts: list[SectionDraft]) -> int:
    return sum(count_words(p.get("text", "")) for d in drafts for p in d.paragraphs)
