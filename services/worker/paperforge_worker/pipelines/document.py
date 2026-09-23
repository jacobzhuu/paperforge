"""文稿装配：大纲 → 分节写作 → PaperIR → 持久化 → Markdown 预览。

这里是 R2 的最后一道闸：所有章节汇成 PaperIR 后，再跑一次 typed-IR 白名单检查
（``PaperIR.enforce_cite_key_whitelist``），任何漏网的 cite key 在此被 strip
并写入 ``citation_warnings``——渲染器永远拿不到白名单外的引用。

Draft-first：单章失败只影响该章（留占位 + 告警），其余章节照常交付。
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from typing import Any

from db import (
    WRITE_DOCUMENT_KEY,
    apply_generated_publication_metadata,
    create_document,
    get_cards,
    get_outline,
    get_project,
    get_writing_whitelist,
    grounded_asset_payloads,
    latest_document,
    latest_outline,
    list_citation_usage,
    list_entries,
    list_sections,
    polish_skip_requested,
    reference_metadata_payload,
    replace_citation_usage,
    replace_sentence_downgrades,
    upsert_section,
)
from db.models.paper import Outline, PaperDocument, PaperSection
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
from sqlalchemy import select

from paperforge_worker.concurrency import bounded_map
from paperforge_worker.context import JobContext, JobStopped
from paperforge_worker.orchestration.tracing import traced
from paperforge_worker.orchestration.writing_graph import (
    WRITING_VERSION,
    descendants,
    fingerprint,
    is_frame,
    layers,
    merge_terms,
    section_graph,
)
from paperforge_worker.pipelines.publication_metadata import generate_publication_metadata
from paperforge_worker.pipelines.writing import (
    SectionDefect,
    SectionDraft,
    WritingContext,
    coherence_pass,
    count_words,
    inspect_section_draft,
    repair_unsourced_numbers,
    section_evidence_for,
    sentence_downgrade_events,
    write_section,
    writer_policy,
)

logger = get_logger(__name__)

# 摘要/引言/结论在正文写完后生成（设计 §4.4.1）。
FRAME_ORDER = {"abstract": -2, "introduction": -1, "conclusion": 999}

# write 阶段在整条管线里占的进度区间（与 worker._STAGE_PROGRESS 对齐）。
# 分节写作与连贯性润色各占一段：润色是独立阶段名（polish），界面上不能再叫「分节写作」——
# 那会让人对着一屏「已生成」的章节以为系统卡死了。
_WRITE_PROGRESS_START = 0.65
_POLISH_PROGRESS_START = 0.78
_POLISH_PROGRESS_END = 0.85
_WRITE_PROGRESS_END = 0.90

WRITE_STAGE = "write"
POLISH_STAGE = "polish"

# 一节最多写几次（含首轮）。三次覆盖了实测的全部可恢复失败：截断、整段照抄、
# 中途切语种。再往上加只是让一个真正写不出来的章节多烧两次钱。
MAX_SECTION_ATTEMPTS = 3
# 交付前的补写扫描最多再救几节。这是兜底不是主力：主力是 _write_one 里的阶梯，
# 到这一步还没成的通常是证据本身有问题，重试收益递减。
MAX_RECOVERY_SECTIONS = 4


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
    #: 重试之后才写成的章节——这些是恢复回路真正救回来的，成本上也应看得见。
    recovered_sections: list[dict[str, Any]] = field(default_factory=list)
    #: 用尽重试仍然没有成熟正文的章节。非空就意味着这篇稿子**不完整**。
    incomplete_sections: list[dict[str, Any]] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        """这篇稿子能不能算写完了。

        以前的判据是 ``section_count > 0``——只要有章节行就算交付，哪怕每一节都是
        降级占位。完整性必须看正文本身。
        """
        return self.section_count > 0 and not self.incomplete_sections

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
            "recovered_sections": self.recovered_sections,
            "incomplete_sections": self.incomplete_sections,
            "complete": self.complete,
        }


@traced("coordinator", "write_document")
async def write_document(
    context: JobContext,
    *,
    language: str = "en",
    title: str = "",
    coherence: bool = True,
    paper_type: str = "review",
    outline_id: uuid.UUID | str | None = None,
) -> WriteOutcome:
    """按大纲逐章写作并落库，返回统计。"""
    outcome = WriteOutcome()
    runner = context.llm_runner()
    outline_id = outline_id or context.checkpoint.get("writer_outline_id")

    async with context.session() as session:
        if outline_id is not None:
            try:
                requested_outline_id = uuid.UUID(str(outline_id))
            except ValueError:
                requested_outline_id = None
            outline_row = (
                await get_outline(session, requested_outline_id)
                if requested_outline_id is not None
                else None
            )
            if outline_row is not None and outline_row.project_id != context.project_id:
                outline_row = None
        else:
            outline_row = await latest_outline(session, context.project_id)
        if outline_row is None:
            outcome.warnings.append(
                {
                    "stage": "write",
                    "reason": "outline_not_found" if outline_id is not None else "no_outline",
                }
            )
            return outcome
        outline = dict(outline_row.tree_json or {})
        if (context.checkpoint.get("writer_outline_hash") and
                fingerprint(outline) != context.checkpoint["writer_outline_hash"]):
            raise JobStopped("pause", reason="writing_inputs_changed")
        if (outline.get("dependency_contract", {}).get("requires_confirmation", False)
                and outline_row.status != "confirmed"):
            raise JobStopped("pause", reason="outline_confirmation_required")
        whitelist = await get_writing_whitelist(session, context.project_id)
        cards = await _card_context(session, context.project_id)
        assets = await grounded_asset_payloads(session, context.project_id)
        outline_id = outline_row.id

    sections = [s for s in (outline.get("sections") or []) if isinstance(s, dict)]
    if not sections:
        outcome.warnings.append({"stage": "write", "reason": "empty_outline"})
        return outcome

    writing_context = WritingContext(outline=outline, language=language, paper_type=paper_type)
    body_sections = [s for s in sections if not is_frame(s)]
    frame_sections = [s for s in sections if is_frame(s)]
    frame_waves = _frame_waves(sections)  # Validate before any paid generation.
    order_by_key = {str(s.get("key") or ""): index for index, s in enumerate(sections)}

    # 建档提前到写作之前。write 是全管线最长的一段（本地实测 7 节约 18 分钟），
    # 而此前所有章节只在末尾一次性 upsert：中途撞上 arq job_timeout 或 worker 重启，
    # 已经写完的章节连同那十几分钟的 LLM 调用一起蒸发，重跑只能从头再写一遍。
    # 现在写完一节存一节，末尾那次全量 upsert 仍然保留（它带全篇白名单终检），
    # 只是不再是唯一的落库时机。
    #
    # 续跑先认上一轮的 document：用户在第 6 节按了暂停，「继续」必须从第 7 节接着写，
    # 否则新建 document 从头重写，暂停就等于没暂停。
    document_id, resumed = await _resume_document(context)
    mode = str(
        context.checkpoint.get("writer_execution_mode")
        or ("legacy" if document_id is not None else context.settings.writer_execution_mode)
    )
    frame_width = int(
        context.checkpoint.get("writer_frame_concurrency")
        or (
            1
            if document_id is not None or mode != "dag_parallel"
            else context.settings.writer_frame_concurrency
        )
    )
    if "writer_polish_policy" not in context.checkpoint:
        await context.emit("polish.policy", {}, checkpoint={
            "writer_polish_policy": "legacy" if document_id is not None else
                context.settings.writer_polish_policy,
            "writer_polish_concurrency": context.settings.writer_polish_concurrency,
        })
    if document_id is None:
        async with context.session() as session:
            document = await create_document(
                session,
                project_id=context.project_id,
                outline_id=outline_id,
            )
            document_id = document.id
            document.writing_state_json = {
                "version": WRITING_VERSION,
                "mode": mode,
                "project_id": str(context.project_id),
                "document_id": str(document_id),
                "outline_id": str(outline_id),
                "nodes": {},
                "glossary": dict(outline.get("glossary") or {}),
                "source_hash": fingerprint(
                    {"outline": outline, "cards": cards, "whitelist": whitelist, "assets": assets}
                ),
            }
        # 单独落一次 checkpoint：write 阶段跑完才有阶段 checkpoint，
        # 而暂停恰恰发生在「没跑完」的时候。update_job 是合并语义，安全。
        await context.emit(
            "write.document",
            {"document_id": str(document_id)},
            stage=WRITE_STAGE,
            checkpoint={
                WRITE_DOCUMENT_KEY: str(document_id),
                "writer_execution_mode": mode,
                "writer_frame_concurrency": frame_width,
            },
        )
    outcome.document_id = str(document_id)

    # 分节写作占 0.65→0.82，润色占 0.82→0.90：两段各自线性摊，
    # 用户看到的百分比因此和「写到第几节 / 润到第几节」对得上。
    written = len(resumed)
    total_sections = len(sections)
    frames_started = False

    def _write_progress() -> float:
        if frames_started:
            fraction = (written - len(body_sections)) / max(1, len(frame_sections))
            return _POLISH_PROGRESS_END + (_WRITE_PROGRESS_END - _POLISH_PROGRESS_END) * min(
                1.0, max(0.0, fraction)
            )
        span = _POLISH_PROGRESS_START - _WRITE_PROGRESS_START
        return _WRITE_PROGRESS_START + span * min(1.0, written / max(1, len(body_sections)))

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

    # 续跑：已落库的章节直接进 drafts，并注册进滚动摘要——不注册的话后续章节
    # 拿不到前文上下文，接着写出来的部分会和前面脱节。
    drafts: dict[str, SectionDraft] = {}
    for section in sections:
        key = str(section.get("key") or "")
        if key in resumed:
            drafts[key] = resumed[key]
            writing_context.register(key, resumed[key])

    if mode in {"dag_serial", "dag_parallel"}:
        await _write_body_graph(
            context,
            document_id=document_id,
            sections=sections,
            drafts=drafts,
            writing_context=writing_context,
            cards=cards,
            whitelist=whitelist,
            assets=assets,
            outcome=outcome,
            mode=mode,
        )
        resumed.update(drafts)
        written = len(drafts)

    for section in body_sections:
        if str(section.get("key") or "") in resumed:
            continue
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
        # 落库 + 发完事件才查停止开关：这一节的 LLM 花费已经变成库里的正文，
        # 停在这里不浪费任何东西。write 阶段本身实测 18 分钟，只在阶段边界查
        # 等于用户点了取消要干等十几分钟。
        await context.raise_if_stopped()

    # Recover and integrate BODY first. Frames must summarize the accepted body,
    # never an earlier pre-repair/pre-polish snapshot.
    await _recover_incomplete_sections(
        context,
        sections=body_sections,
        drafts=drafts,
        document_id=document_id,
        order_by_key=order_by_key,
        cards=cards,
        whitelist=whitelist,
        language=language,
        writing_context=writing_context,
        runner=runner,
        outcome=outcome,
        assets=assets,
    )
    if coherence:
        body_drafts = {s["key"]: drafts[s["key"]] for s in body_sections if s["key"] in drafts}
        await _integrate_body(context, body_drafts, writing_context)
        await _polish_all(
            context,
            drafts=body_drafts,
            document_id=document_id,
            order_by_key=order_by_key,
            whitelist=whitelist,
            language=language,
            writing_context=writing_context,
            runner=runner,
            outcome=outcome,
        )
        drafts.update(body_drafts)
        for key, draft in body_drafts.items():
            writing_context.register(key, draft)

    # Frame input identity includes ALL accepted body sections, not the preceding window.
    body_hash = fingerprint(
        {s["key"]: drafts[s["key"]].expected_body_hash for s in body_sections if s["key"] in drafts}
    )
    frames_started = True

    async def frame_emitted(draft):
        nonlocal written
        written += 1
        await _emit_section(draft)

    await _write_frames(
        context,
        frame_sections=frame_sections,
        waves=frame_waves,
        width=frame_width,
        drafts=drafts,
        resumed=resumed,
        body_hash=body_hash,
        body_keys=[str(s["key"]) for s in body_sections],
        document_id=document_id,
        order_by_key=order_by_key,
        cards=cards,
        whitelist=whitelist,
        writing_context=writing_context,
        runner=runner,
        outcome=outcome,
        assets=assets,
        on_committed=frame_emitted,
    )

    # 交付前的补写：润色之前先把没写成的章节救回来。放在润色之前是因为润色只
    # 处理已有正文，先润后补等于新补的那几节永远没被打磨过。
    await _recover_incomplete_sections(
        context,
        sections=frame_sections,
        drafts=drafts,
        document_id=document_id,
        order_by_key=order_by_key,
        cards=cards,
        whitelist=whitelist,
        language=language,
        writing_context=writing_context,
        runner=runner,
        outcome=outcome,
        assets=assets,
    )

    # Frames are not globally rewritten after their snapshot has been fixed.

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
                job_id=context.job_id,
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
    report = _numlint(ordered, assets=assets, paper_type=paper_type, cards=cards)

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
    await context.emit(
        "write.assembled", outcome.to_payload(), stage=WRITE_STAGE, progress=_WRITE_PROGRESS_END
    )
    await context.raise_if_stopped()
    return outcome


def _frame_waves(sections: list[dict]) -> list[list[str]]:
    """Only known independent frames fan out; custom frames retain serial barriers."""
    keys = [str(s.get("key") or "") for s in sections]
    if len(set(keys)) != len(keys) or not all(keys):
        raise ValueError("writing sections require unique keys")
    frames = [s for s in sections if is_frame(s)]
    frame_keys = [str(s["key"]) for s in frames]
    graph = {}
    for s in frames:
        key = str(s["key"])
        deps = s.get("depends_on", [])
        if not isinstance(deps, list) or any(
            not isinstance(d, str) or d not in keys or d == key for d in deps
        ):
            raise ValueError(f"invalid frame dependencies: {key}")
        graph[key] = [d for d in deps if d in frame_keys]
    waves = layers(graph)
    if any(k not in FRAME_ORDER for k in frame_keys):
        return [[k] for wave in waves for k in wave]
    return waves


async def _write_frames(
    context,
    *,
    frame_sections,
    waves,
    width,
    drafts,
    resumed,
    body_hash,
    body_keys,
    document_id,
    order_by_key,
    cards,
    whitelist,
    writing_context,
    runner,
    outcome,
    assets,
    on_committed,
):
    if not frame_sections:
        return
    frozen = deepcopy(writing_context)
    by_key = {str(s["key"]): s for s in frame_sections}
    source_hash = fingerprint(
        {"outline": frozen.outline, "cards": cards, "whitelist": whitelist, "assets": assets}
    )
    saved = context.checkpoint.get("frame_context") or {}
    if saved.get("body_hash") == body_hash and saved.get("source_hash") == source_hash:
        frozen = WritingContext(
            outline=deepcopy(writing_context.outline), **deepcopy(saved["context"])
        )
    else:
        await context.emit(
            "frame.context_frozen",
            {
                "body_snapshot": body_hash,
                "source_hash": source_hash,
            },
            checkpoint={
                "frame_context": {
                    "body_hash": body_hash,
                    "source_hash": source_hash,
                    "context": {k: v for k, v in asdict(frozen).items() if k != "outline"},
                }
            },
        )
    for wave in waves:
        ready = [
            k
            for k in wave
            if not (
                k in resumed
                and drafts.get(k)
                and drafts[k].generation.get("body_snapshot") == body_hash
                and drafts[k].generation.get("source_snapshot", source_hash) == source_hash
            )
        ]
        queued_at = time.monotonic()

        async def generate(key, queued_at=queued_at):
            await context.raise_if_stopped()
            started = time.monotonic()
            events, warnings = [], []

            async def emit(*args, **kwargs):
                events.append((args, kwargs))

            section = {
                **by_key[key],
                "summary": _frame_goal(by_key[key], frozen.outline, frozen.language),
            }
            identity = fingerprint(
                {
                    "section": section,
                    "context": asdict(frozen),
                    "source_hash": source_hash,
                    "body_snapshot": body_hash,
                    "writer_policy": writer_policy(frozen.language, frozen.paper_type),
                    "model": context.settings.llm_config().model_for_role("writer"),
                }
            )
            local = WriteOutcome()
            async with context.span(
                "subagent",
                "frame",
                node_id=key,
                input_hash=identity,
                body_snapshot=body_hash,
                admission_wait_ms=(started - queued_at) * 1000,
            ):
                draft = await _write_one(
                    section=section,
                    cards=cards,
                    whitelist=set(whitelist),
                    writing_context=deepcopy(frozen),
                    runner=runner,
                    context=context,
                    outcome=local,
                    assets=assets,
                    emit=emit,
                    warn=lambda *args: warnings.append(args),
                )
            draft.generation.update(
                {"body_snapshot": body_hash, "input_hash": identity, "source_snapshot": source_hash}
            )
            return draft, local, events, warnings, time.monotonic()

        failures = []

        async def commit(_index, key, result, failures=failures):
            if isinstance(result, BaseException):
                failures.append(key)
                await context.emit("frame.failed", {"section": key, "error": type(result).__name__})
                return
            draft, local, events, warnings, finished = result
            previous = drafts.get(key)
            draft.expected_body_hash = previous.expected_body_hash if previous else None
            commit_start = time.monotonic()
            await _persist_draft(
                context,
                document_id=document_id,
                draft=draft,
                order_no=order_by_key[key],
                whitelist=whitelist,
                language=frozen.language,
                frame_guard={
                    "body_hash": body_hash,
                    "body_keys": body_keys,
                    "source_hash": source_hash,
                },
            )
            drafts[key] = draft
            outcome.warnings.extend(local.warnings)
            outcome.recovered_sections.extend(local.recovered_sections)
            for args in warnings:
                context.warn(*args)
            for args, kwargs in events:
                await context.emit(*args, **kwargs)
            await context.emit(
                "frame.committed",
                {
                    "section": key,
                    "body_snapshot": body_hash,
                    "input_hash": draft.generation["input_hash"],
                    "ordered_commit_wait_ms": (commit_start - finished) * 1000,
                    "persist_ms": (time.monotonic() - commit_start) * 1000,
                },
            )
            await on_committed(draft)

        await bounded_map(
            ready, generate, limit=width, on_ready=commit, stop_check=context.raise_if_stopped
        )
        if failures:
            raise JobStopped("pause", reason="frame_generation_failed")


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
    # 续跑的章节（generator="resumed"）也要润色：暂停发生在写作途中时，先写好的
    # 那几节还没轮到润色，排除它们等于让「继续」出来的稿子前半段永远没被打磨。
    # 上一轮已经润过的靠 checkpoint 记名单排除，不重复付钱。
    if context.checkpoint.get("writer_polish_policy", "legacy") != "legacy":
        from paperforge_worker.pipelines.parallel_polish import polish

        return await polish(context, drafts=drafts, document_id=document_id,
                            order_by_key=order_by_key, whitelist=whitelist, language=language,
                            writing_context=writing_context, runner=runner, outcome=outcome)
    polished_before = {str(key) for key in (context.checkpoint.get(WRITE_POLISHED_KEY) or [])}
    polishable = [
        key
        for key, draft in drafts.items()
        if (draft.generator.startswith("llm") or draft.generator == "resumed")
        and key not in polished_before
    ]
    total = len(polishable)
    outcome.polish_pending_count = total
    if not total:
        return

    span = _POLISH_PROGRESS_END - _POLISH_PROGRESS_START
    await context.emit(
        "polish.started",
        {"total": total},
        stage=POLISH_STAGE,
        progress=_POLISH_PROGRESS_START,
    )

    done = 0
    polish_results = {"accepted": 0, "changed": 0, "rejected": 0}
    for key in polishable:
        # 取消/暂停优先于「跳过润色」：跳过是「别润了，直接交稿」，
        # 停止是「整个任务先别跑了」，两者的收尾完全不同。
        await context.raise_if_stopped()
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
        polish_started = time.monotonic()
        polished = await coherence_pass(
            draft=draft,
            context=writing_context,
            whitelist=set(whitelist),
            runner=runner,
        )
        result = polished.generation.get("polish_result")
        if result:
            polish_results["accepted" if result["accepted"] else "rejected"] += 1
            polish_results["changed"] += int(result["changed"])
        drafts[key] = polished
        writing_context.register(key, polished)
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
        polished_before.add(key)
        await context.emit(
            "polish.section",
            {
                "section": key,
                "title": polished.title,
                "result": polished.generation.get("polish_result"),
                "elapsed_ms": (time.monotonic() - polish_started) * 1000,
                "done": done,
                "total": total,
            },
            stage=POLISH_STAGE,
            progress=_POLISH_PROGRESS_START + span * (done / total),
            # 逐节记名单：在润色途中暂停后「继续」，已润过的这些不再重跑。
            checkpoint={WRITE_POLISHED_KEY: sorted(polished_before)},
        )

    outcome.polish_pending_count = total - done
    await context.emit(
        "polish.completed",
        {
            "done": done,
            "total": total,
            "skipped": outcome.polish_skipped,
            "results": polish_results,
        },
        stage=POLISH_STAGE,
        progress=_POLISH_PROGRESS_END,
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


#: 已润色完成的章节，续跑时不再重复润色（每节一次 LLM 调用）。
WRITE_POLISHED_KEY = "write_polished_keys"


def _draft_from_section(section: PaperSection) -> SectionDraft:
    """把已落库的章节还原成 SectionDraft，供续跑时跳过重写。

    只还原 text + cite_keys 两样，但这两样必须准：收尾的全量 upsert 会拿
    `draft.to_ir_section()` 重建 body_ir，而它只在 paragraph 带 `sentences` 时才发
    CiteRun——还原成纯文本段落会把续跑章节的引用**全部抹掉**。
    新稿的术语与生成版本从 generation_json 恢复；旧稿保留兼容降级。
    """
    paragraphs: list[dict[str, Any]] = []
    for block in (section.body_ir_json or {}).get("blocks") or []:
        if not isinstance(block, dict) or block.get("type") != "paragraph":
            continue
        sentences: list[dict[str, Any]] = []
        pending = ""
        for run in block.get("runs") or []:
            if not isinstance(run, dict):
                continue
            if run.get("t") == "text":
                pending += str(run.get("v") or "")
            elif run.get("t") == "cite":
                keys = [str(key) for key in (run.get("keys") or []) if key]
                evidence_ids = [str(value) for value in (run.get("evidence_ids") or []) if value]
                sentences.append(
                    {
                        "text": pending.strip(),
                        "cite_keys": keys,
                        "evidence_ids": evidence_ids,
                    }
                )
                pending = ""
            elif run.get("t") == "grounding":
                if pending.strip():
                    sentences.append({"text": pending.strip(), "cite_keys": [], "evidence_ids": []})
                    pending = ""
                if sentences:
                    sentences[-1]["source_refs"] = [
                        str(value) for value in (run.get("source_refs") or []) if value
                    ]
        if pending.strip():
            sentences.append({"text": pending.strip(), "cite_keys": []})
        if not sentences:
            continue
        paragraphs.append(
            {
                "text": " ".join(item["text"] for item in sentences).strip(),
                "sentences": sentences,
                "cite_keys": [key for item in sentences for key in item["cite_keys"]],
                "stance_summary": block.get("stance_summary"),
            }
        )
    return SectionDraft(
        section_key=section.section_key,
        title=section.title,
        paragraphs=paragraphs,
        model=section.model,
        generator=("resumed" if section.status != "needs_rewrite" else "failed"),
        terms=dict((section.generation_json or {}).get("terms") or {}),
        generation=dict(section.generation_json or {}),
        expected_body_hash=fingerprint(section.body_ir_json),
        level=int((section.body_ir_json or {}).get("level") or 1),
        parent_key=section.parent_key,
        inline_tables=list((section.generation_json or {}).get("inline_tables") or []),
    )


async def _resume_document(
    context: JobContext,
) -> tuple[uuid.UUID | None, dict[str, SectionDraft]]:
    """上一轮留下的 document 及其已落库章节；没有可续的就返回 (None, {})。"""
    raw = context.checkpoint.get(WRITE_DOCUMENT_KEY)
    if not raw:
        return None, {}
    try:
        document_id = uuid.UUID(str(raw))
    except ValueError:
        return None, {}
    try:
        async with context.session() as session:
            document = await session.get(PaperDocument, document_id)
            if document is None or document.project_id != context.project_id:
                return None, {}
            rows = await list_sections(session, document_id)
    except Exception:  # noqa: BLE001 - 读不出上一轮的稿子就当没有，从头写
        logger.warning("failed to load resumable document", exc_info=True)
        return None, {}
    return document_id, {row.section_key: _draft_from_section(row) for row in rows}


async def _persist_draft(
    context: JobContext,
    *,
    document_id: uuid.UUID,
    draft: SectionDraft,
    order_no: int,
    whitelist: dict[str, uuid.UUID],
    language: str,
    graph_state: dict | None = None,
    frame_guard: dict | None = None,
    polish_guard: dict | None = None,
) -> None:
    """单节落库（写完一节存一节）。

    红线不因为「存得早」而放松：这里同样跑一次 typed-IR 白名单检查，
    数据库里任何时刻都不会出现白名单外的引用。
    """
    single = _build_paper_ir(title="", language=language, drafts=[(draft.section_key, draft)])
    single.enforce_cite_key_whitelist(set(whitelist), strip=True)
    ir_section = single.sections[0] if single.sections else None
    async with context.session() as session:
        if any(g is not None for g in (graph_state, frame_guard, polish_guard)):
            guard = next(g for g in (graph_state, frame_guard, polish_guard) if g is not None)
            doc = await session.get(PaperDocument, document_id)
            source_outline = await session.scalar(
                select(Outline).where(Outline.id == doc.outline_id).with_for_update()
            )
            current_sources = {
                "outline": source_outline.tree_json if source_outline else None,
                "cards": await _card_context(session, context.project_id),
                "whitelist": await get_writing_whitelist(session, context.project_id),
                "assets": await grounded_asset_payloads(session, context.project_id),
            }
            if fingerprint(current_sources) != guard["source_hash"]:
                raise JobStopped("pause", reason="writing_inputs_changed")
        if frame_guard is not None:
            await session.scalar(
                select(PaperDocument).where(PaperDocument.id == document_id).with_for_update()
            )
            rows = await list_sections(session, document_id)
            current = {
                r.section_key: fingerprint(r.body_ir_json)
                for r in rows
                if r.section_key in frame_guard["body_keys"]
            }
            if fingerprint(current) != frame_guard["body_hash"]:
                raise JobStopped("pause", reason="writing_inputs_changed")
        if polish_guard is not None:
            await session.scalar(select(PaperDocument).where(
                PaperDocument.id == document_id).with_for_update())
            rows = await list_sections(session, document_id)
            current = {r.section_key: fingerprint(r.body_ir_json) for r in rows
                       if r.section_key in polish_guard["expected"]}
            if current != polish_guard["expected"]:
                raise JobStopped("pause", reason="polish_inputs_changed")
        await _upsert_draft(
            session,
            project_id=context.project_id,
            document_id=document_id,
            draft=draft,
            order_no=order_no,
            body_ir=ir_section.model_dump(mode="json") if ir_section else None,
            whitelist=whitelist,
            job_id=context.job_id,
        )
        if polish_guard is not None:
            polish_guard["expected"][draft.section_key] = draft.expected_body_hash
            polish_guard["results"][draft.section_key] = {
                "rewrite": draft.generation["polish_decision"]["rewrite"],
                "accepted": draft.generation["polish_result"]["accepted"],
                "output_hash": draft.expected_body_hash,
            }
            document = await session.get(PaperDocument, document_id)
            saved_state = deepcopy(document.writing_state_json or {})
            saved_state["polish"] = deepcopy(polish_guard)
            document.writing_state_json = saved_state
        if graph_state is not None:
            document = await session.get(PaperDocument, document_id)
            graph_state["nodes"][draft.section_key]["output_hash"] = draft.expected_body_hash
            document.writing_state_json = deepcopy(graph_state)


# 这几个 generator 的含义是「这一节没有正文」：模型没给出可用输出，降级只留下一句说明。
# 必须和 `generated` 区分开落库，质量门才报得出 section_not_generated，编辑器才标得出来。
# `evidence_gap_skeleton` 不在此列——那是证据不足，已由 placeholders_present 覆盖，
# 作者要做的是补来源而不是重跑这一节。
_NOT_GENERATED_GENERATORS = frozenset({"deterministic", "deterministic_fallback", "failed"})


async def _upsert_draft(
    session,
    *,
    project_id: uuid.UUID,
    document_id: uuid.UUID,
    draft: SectionDraft,
    order_no: int,
    body_ir: dict[str, Any] | None,
    whitelist: dict[str, uuid.UUID],
    job_id: uuid.UUID | None = None,
) -> None:
    """章节 + 引用使用记录的落库动作，中途存与收尾存共用同一份实现。"""
    cite_keys = sorted({key for p in draft.paragraphs for key in p.get("cite_keys", [])})
    asset_refs: list[str] = []
    if body_ir:
        section_ir = IRSection(**body_ir)
        asset_refs = sorted(
            PaperIR(meta=PaperMeta(title=""), sections=[section_ir]).collect_asset_refs()
        )
    document = await session.scalar(
        select(PaperDocument).where(PaperDocument.id == document_id).with_for_update()
    )
    if document is None or document.project_id != project_id:
        raise JobStopped("pause", reason="writing_inputs_changed")
    previous = await session.scalar(
        select(PaperSection)
        .where(
            PaperSection.document_id == document_id, PaperSection.section_key == draft.section_key
        )
        .with_for_update()
    )
    if fingerprint(previous.body_ir_json if previous else None) != (
        draft.expected_body_hash or fingerprint(None)
    ):
        raise JobStopped("pause", reason="writing_inputs_changed")
    row = await upsert_section(
        session,
        document_id=document_id,
        section_key=draft.section_key,
        title=draft.title,
        order_no=order_no,
        parent_key=draft.parent_key,
        body_ir=body_ir,
        cite_keys=cite_keys,
        asset_refs=asset_refs,
        status=(
            previous.status
            if previous and fingerprint(body_ir) == fingerprint(previous.body_ir_json)
            else "needs_rewrite"
            if draft.generator in _NOT_GENERATED_GENERATORS
            else "generated"
        ),
        model=draft.model,
    )
    draft.generation.update(
        {
            "version": WRITING_VERSION,
            "terms": dict(draft.terms),
            "inline_tables": draft.inline_tables,
        }
    )
    row.generation_json = deepcopy(draft.generation)
    draft.expected_body_hash = fingerprint(body_ir)
    if document.writing_state_json:
        state = deepcopy(document.writing_state_json)
        node = state.get("nodes", {}).get(draft.section_key)
        if node:
            node["output_hash"] = draft.expected_body_hash
            document.writing_state_json = state
    usages = [
        {
            "work_id": whitelist[key],
            "cite_key": key,
            "context_snippet": _snippet(draft, key),
        }
        for key in cite_keys
        if key in whitelist
    ]
    # 规则拿掉的句子与正文同一事务落库。`to_ir_section()` 只读 `sentences`，所以
    # 线索必须从**草稿**上取——这正是它此前消失的那一步。整节替换：修复轮重写章节
    # 时，上一轮的删除记录跟着旧正文一起走，计数不会随轮次累加。
    await replace_sentence_downgrades(
        session,
        project_id=project_id,
        job_id=job_id,
        document_id=document_id,
        section_id=row.id,
        section_key=draft.section_key,
        events=sentence_downgrade_events(draft),
    )
    await replace_citation_usage(
        session,
        project_id=project_id,
        section_id=row.id,
        usages=usages,
    )


async def _integrate_body(context, drafts, writing_context) -> None:
    """Use existing coherence editor with full-body findings and deterministic term ownership."""
    glossary, conflicts = merge_terms(
        dict(writing_context.outline.get("glossary") or {}),
        [(key, draft.terms) for key, draft in drafts.items()],
    )
    writing_context.glossary = glossary
    seen: dict[str, str] = {}
    duplicates = []
    for key, draft in drafts.items():
        for paragraph in draft.paragraphs:
            text = str(paragraph.get("text") or "").strip()
            if len(text) > 80 and text in seen:
                duplicates.append({"section": key, "other_section": seen[text]})
            seen[text] = key
    from paperforge_worker.orchestration.writing_graph import findings_snapshot

    writing_context.integration_notes = (
        "Check cross-section contradictions and repeated arguments against these findings; "
        "do not change factual claims or sentence provenance.\n"
        + findings_snapshot(
            writing_context.outline.get("sections", []), writing_context.rolling_summaries
        )
        + f"\nTerm conflicts: {conflicts}\nDuplicate paragraphs: {duplicates}"
    )
    await context.emit(
        "integration.findings",
        {
            "term_conflicts": conflicts,
            "duplicate_paragraphs": duplicates,
            "body_snapshot": fingerprint({k: d.expected_body_hash for k, d in drafts.items()}),
        },
    )


async def _write_body_graph(
    context,
    *,
    document_id,
    sections,
    drafts,
    writing_context,
    cards,
    whitelist,
    assets,
    outcome,
    mode,
) -> None:
    graph = section_graph(sections)
    bodies = {str(s["key"]): s for s in sections if not is_frame(s)}
    body_graph = {key: deps for key, deps in graph.items() if key in bodies}
    order = {str(s["key"]): index for index, s in enumerate(sections)}
    async with context.session() as session:
        document = await session.get(PaperDocument, document_id)
        state = deepcopy(document.writing_state_json or {})
    if state and state.get("version") != WRITING_VERSION:
        raise ValueError("unsupported writing state version")
    if not state:
        state = {
            "version": WRITING_VERSION,
            "mode": mode,
            "graph": graph,
            "nodes": {},
            "project_id": str(context.project_id),
            "document_id": str(document_id),
            "glossary": dict(writing_context.outline.get("glossary") or {}),
        }
    if state["project_id"] != str(context.project_id) or state["document_id"] != str(document_id):
        raise ValueError("writing state scope mismatch")
    edited = [
        key
        for key, draft in drafts.items()
        if key in state["nodes"]
        and state["nodes"][key].get("output_hash", state["nodes"][key].get("base_output_hash"))
        != draft.expected_body_hash
    ]
    if edited:
        await context.emit(
            "writing_dag.input_conflict", {"sections": edited, "reason": "document_changed"}
        )
        raise JobStopped("pause", reason="writing_inputs_changed")
    state["source_hash"] = fingerprint(
        {
            "outline": writing_context.outline,
            "cards": cards,
            "whitelist": whitelist,
            "assets": assets,
        }
    )
    # Exclude unrelated evidence from each node identity; include every value its prompt reads.
    inputs = {
        key: fingerprint(
            {
                "version": WRITING_VERSION,
                "section": section,
                "evidence": section_evidence_for(section, writing_context.outline),
                "cards": {k: cards.get(k) for k in section.get("cite_keys", [])},
                "whitelist": sorted(set(section.get("cite_keys", [])) & set(whitelist)),
                "assets": assets,
                "glossary": state["glossary"],
                "language": writing_context.language,
                "paper_type": writing_context.paper_type,
                "outline_titles": [s.get("title") for s in sections],
                "topic": writing_context.outline.get("topic"),
                "question": writing_context.outline.get("research_question"),
                "model": context.settings.llm_config().model_for_role("writer"),
                "policy": writer_policy(writing_context.language, writing_context.paper_type),
                "provider": context.settings.llm_config().provider,
                "thinking": context.settings.llm_role_thinking,
                "retry": context.settings.llm_role_retry,
                "dependencies": body_graph[key],
            }
        )
        for key, section in bodies.items()
    }
    changed = {
        key
        for key in bodies
        if state["nodes"].get(key, {}).get("input_hash") != inputs[key]
        or (
            key in drafts
            and state["nodes"].get(key, {}).get("output_hash") != drafts[key].expected_body_hash
        )
    }
    invalid = descendants(graph, changed)
    if invalid:
        await context.emit(
            "writing_dag.invalidated",
            {"sections": sorted(invalid)},
            checkpoint={
                WRITE_POLISHED_KEY: [
                    k for k in context.checkpoint.get(WRITE_POLISHED_KEY, []) if k not in invalid
                ],
            },
        )
    for key in invalid:
        state["nodes"].pop(key, None)
    state["graph"] = graph
    for key in invalid:
        if key in drafts and key not in bodies:
            drafts[key].generation.pop("body_snapshot", None)
    # Persist the frozen context and interrupted identities before issuing calls.
    async with context.session() as session:
        document = await session.scalar(
            select(PaperDocument).where(PaperDocument.id == document_id).with_for_update()
        )
        document.writing_state_json = deepcopy(state)

    for wave in layers(body_graph):
        ready = [
            key
            for key in wave
            if not (state["nodes"].get(key, {}).get("status") == "completed" and key in drafts)
        ]
        if not ready:
            continue
        # A failed dependency blocks downstream generation rather than inventing its findings.
        blocked = [
            key
            for key in ready
            if any(
                state["nodes"].get(dep, {}).get("status") != "completed" for dep in body_graph[key]
            )
        ]
        if blocked:
            await context.emit("writing_dag.blocked", {"sections": blocked})
            raise JobStopped("pause", reason="writing_dependency_failed")
        await context.raise_if_stopped()
        for key in ready:
            old = state["nodes"].get(key, {})
            state["nodes"][key] = {
                "status": "started",
                "input_hash": inputs[key],
                "attempt": int(old.get("attempt", 0)) + 1,
                "base_output_hash": drafts[key].expected_body_hash if key in drafts else None,
            }
        async with context.session() as session:
            document = await session.scalar(
                select(PaperDocument).where(PaperDocument.id == document_id).with_for_update()
            )
            document.writing_state_json = deepcopy(state)
        await context.emit(
            "writing_dag.wave",
            {
                "sections": ready,
                "mode": mode,
                "ready_nodes": len(ready),
                "local_window_limit": context.settings.writer_concurrency
                if mode == "dag_parallel"
                else 1,
                "note": "Local window includes provider waiters; not actual HTTP concurrency.",
            },
        )

        async def generate(key):
            events, warnings = [], []

            async def emit(*args, **kwargs):
                events.append((args, kwargs))

            view = WritingContext(
                outline=deepcopy(writing_context.outline),
                language=writing_context.language,
                paper_type=writing_context.paper_type,
                glossary=dict(state["glossary"]),
                dependency_summaries={
                    dep: writing_context.rolling_summaries.get(dep, "") for dep in body_graph[key]
                },
            )
            local = WriteOutcome()
            async with context.span(
                "subagent",
                "section",
                node_id=key,
                input_hash=inputs[key],
                attempt=state["nodes"][key]["attempt"],
            ):
                draft = await _write_one(
                    section=bodies[key],
                    cards=cards,
                    whitelist=set(whitelist),
                    writing_context=view,
                    runner=context.llm_runner(),
                    context=context,
                    outcome=local,
                    assets=assets,
                    emit=emit,
                    warn=lambda *args: warnings.append(args),
                )
            return draft, local, events, warnings

        async def commit(_index, key, result):
            if isinstance(result, BaseException):
                state["nodes"][key]["status"] = "failed"
                await context.emit(
                    "writing_dag.failed", {"section": key, "error": type(result).__name__}
                )
                return
            draft, local, events, warnings = result
            previous = drafts.get(key)
            draft.expected_body_hash = previous.expected_body_hash if previous else None
            draft.generation.update({"input_hash": inputs[key], "dependencies": body_graph[key]})
            state["nodes"][key]["status"] = "completed" if draft.has_body else "failed"
            state["nodes"][key]["output_hash"] = fingerprint(
                draft.to_ir_section().model_dump(mode="json")
            )
            await _persist_draft(
                context,
                document_id=document_id,
                draft=draft,
                order_no=order[key],
                whitelist=whitelist,
                language=writing_context.language,
                graph_state=state,
            )
            drafts[key] = draft
            writing_context.register(key, draft)
            outcome.warnings.extend(local.warnings)
            outcome.recovered_sections.extend(local.recovered_sections)
            for args in warnings:
                context.warn(*args)
            for args, kwargs in events:
                await context.emit(*args, **kwargs)
            await context.emit(
                "write.section",
                {
                    "section": key,
                    "title": draft.title,
                    "words": draft.word_count,
                    "generator": draft.generator,
                    "index": sum(n["status"] == "completed" for n in state["nodes"].values()),
                    "total": len(sections),
                },
                stage=WRITE_STAGE,
                progress=_WRITE_PROGRESS_START
                + (_POLISH_PROGRESS_START - _WRITE_PROGRESS_START)
                * sum(n["status"] == "completed" for n in state["nodes"].values())
                / max(1, len(bodies)),
            )

        await bounded_map(
            ready,
            generate,
            limit=context.settings.writer_concurrency if mode == "dag_parallel" else 1,
            on_ready=commit,
            stop_check=context.raise_if_stopped,
        )
        if any(state["nodes"][key]["status"] != "completed" for key in ready):
            raise JobStopped("pause", reason="writing_node_failed")


def _repair_levels(outline_keys: Sequence[str], targets: set[str]) -> dict[str, int]:
    """把修复目标按「前文摘要」依赖分层：同一层的目标互相读不到对方的重写结果。

    ``WritingContext.preceding_summary`` 只读 outline 里**前两节**的滚动摘要
    （``keys[max(0, index - 2):index]``），所以依赖是一条窄窗口关系，而不是全前缀。
    分层规则就是这条关系的传递闭包：

        level[i] = 0                                       若 i-1、i-2 处都不是目标
                 = 1 + max(level[j] for j in {i-1, i-2} 且 j 是目标)

    于是层内任意两个目标在 outline 上至少相隔 3 个位置，谁也读不到谁的
    ``rolling_summaries`` 条目——把它们并发跑，喂给模型的提示词与串行**逐字节相同**。

    首轮写作用不上这个：那里每个正文小节都是目标，``i → i-1, i-2`` 构成稠密链条，
    分层会退化成 ``level[i] = i``（即完全串行）。修复轮的目标是稀疏子集，才有并行度。

    注意 ``abstract`` 与 ``introduction`` 永远是目标（见 ``repair_document_sections``），
    而它们在 outline 里就排在最前两位，所以 outline 序号 2、3 上的目标被强制到
    第 2、3 层。实际每次分出 3-4 层是正常的，不是 1 层。
    """
    levels: dict[str, int] = {}
    for index, key in enumerate(outline_keys):
        if key not in targets:
            continue
        dependencies = [
            levels[outline_keys[position]]
            for position in (index - 1, index - 2)
            if position >= 0 and outline_keys[position] in targets
        ]
        levels[key] = 1 + max(dependencies) if dependencies else 0
    return levels


@traced("repair", "sections")
async def repair_document_sections(
    context: JobContext,
    *,
    section_keys: set[str],
    language: str,
    paper_type: str,
    notes: dict[str, str] | None = None,
    concurrency: int = 1,
) -> WriteOutcome:
    """Rewrite only quality-failing sections against the latest evidence/outline.

    ``notes`` carries a per-section instruction describing *what specifically* was
    wrong with that section. Without it every repair is the same generic "drop
    unsupported claims" nudge, and a section that failed for listing its evidence
    one-sentence-at-a-time gets rewritten into the same list.
    """
    outcome = WriteOutcome()
    async with context.session() as session:
        document = await latest_document(session, context.project_id)
        outline_row = (
            await get_outline(session, document.outline_id)
            if document and document.outline_id
            else None
        )
        if outline_row is None or document is None:
            return outcome
        outline = dict(outline_row.tree_json or {})
        rows = await list_sections(session, document.id)
        whitelist = await get_writing_whitelist(session, context.project_id)
        cards = await _card_context(session, context.project_id)
        assets = await grounded_asset_payloads(session, context.project_id)
    sections = [item for item in outline.get("sections") or [] if isinstance(item, dict)]
    existing = {row.section_key: _draft_from_section(row) for row in rows}
    writing_context = WritingContext(outline=outline, language=language, paper_type=paper_type)
    for section in sections:
        key = str(section.get("key") or "")
        if key in existing:
            writing_context.register(key, existing[key])

    # Frame sections summarize the repaired body and must never remain attached
    # to claims that were removed in a prior round.
    targets = set(section_keys) | {"abstract", "introduction", "conclusion"}
    order_by_key = {str(item.get("key") or ""): index for index, item in enumerate(sections)}
    outline_keys = [str(item.get("key") or "") for item in sections]
    section_by_key = {key: item for key, item in zip(outline_keys, sections, strict=True)}
    explicit_graph = (
        section_graph(sections)
        if (document.writing_state_json or {}).get("mode") in {"dag_serial", "dag_parallel"}
        else None
    )
    if explicit_graph is not None:
        targets = descendants(explicit_graph, targets)
    body_targets = {
        key for key in targets if key in section_by_key and not is_frame(section_by_key[key])
    }
    levels = _repair_levels(outline_keys, body_targets)
    if explicit_graph is not None:
        repair_graph = {
            key: [dep for dep in deps if dep in body_targets]
            for key, deps in explicit_graph.items()
            if key in body_targets
        }
        levels = {key: index for index, wave in enumerate(layers(repair_graph)) for key in wave}
    frame_level = max(levels.values(), default=-1) + 1
    levels.update(
        {
            key: frame_level
            for key in targets
            if key in section_by_key and is_frame(section_by_key[key])
        }
    )

    # glossary 冻结（并发正确性的前提）。
    #
    # `WritingContext.glossary` 被插进**每一个** writer 提示词（`_build_prompt`），
    # 而 `register()` 会往里加词。串行执行下，第 i 个目标因此看得到前面**所有**目标
    # 新造的术语——这是一条隐蔽的全序依赖，比「前两节摘要」那条宽得多，按它分层
    # 会让每个目标都依赖所有更早的目标，调度直接退化成串行。
    #
    # 所以这里把它冻住：整轮修复共用进入时的那一份。代价很小——修复路径的基础
    # glossary 已经由**全部已有章节**播种（上面那个预填充循环），而 `register` 用的是
    # `setdefault`，所以唯一丢掉的是「某次重写新造的词泄漏给同一轮里更晚的一节」。
    # 换来的是一条可证明、可测试的顺序无关性：并发与串行喂给模型的提示词逐字节相同。
    frozen_glossary = dict(writing_context.glossary)
    wave_width = max(1, int(concurrency))

    for level in sorted(set(levels.values())):
        wave = [key for key in outline_keys if levels.get(key) == level]
        if not wave:
            continue
        # 本层开始时的滚动摘要。由分层保证：本层任一目标的前两节都不在本层，
        # 所以这份快照与串行执行到该目标时看到的内容完全一致。
        wave_summaries = dict(writing_context.rolling_summaries)

        async def _repair_one(
            key: str,
            *,
            wave_summaries: dict[str, str] = wave_summaries,
        ) -> tuple[SectionDraft, WriteOutcome, list[tuple[Any, Any]], list[Any]]:
            section = section_by_key[key]
            repair_section = {
                **section,
                "summary": (
                    str(section.get("summary") or "")
                    + "\nQUALITY REPAIR: omit every claim that is not directly supported by the "
                    "provided evidence/source refs; split non-comparable results and copy no "
                    "number without an exact locator."
                    # 只说「删掉」会让每一轮修复都把这一节削短一点。实测（项目 ff6b9983
                    # 第 2 版）结论 351 → 136 → 137 字、引言 274 → 179 → 171 字：三轮下来
                    # 框架章节被削掉六成。修复是**重写**，不是删除——删掉超证据的说法之后，
                    # 还要把这一节该覆盖的内容用站得住的证据重新写完整。
                     + "\nThis is a rewrite, not a deletion: after removing the unsupported "
                    "claims, still cover this section's stated goal in full using the evidence "
                    "that does hold, and keep the section's target length."
                    + (f"\n{(notes or {}).get(key, '')}" if (notes or {}).get(key) else "")
                ),
            }
            # 每个任务拿自己的视图与自己的 outcome：并发期间不碰任何共享可变量。
            view = WritingContext(
                outline=writing_context.outline,
                language=language,
                paper_type=paper_type,
                glossary=frozen_glossary,
                rolling_summaries=dict(wave_summaries),
                dependency_summaries=(
                    {dep: wave_summaries.get(dep, "") for dep in explicit_graph[key]}
                    if explicit_graph is not None
                    else None
                ),
            )
            local_outcome = WriteOutcome()
            events: list[tuple[Any, Any]] = []
            warns: list[Any] = []

            async def _buffer_emit(*args: Any, **kwargs: Any) -> None:
                events.append((args, kwargs))

            def _buffer_warn(*args: Any) -> None:
                warns.append(args)

            draft = await _write_one(
                section=repair_section,
                cards=cards,
                whitelist=set(whitelist),
                writing_context=view,
                runner=context.llm_runner(),
                context=context,
                outcome=local_outcome,
                assets=assets,
                emit=_buffer_emit,
                warn=_buffer_warn,
            )
            previous = existing.get(key)
            draft.expected_body_hash = previous.expected_body_hash if previous else None
            if is_frame(section):
                draft.generation["body_snapshot"] = fingerprint(
                    {
                        k: d.expected_body_hash
                        for k, d in existing.items()
                        if k in section_by_key and not is_frame(section_by_key[k])
                    }
                )
            return draft, local_outcome, events, warns

        async def _commit(_index: int, key: str, result: Any) -> None:
            if isinstance(result, BaseException):
                # 一节修复失败只损失这一节：保留库里原来的正文，记降级标记。
                context.warn(
                    "quality_repair",
                    type(result).__name__,
                    {"section": key, "message": str(result)[:300]},
                )
                outcome.warnings.append(
                    {
                        "stage": "quality_repair",
                        "section": key,
                        "reason": type(result).__name__,
                    }
                )
                return
            draft, local_outcome, events, warns = result
            # 缓冲的告警与事件按 outline 顺序重放，事件流与串行时一致。
            for warn_args in warns:
                context.warn(*warn_args)
            for args, kwargs in events:
                await context.emit(*args, **kwargs)
            outcome.warnings.extend(local_outcome.warnings)
            outcome.recovered_sections.extend(local_outcome.recovered_sections)
            outcome.incomplete_sections.extend(local_outcome.incomplete_sections)
            outcome.rewrite_count += local_outcome.rewrite_count

            section = section_by_key[key]
            previous = existing.get(key)
            if _is_thinner_refresh(section, previous, draft):
                # 框架章节不是因为自己有问题才被重写的——它们每一轮都跟着正文刷新一遍，
                # 为的是不停留在已经被删掉的论断上。既然不是在修缺陷，那么「刷完之后
                # 短了一大截」就只是这一次采样偏短，不是修复。实测（项目 ff6b9983 第 3 版）
                # 四轮修复下来引言 264→364→361→315→235、结论 207→240→174→214→284：
                # 每一轮都是一次全新生成，长度上下摆动 ±50%，最后交付的是**最后一次**
                # 而不是最好的一次。这里给刷新加一道棘轮，正文小节不受影响（它们的重写
                # 是冲着具体缺陷去的，变短可能正是修复本身）。
                await context.emit(
                    "quality_repair.refresh_rejected",
                    {
                        "section": key,
                        "kept_words": previous.word_count if previous else 0,
                        "rejected_words": draft.word_count,
                    },
                    stage="quality_repair",
                )
                return
            await _persist_draft(
                context,
                document_id=document.id,
                draft=draft,
                order_no=order_by_key.get(key, 0),
                whitelist=whitelist,
                language=language,
            )
            # `existing[key]` 与 `register` 留在有序 pass 里：outline key 唯一，
            # 任务写不到别人的槽，所以 `_is_thinner_refresh` 的比较与串行逐位一致。
            existing[key] = draft
            writing_context.register(key, draft)
            outcome.section_count += 1
            outcome.word_count += draft.word_count
            await context.emit(
                "quality_repair.section",
                {"section": key, "words": draft.word_count},
                stage="quality_repair",
            )

        await bounded_map(wave, _repair_one, limit=wave_width, on_ready=_commit)
        # 停止检查放在**层边界**，不放在任务里：层中途抛出会丢弃兄弟任务已经付过钱
        # 的草稿，违反「已完成的调用不丢弃」这条不变量。
        await context.raise_if_stopped()
    outcome.document_id = str(document.id)
    return outcome


#: 框架章节刷新后短于原来的这个比例就不采用。0.8 是「明显更短」而不是「措辞变了」：
#: 实测四轮刷新的长度波动在 ±50%，而真正因为删掉超证据论断而变短的幅度要小得多。
FRAME_REFRESH_MIN_RATIO = 0.8


def _is_thinner_refresh(
    section: dict[str, Any],
    previous: SectionDraft | None,
    draft: SectionDraft,
) -> bool:
    """这次刷新是不是把框架章节写薄了。

    只对框架章节生效，且只在**已经有上一稿**时生效。新稿完全没写出来（0 字）时也算，
    否则一次失败的生成会把整节清空。
    """
    if section.get("kind") != "frame" or previous is None:
        return False
    if previous.word_count <= 0:
        return False
    return draft.word_count < previous.word_count * FRAME_REFRESH_MIN_RATIO


async def snapshot_document_sections(context: JobContext) -> dict[str, dict[str, Any]]:
    async with context.session() as session:
        document = await latest_document(session, context.project_id)
        rows = await list_sections(session, document.id) if document else []
        usage_rows = await list_citation_usage(session, context.project_id)
    usages_by_section: dict[uuid.UUID, list[dict[str, Any]]] = {}
    for usage in usage_rows:
        usages_by_section.setdefault(usage.section_id, []).append(
            {
                "work_id": usage.work_id,
                "cite_key": usage.cite_key,
                "context_snippet": usage.context_snippet,
            }
        )
    return {
        row.section_key: {
            "document_id": row.document_id,
            "title": row.title,
            "order_no": row.order_no,
            "body_ir": row.body_ir_json,
            "cite_keys": row.cite_keys_json or [],
            "asset_refs": row.asset_refs_json or [],
            "status": row.status,
            "model": row.model,
            "generation": row.generation_json,
            "usages": usages_by_section.get(row.id, []),
        }
        for row in rows
    }


async def restore_document_sections(
    context: JobContext,
    snapshot: dict[str, dict[str, Any]],
) -> None:
    """Restore a non-improving repair candidate and its citation-usage rows."""
    async with context.session() as session:
        document = await latest_document(session, context.project_id)
        current_rows = await list_sections(session, document.id) if document else []
        for current in current_rows:
            if current.section_key not in snapshot:
                await session.delete(current)
        await session.flush()
        for key, item in snapshot.items():
            row = await upsert_section(
                session,
                document_id=item["document_id"],
                section_key=key,
                title=item["title"],
                order_no=item["order_no"],
                body_ir=item["body_ir"],
                cite_keys=item["cite_keys"],
                asset_refs=item["asset_refs"],
                status=item["status"],
                model=item["model"],
            )
            row.generation_json = item.get("generation")
            await replace_citation_usage(
                session,
                project_id=context.project_id,
                section_id=row.id,
                usages=item["usages"],
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
        meta=PaperMeta(title=project_title, language=language),  # type: ignore[arg-type]
        sections=sections,
        bibliography=Bibliography(style=citation_style),  # type: ignore[arg-type]
    )
    # 双保险：预览也走一次白名单检查，绝不展示越权引用。
    ir.enforce_cite_key_whitelist(set(whitelist), strip=True)
    return render_markdown(ir, references=references, style=citation_style)


def _document_model():
    from db.models.paper import PaperDocument

    return PaperDocument


def _numlint(
    ordered: list[tuple[str, SectionDraft]],
    *,
    assets: list[dict[str, Any]],
    paper_type: str,
    cards: dict[str, dict[str, Any]],
):
    """全篇数值溯源检查。修复轮跑完要用同一把尺子复检，所以抽成一个函数。"""
    return lint_sections(
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


async def _repair_unsourced_numbers(
    context: JobContext,
    *,
    report,
    ordered: list[tuple[str, SectionDraft]],
    document_id: uuid.UUID,
    order_by_key: dict[str, int],
    whitelist: dict[str, uuid.UUID],
    language: str,
    runner,
    outcome: WriteOutcome,
    relint,
):
    """把没有出处的数字从正文里清掉，然后用同一把尺子复检。

    NUMLINT 以前只报数：一个查不到出处的「超过 200 种」照样进交付稿，而它读起来
    和有出处的数据毫无区别。这里按章节把受影响的句子交回模型改写成定性表述，
    改不动的直接删句；引用绑定全程不动。
    """
    by_section: dict[str, set[str]] = {}
    for finding in report.unsourced:
        by_section.setdefault(finding.section_key, set()).add(finding.value)
    drafts_by_key = dict(ordered)
    rewritten_count = 0
    removed_count = 0
    repaired_sections: list[str] = []

    for section_key, values in by_section.items():
        draft = drafts_by_key.get(section_key)
        if draft is None:
            continue
        draft, stats = await repair_unsourced_numbers(
            draft=draft,
            values=values,
            language=language,
            runner=runner,
        )
        if not (stats["rewritten"] or stats["removed"]):
            continue
        rewritten_count += stats["rewritten"]
        removed_count += stats["removed"]
        repaired_sections.append(section_key)
        await _persist_draft(
            context,
            document_id=document_id,
            draft=draft,
            order_no=order_by_key.get(section_key, 0),
            whitelist=whitelist,
            language=language,
        )

    if repaired_sections:
        repaired = {
            "rewritten": rewritten_count,
            "removed": removed_count,
            "sections": sorted(repaired_sections),
        }
        outcome.warnings.append(
            {"stage": "numlint", "reason": "unsourced_numbers_repaired", **repaired}
        )
        await context.emit("numlint.repaired", repaired, stage="numlint")
    return relint()


#: 会驱动重写、但不否决交付的缺陷码。
NON_BLOCKING_SECTION_DEFECTS = frozenset({"below_target"})


async def _recover_incomplete_sections(
    context: JobContext,
    *,
    sections: list[dict[str, Any]],
    drafts: dict[str, SectionDraft],
    document_id: uuid.UUID,
    order_by_key: dict[str, int],
    cards: dict[str, dict[str, Any]],
    whitelist: dict[str, uuid.UUID],
    language: str,
    writing_context: WritingContext,
    runner,
    outcome: WriteOutcome,
    assets: list[dict[str, Any]] | None,
) -> None:
    """交付前把还没写成的章节再救一轮，并记录最终仍然缺的是哪几节。

    到这里时全篇的滚动摘要已经建好，重写一节能看到完整的上下文——这是首轮写作
    时没有的信息，所以这一轮**不是**把同一次调用原样再发一遍。

    这一步同时是完整性的唯一判据来源：跑完之后 ``outcome.incomplete_sections``
    要么是空的（稿子完整），要么明确列出哪几节缺、缺什么，由上层决定还能不能算交付。
    """
    by_key = {str(section.get("key") or ""): section for section in sections}
    pending = [
        key
        for key, draft in drafts.items()
        if key in by_key
        and inspect_section_draft(
            draft,
            language=language,
            evidence=section_evidence_for(by_key.get(key) or {}, writing_context.outline),
            is_frame=(by_key.get(key) or {}).get("kind") == "frame",
            section=by_key.get(key),
        )
    ]
    if not pending:
        return

    await context.emit(
        "write.recovery_started",
        {"sections": sorted(pending), "total": len(pending)},
        stage=WRITE_STAGE,
    )
    for key in sorted(pending)[:MAX_RECOVERY_SECTIONS]:
        section = by_key.get(key)
        if section is None or runner is None or not runner.enabled:
            continue
        await context.raise_if_stopped()
        previous = drafts[key]
        retried = await _write_one(
            section=(
                section
                if section.get("kind") != "frame"
                else {**section, "summary": _frame_goal(section, writing_context.outline, language)}
            ),
            cards=cards,
            whitelist=set(whitelist),
            writing_context=writing_context,
            runner=runner,
            context=context,
            outcome=outcome,
            assets=assets,
            # 这一轮只补一次：主力阶梯已经在首轮用掉了，这里再堆重试收益递减。
            max_attempts=1,
        )
        if not retried.has_body and previous.has_body:
            continue
        retried.expected_body_hash = previous.expected_body_hash
        retried.generation = dict(previous.generation)
        drafts[key] = retried
        writing_context.register(key, retried)
        await _persist_draft(
            context,
            document_id=document_id,
            draft=retried,
            order_no=order_by_key.get(key, 0),
            whitelist=whitelist,
            language=language,
        )

    # 完整性只看「有没有写出来」。``below_target`` 是「写薄了」——它有正文、有引用，
    # 只是没够着本节的篇幅目标；它已经在写作回路里驱动过一次重写，不该再让交付判定
    # 说出「这一节仍然没有正文」这种与事实相反的话。
    outcome.incomplete_sections = [
        item for item in outcome.incomplete_sections if item["section"] not in by_key
    ] + [
        {
            "section": key,
            "defects": blocking,
            "generator": drafts[key].generator,
        }
        for key in sorted(drafts)
        if key in by_key
        and (
            blocking := [
                defect.code
                for defect in inspect_section_draft(
                    drafts[key],
                    language=language,
                    evidence=section_evidence_for(by_key.get(key) or {}, writing_context.outline),
                    is_frame=(by_key.get(key) or {}).get("kind") == "frame",
                    section=by_key.get(key),
                )
                if defect.code not in NON_BLOCKING_SECTION_DEFECTS
            ]
        )
    ]
    await context.emit(
        "write.recovery_completed",
        {
            "recovered": [item["section"] for item in outcome.recovered_sections],
            "incomplete": outcome.incomplete_sections,
        },
        stage=WRITE_STAGE,
    )


@traced("subagent", "section_writer")
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
    max_attempts: int = MAX_SECTION_ATTEMPTS,
    emit: Callable[..., Awaitable[None]] | None = None,
    warn: Callable[..., None] | None = None,
) -> SectionDraft:
    """写一节，并在这一节内部完成恢复。

    单节失败以前是终点：一次调用不成就落降级占位，整篇带着窟窿继续走。实际发生的
    失败绝大多数是**这一次调用**的问题（推理吃光预算、整段照抄证据、写着写着切到
    英文），换一种要法再问一次就能拿到正常正文。所以恢复必须发生在这里——此时
    prompt、证据、白名单都还在手上，而不是等到几十分钟后由一个全篇修复任务重来。

    升级阶梯，每一级都针对上一级暴露的具体问题：
      1. 正常写；
      2. 带纠正指令重写（照抄/语种/过短各有各的说法）；截断则改走紧凑档降低需求；
      3. 紧凑档 + 纠正指令一起上。
    仍然不成才落 needs_rewrite，交给交付前的补写扫描与质量修复闭环。
    """
    # 事件与告警走可注入的出口。串行调用方不传，行为与引入前逐字节相同；
    # 修复轮的波调度并发跑这个函数，那里传入缓冲区，等回到有序 pass 再按
    # outline 顺序重放——`context.emit` 要占一条数据库连接，并发发事件既会打乱
    # 事件流，也会把连接池吃穿。
    _emit = emit if emit is not None else context.emit
    _warn = warn if warn is not None else context.warn
    section_key = str(section.get("key") or "section")
    is_frame = section.get("kind") == "frame"
    section_evidence = section_evidence_for(section, writing_context.outline)
    best: SectionDraft | None = None
    best_defects: list[SectionDefect] = []
    compact = False
    corrections: list[str] = []

    for attempt in range(1, max_attempts + 1):
        try:
            draft = await write_section(
                section=section,
                cards=cards,
                whitelist=whitelist,
                context=writing_context,
                runner=runner,
                assets=assets,
                compact=compact,
                corrections=corrections or None,
            )
        except JobStopped:
            raise
        except Exception as error:  # noqa: BLE001 - 单章失败不阻断整篇（draft-first）
            logger.warning(
                "section writing failed",
                extra={"section": section_key, "error": type(error).__name__, "attempt": attempt},
            )
            _warn("write", type(error).__name__, {"section": section_key})
            outcome.warnings.append(
                {"stage": "write", "section": section_key, "reason": type(error).__name__}
            )
            draft = SectionDraft(
                section_key=section_key,
                title=str(section.get("title") or section_key),
            )
            draft.paragraphs = [{"text": "", "cite_keys": []}]
            draft.generator = "failed"
            draft.failure_reason = type(error).__name__

        draft.attempts = attempt
        defects = inspect_section_draft(
            draft,
            language=writing_context.language,
            evidence=section_evidence,
            is_frame=is_frame,
            section=section,
        )
        if not defects:
            if attempt > 1:
                outcome.recovered_sections.append(
                    {"section": section_key, "attempts": attempt, "recovered_from": corrections[:1]}
                )
                await _emit(
                    "write.section_recovered",
                    {"section": section_key, "attempts": attempt},
                    stage=WRITE_STAGE,
                )
            return draft

        # 「有正文但有瑕疵」严格好于「只有占位」：留住目前最好的一版，
        # 全部重试都失败时至少不会把一段真正文换成一句占位。
        if best is None or _draft_rank(draft, defects) > _draft_rank(best, best_defects):
            best, best_defects = draft, defects

        if attempt >= max_attempts or all(not defect.recoverable for defect in defects):
            break

        corrections = [
            text
            for defect in defects
            if (text := defect.correction(language=writing_context.language))
        ]
        # 截断/空返回不是「写得不对」，加纠正指令没有意义——要减少要写的量。
        if any(defect.code == "not_generated" for defect in defects) or not corrections:
            compact = True
        logger.info(
            "retrying section",
            extra={
                "section": section_key,
                "attempt": attempt + 1,
                "defects": [defect.code for defect in defects],
                "compact": compact,
            },
        )
        await _emit(
            "write.section_retry",
            {
                "section": section_key,
                "attempt": attempt + 1,
                "defects": [defect.code for defect in defects],
                "compact": compact,
            },
            stage=WRITE_STAGE,
        )

    assert best is not None  # 循环至少跑一轮
    outcome.warnings.append(
        {
            "stage": "write",
            "section": section_key,
            "reason": "section_not_matured",
            "defects": [defect.code for defect in best_defects],
            "attempts": best.attempts,
        }
    )
    _warn(
        "write",
        "section_not_matured",
        {"section": section_key, "defects": [defect.code for defect in best_defects]},
    )
    return best


def _draft_rank(draft: SectionDraft, defects: list[SectionDefect]) -> tuple[int, int, int]:
    """比较两版草稿哪一版更接近成稿：有正文 > 缺陷少 > 篇幅足。"""
    return (int(draft.has_body), -len(defects), draft.word_count)


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
    # `FRAME_SECTION_BRIEFS` spells out paragraph structure and what each one
    # must contain; flattening it to a single line here threw that away on the
    # initial write (the repair path used the brief and produced better prose
    # than the first draft).  The brief also disagreed with itself: it asked the
    # abstract for 150-250 characters while the outline set `target_words` 350.
    brief = str(section.get("summary") or "").strip()
    if brief:
        return f"{brief}\n研究问题：{question}" if zh else f"{brief}\nResearch question: {question}"
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
