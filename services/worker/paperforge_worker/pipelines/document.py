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
    apply_generated_publication_metadata,
    create_document,
    get_cards,
    get_project,
    get_writing_whitelist,
    latest_outline,
    list_entries,
    parsed_asset_payloads,
    polish_skip_requested,
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
from paper_ir.schema import Section as IRSection

from paperforge_worker.context import JobContext
from paperforge_worker.pipelines.publication_metadata import generate_publication_metadata
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

# write 阶段在整条管线里占的进度区间（与 worker._STAGE_PROGRESS 对齐）。
# 分节写作与连贯性润色各占一段：润色是独立阶段名（polish），界面上不能再叫「分节写作」——
# 那会让人对着一屏「已生成」的章节以为系统卡死了。
_WRITE_PROGRESS_START = 0.65
_POLISH_PROGRESS_START = 0.82
_WRITE_PROGRESS_END = 0.90

WRITE_STAGE = "write"
POLISH_STAGE = "polish"


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
    polished_count: int = 0
    polish_pending_count: int = 0
    polish_skipped: bool = False
    publication_metadata_generator: str | None = None

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
            "polished_count": self.polished_count,
            # 跳过是用户的主动选择，不是降级；但产物里必须留下痕迹，
            # 否则「这稿为什么读起来不如上次连贯」将无从追溯。
            "polish_pending_count": self.polish_pending_count,
            "polish_skipped": self.polish_skipped,
            "publication_metadata_generator": self.publication_metadata_generator,
        }


async def write_document(
    context: JobContext,
    *,
    language: str = "en",
    title: str = "",
    coherence: bool = True,
    paper_type: str = "review",
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

    writing_context = WritingContext(outline=outline, language=language, paper_type=paper_type)
    body_sections = [s for s in sections if s.get("kind") != "frame"]
    frame_sections = [s for s in sections if s.get("kind") == "frame"]
    order_by_key = {str(s.get("key") or ""): index for index, s in enumerate(sections)}

    # 建档提前到写作之前。write 是全管线最长的一段（本地实测 7 节约 18 分钟），
    # 而此前所有章节只在末尾一次性 upsert：中途撞上 arq job_timeout 或 worker 重启，
    # 已经写完的章节连同那十几分钟的 LLM 调用一起蒸发，重跑只能从头再写一遍。
    # 现在写完一节存一节，末尾那次全量 upsert 仍然保留（它带全篇白名单终检），
    # 只是不再是唯一的落库时机。
    async with context.session() as session:
        document = await create_document(
            session,
            project_id=context.project_id,
            outline_id=outline_id,
        )
        document_id = document.id
    outcome.document_id = str(document_id)

    # 分节写作占 0.65→0.82，润色占 0.82→0.90：两段各自线性摊，
    # 用户看到的百分比因此和「写到第几节 / 润到第几节」对得上。
    written = 0
    total_sections = len(sections)

    def _write_progress() -> float:
        span = _POLISH_PROGRESS_START - _WRITE_PROGRESS_START
        return _WRITE_PROGRESS_START + span * min(1.0, written / max(1, total_sections))

    async def _emit_section(draft: SectionDraft) -> None:
        await context.emit(
            "write.section",
            {
                "section": draft.section_key,
                "title": draft.title,
                "words": draft.word_count,
                "generator": draft.generator,
                # 序号是「正在做什么」的一半信息量：只报标题，用户无从判断还剩多少。
                "index": written,
                "total": total_sections,
            },
            stage=WRITE_STAGE,
            progress=_write_progress(),
        )

    drafts: dict[str, SectionDraft] = {}
    for section in body_sections:
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
        await _persist_draft(
            context,
            document_id=document_id,
            draft=draft,
            order_no=order_by_key.get(draft.section_key, len(drafts) - 1),
            whitelist=whitelist,
            language=language,
        )
        written += 1
        await _emit_section(draft)

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
        await _persist_draft(
            context,
            document_id=document_id,
            draft=draft,
            order_no=order_by_key.get(draft.section_key, len(drafts) - 1),
            whitelist=whitelist,
            language=language,
        )
        written += 1
        # 框架章节此前不发事件：摘要/引言/结论那几分钟前端完全没有进度可看。
        await _emit_section(draft)

    if coherence:
        await _polish_all(
            context,
            drafts=drafts,
            document_id=document_id,
            order_by_key=order_by_key,
            whitelist=whitelist,
            language=language,
            writing_context=writing_context,
            runner=runner,
            outcome=outcome,
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

    # 收尾再全量写一次：这一遍的 body_ir 来自跑过全篇白名单终检的 IR，
    # 顺带把 order_no 归一到最终顺序，覆盖掉写作途中按大纲下标存的那版。
    async with context.session() as session:
        for order_no, (section_key, draft) in enumerate(ordered):
            ir_section = next((s for s in ir.sections if s.key == section_key), None)
            await _upsert_draft(
                session,
                project_id=context.project_id,
                document_id=document_id,
                draft=draft,
                order_no=order_no,
                body_ir=ir_section.model_dump(mode="json") if ir_section else None,
                whitelist=whitelist,
            )

    # 摘要在正文之后生成，并可能经过连贯性润色；必须使用这里的最终版本，避免
    # 题名与关键词基于旧摘要。已有值视为用户编辑，自动流程只补空白，不覆盖。
    abstract_draft = drafts.get("abstract")
    abstract_text = (
        " ".join(paragraph.get("text", "") for paragraph in abstract_draft.paragraphs).strip()
        if abstract_draft is not None
        else ""
    )
    if abstract_text:
        async with context.session() as session:
            project = await get_project(session, context.project_id)
            needs_metadata = bool(
                project
                and (
                    not (project.publication_title or "").strip()
                    or not (project.keywords_json or [])
                )
            )
            scope = {**outline, **dict(project.scope_json or {})} if project else dict(outline)
            working_title = project.title if project else title
        if needs_metadata:
            generated = await generate_publication_metadata(
                abstract=abstract_text,
                project_title=working_title,
                scope=scope,
                language=language,
                runner=runner,
            )
            async with context.session() as session:
                project = await get_project(session, context.project_id)
                if project is not None:
                    title_added, keywords_added = await apply_generated_publication_metadata(
                        session,
                        project,
                        title=generated.title,
                        keywords=generated.keywords,
                    )
                else:
                    title_added = keywords_added = False
            outcome.publication_metadata_generator = generated.generator
            await context.emit(
                "publication_metadata.generated",
                {
                    "title_added": title_added,
                    "keywords_added": keywords_added,
                    "keyword_count": len(generated.keywords),
                    "generator": generated.generator,
                },
            )

    outcome.section_count = len(ordered)
    outcome.word_count = sum(draft.word_count for _key, draft in ordered)
    outcome.cite_key_count = len(ir.collect_cite_keys())
    outcome.citation_warning_count = sum(len(section.citation_warnings) for section in ir.sections)
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
        paper_type=paper_type,
        literature_evidence=[
            {
                "cite_key": key,
                "text": point.get("text") if isinstance(point, dict) else point,
                "located": bool(
                    isinstance(point, dict)
                    and (point.get("page") or point.get("section") or point.get("paragraph"))
                ),
            }
            for key, card in cards.items()
            if card.get("fulltext_used")
            for point in card.get("quotable_points") or []
        ],
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


async def _polish_all(
    context: JobContext,
    *,
    drafts: dict[str, SectionDraft],
    document_id: uuid.UUID,
    order_by_key: dict[str, int],
    whitelist: dict[str, uuid.UUID],
    language: str,
    writing_context: WritingContext,
    runner,
    outcome: WriteOutcome,
) -> None:
    """连贯性润色：独立阶段、逐节可见、随时可跳过。

    这一段动辄十几分钟，且此时所有章节都已「已生成」并落库——用户看到的是一屏
    完成的正文配一个不动的「分节写作」，只能理解为卡死。所以它有自己的阶段名，
    每节报一次进度，并且允许中途叫停：已润色的保留，剩下的直接用初稿。
    """
    polishable = [key for key, draft in drafts.items() if draft.generator.startswith("llm")]
    total = len(polishable)
    outcome.polish_pending_count = total
    if not total:
        return

    span = _WRITE_PROGRESS_END - _POLISH_PROGRESS_START
    await context.emit(
        "polish.started",
        {"total": total},
        stage=POLISH_STAGE,
        progress=_POLISH_PROGRESS_START,
    )

    done = 0
    for key in polishable:
        # 每节之间查一次开关：正在跑的那一节跑完再停，不打断已经付过钱的调用。
        if await _polish_skip_requested(context):
            outcome.polish_skipped = True
            await context.emit(
                "polish.skipped",
                {"done": done, "total": total, "remaining": total - done},
                stage=POLISH_STAGE,
                progress=_POLISH_PROGRESS_START + span * (done / total),
            )
            logger.info(
                "coherence polish skipped by user",
                extra={"done": done, "total": total},
            )
            break
        draft = drafts[key]
        polished = await coherence_pass(
            draft=draft,
            context=writing_context,
            whitelist=set(whitelist),
            runner=runner,
        )
        drafts[key] = polished
        await _persist_draft(
            context,
            document_id=document_id,
            draft=polished,
            order_no=order_by_key.get(key, 0),
            whitelist=whitelist,
            language=language,
        )
        done += 1
        outcome.polished_count = done
        await context.emit(
            "polish.section",
            {
                "section": key,
                "title": polished.title,
                "done": done,
                "total": total,
            },
            stage=POLISH_STAGE,
            progress=_POLISH_PROGRESS_START + span * (done / total),
        )

    outcome.polish_pending_count = total - done
    await context.emit(
        "polish.completed",
        {"done": done, "total": total, "skipped": outcome.polish_skipped},
        stage=POLISH_STAGE,
        progress=_WRITE_PROGRESS_END,
    )


async def _polish_skip_requested(context: JobContext) -> bool:
    """用户是否按下了「跳过润色」（标记写在 job.checkpoint_json 上）。"""
    if context.job_id is None:
        return False
    from db.models.paper import GenerationJob

    try:
        async with context.session() as session:
            return polish_skip_requested(await session.get(GenerationJob, context.job_id))
    except Exception:  # noqa: BLE001 - 读不到开关就当没按过，继续润色
        logger.warning("failed to read polish skip flag", exc_info=True)
        return False


async def _persist_draft(
    context: JobContext,
    *,
    document_id: uuid.UUID,
    draft: SectionDraft,
    order_no: int,
    whitelist: dict[str, uuid.UUID],
    language: str,
) -> None:
    """单节落库（写完一节存一节）。

    红线不因为「存得早」而放松：这里同样跑一次 typed-IR 白名单检查，
    数据库里任何时刻都不会出现白名单外的引用。
    """
    single = _build_paper_ir(title="", language=language, drafts=[(draft.section_key, draft)])
    single.enforce_cite_key_whitelist(set(whitelist), strip=True)
    ir_section = single.sections[0] if single.sections else None
    try:
        async with context.session() as session:
            await _upsert_draft(
                session,
                project_id=context.project_id,
                document_id=document_id,
                draft=draft,
                order_no=order_no,
                body_ir=ir_section.model_dump(mode="json") if ir_section else None,
                whitelist=whitelist,
            )
    except Exception as error:  # noqa: BLE001 - 中途落库失败不该毁掉整轮写作
        logger.warning(
            "incremental section persist failed",
            extra={"section": draft.section_key, "error": type(error).__name__},
        )


async def _upsert_draft(
    session,
    *,
    project_id: uuid.UUID,
    document_id: uuid.UUID,
    draft: SectionDraft,
    order_no: int,
    body_ir: dict[str, Any] | None,
    whitelist: dict[str, uuid.UUID],
) -> None:
    """章节 + 引用使用记录的落库动作，中途存与收尾存共用同一份实现。"""
    cite_keys = sorted({key for p in draft.paragraphs for key in p.get("cite_keys", [])})
    asset_refs: list[str] = []
    if body_ir:
        section_ir = IRSection(**body_ir)
        asset_refs = sorted(
            PaperIR(meta=PaperMeta(title=""), sections=[section_ir]).collect_asset_refs()
        )
    row = await upsert_section(
        session,
        document_id=document_id,
        section_key=draft.section_key,
        title=draft.title,
        order_no=order_no,
        body_ir=body_ir,
        cite_keys=cite_keys,
        asset_refs=asset_refs,
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
        project_id=project_id,
        section_id=row.id,
        usages=usages,
    )


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
            "quotable_points": (card.quotable_points_json if card else None) or [],
            "fulltext_used": bool(card and card.fulltext_used),
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
