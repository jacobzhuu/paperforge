"""M8 视觉建议与生成管线。

建议阶段只创建结构化 proposal，不调用图片 provider、不改 PaperIR；生成阶段才调用
visuald 或经用户确认后的 ImageProvider。
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any

from db import (
    add_visual_source,
    create_visual,
    document_snapshot_hash,
    get_asset,
    get_project,
    get_visual,
    latest_document,
    list_assets,
    list_sections,
    list_visuals,
    record_visual_attempt,
    visual_input_hash,
)
from storage import make_object_store
from visuals import (
    AIImageSemantics,
    AIImageSpec,
    ChartSpec,
    DiagramSpec,
    ImageProvider,
    ImageProviderError,
    ImageRequest,
    ImageResult,
    VisualdClient,
    classify_visual_error,
    create_image_provider,
    image_provider_configured,
    parse_visual_spec,
)
from visuals.errors import CONTENT_REJECTED, INVALID_REQUEST, PROVIDER_NOT_CONFIGURED

from paperforge_worker.context import JobContext
from paperforge_worker.pipelines.image_prompt import (
    analyze_image_prompt,
    build_paper_context,
    rewrite_rejected_image_prompt,
)
from paperforge_worker.pipelines.visual_planner import (
    MAX_AI_IMAGES,
    MAX_PROPOSALS,
    PlannedProposal,
    SectionBrief,
    plan_visuals,
)


@dataclass
class VisualOutcome:
    visual_id: str
    status: str
    rendition_formats: list[str]
    error_code: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "visual_id": self.visual_id,
            "status": self.status,
            "rendition_formats": self.rendition_formats,
            "error_code": self.error_code,
        }


@dataclass
class VisualPlanOutcome:
    proposed: int
    chart_count: int
    diagram_count: int
    ai_image_count: int
    preview_ready_count: int = 0
    preview_failed_count: int = 0
    auto_generated_count: int = 0
    generation_deferred_count: int = 0
    #: `llm:<model>` 或 `deterministic*`——排查「为什么建议这么烂」时的第一个问题
    #: 就是它到底走了哪条路。
    generator: str = "deterministic"

    def to_payload(self) -> dict[str, Any]:
        return {
            "proposed": self.proposed,
            "chart_count": self.chart_count,
            "diagram_count": self.diagram_count,
            "ai_image_count": self.ai_image_count,
            "preview_ready_count": self.preview_ready_count,
            "preview_failed_count": self.preview_failed_count,
            "auto_generated_count": self.auto_generated_count,
            "generation_deferred_count": self.generation_deferred_count,
            "generator": self.generator,
        }


@dataclass(frozen=True)
class _ImageGenerationOutcome:
    generated: ImageResult | None = None
    error: Exception | None = None
    first_rejection: ImageProviderError | None = None
    compliance_audit: dict[str, Any] | None = None


async def _generate_image_with_compliance_retry(
    provider: ImageProvider,
    request: ImageRequest,
    *,
    runner: Any,
) -> _ImageGenerationOutcome:
    """调用图像厂商；内容拒绝时至多做一次安全改写和一次额外调用。"""
    try:
        generated = await asyncio.to_thread(provider.generate, request)
        return _ImageGenerationOutcome(generated=generated)
    except ImageProviderError as first_error:
        if first_error.code != CONTENT_REJECTED:
            return _ImageGenerationOutcome(error=first_error)
        rejection = first_error

    original_error = str(rejection)[:500]
    try:
        rewrite = await rewrite_rejected_image_prompt(request.prompt, runner=runner)
    except Exception:  # noqa: BLE001 - 改写失败不能掩盖原始内容拒绝
        rewrite = None

    audit: dict[str, Any] = {
        "original_prompt": request.prompt,
        "rewritten_prompt": rewrite.prompt if rewrite else None,
        "original_error_code": rejection.code,
        "original_error_reason": original_error,
        "original_request_id": rejection.request_id,
        "retry_count": 1 if rewrite else 0,
        "max_retries": 1,
        "rewrite_reason": rewrite.reason if rewrite else None,
    }
    if rewrite is None:
        return _ImageGenerationOutcome(
            error=rejection,
            first_rejection=rejection,
            compliance_audit=audit,
        )

    retry_request = ImageRequest(
        prompt=rewrite.prompt,
        size=request.size,
        quality=request.quality,
        output_format=request.output_format,
        negative_prompt=request.negative_prompt,
        seed=request.seed,
    )
    # 没有循环：第二次调用无论成功或失败都直接返回给最终记录。
    try:
        generated = await asyncio.to_thread(provider.generate, retry_request)
    except Exception as retry_error:  # noqa: BLE001 - caller 统一分类并落库
        return _ImageGenerationOutcome(
            error=retry_error,
            first_rejection=rejection,
            compliance_audit=audit,
        )
    return _ImageGenerationOutcome(
        generated=generated,
        first_rejection=rejection,
        compliance_audit=audit,
    )


async def suggest_visuals(
    context: JobContext,
    *,
    summary_only: bool = False,
    auto_generate: bool = False,
) -> VisualPlanOutcome:
    """提出视觉建议；普通入口绝不调用 ImageProvider 或修改章节 IR。

    普通视觉工作台最多提出 6 条，来源有两条：
      - 数据图表由已上传的结果表格确定性推导——模型不得凭空造数据图，
        图表的每个数字都必须能追回某一份素材；
      - 示意图与 AI 插图交给结构化规划器（`visual_planner`）。模型不可用、
        超时或输出不合法时，回退到「论文结构概览 + 概念插图」这套原有的
        确定性建议，`visual_plan` 永远有产物。

    ``summary_only`` 是“跑通全流程”的专用策略：只创建一张论文摘要图；
    ``auto_generate`` 同时开启时只通过 Yunwu 自动生成这一张。Yunwu 未配置或
    临时失败会保留为可重试建议，不把可选视觉失败升级成全流程错误。
    """
    if not context.settings.visuals_enabled:
        return VisualPlanOutcome(0, 0, 0, 0)

    # 第一段：只读。LLM 调用不持有数据库会话——规划要花几秒到几十秒，
    # 攥着连接等它是把连接池拿去当计时器用。
    async with context.session() as session:
        assets = await list_assets(session, context.project_id)
        project = await get_project(session, context.project_id)
        document = await latest_document(session, context.project_id)
        sections = await list_sections(session, document.id) if document else []
        existing = await list_visuals(session, context.project_id)
        existing_hashes = {item.input_hash for item in existing}
        existing_by_hash: dict[str, Any] = {}
        for item in existing:
            # list_visuals 按 created_at 倒序；同哈希存在历史版本时始终选择最新一张。
            existing_by_hash.setdefault(item.input_hash, item)
        # 建议依据的正文指纹：正文一改，界面就能提示「建议基于旧版正文」。
        snapshot = document_snapshot_hash(sections) if sections else None
        document_version = document.version if document else None
        project_title = project.title if project is not None else "the research topic"
        paper_type = project.paper_type if project is not None else "review"

        abstract_section = next(
            (row for row in sections if row.section_key == "abstract"),
            None,
        )
        chart_proposals = [] if summary_only else _chart_proposals(assets, sections)
        body_sections = [row for row in sections if row.section_key != "abstract"]
        briefs = [
            SectionBrief(
                key=row.section_key,
                title=row.title,
                excerpt=_section_excerpt(row),
            )
            for row in body_sections
        ]
        short_review = _is_short_review(paper_type, briefs)
        full_paper_context = _paper_context(project_title, sections)

    analyzed_prompt_ready = True
    if summary_only:
        summary = _summary_visual_proposal(
            project_title,
            abstract_section=abstract_section,
            body_sections=body_sections,
        )
        analysis = await analyze_image_prompt(
            user_intent=(
                "Create a publication-ready graphical abstract that synthesizes the entire paper."
            ),
            full_paper=full_paper_context,
            runner=context.llm_runner(),
            current_spec=summary.spec,
        )
        if analysis:
            summary.spec.update(
                {
                    "prompt": analysis.prompt,
                    "refined_prompt": analysis.prompt,
                    "quality": "high",
                    "semantics": {
                        "subject": analysis.subject,
                        "composition": analysis.composition,
                        "elements": list(analysis.elements),
                        "text_policy": analysis.text_policy,
                        "aspect_ratio": "3:2",
                    },
                }
            )
            summary.title = analysis.title
            summary.caption = analysis.caption
            summary.alt_text = analysis.alt_text
        else:
            # 全流程不得绕过模型分析把模板提示词直接发给 Yunwu。草稿保留，
            # 用户之后可在工作台重试论文上下文分析。
            analyzed_prompt_ready = False
        planned = [summary]
        generator = "graphical_abstract"
    else:
        # 第二段：规划。allow_ai_images 只控制**是否提出**插图建议——即使允许，
        # 也仍然只是 proposal，生图要用户再确认一次。
        planned, generator = await plan_visuals(
            sections=briefs,
            runner=context.llm_runner(),
            allow_ai_images=context.settings.ai_images_enabled,
            paper_context=full_paper_context,
        )
        if short_review:
            # 短综述保留至多一张真正解释关系的示意图；装饰性 AI 插图会稀释信息密度。
            planned = [item for item in planned if item.kind == "diagram"][:1]
        if not planned and not short_review:
            planned = _fallback_proposals(
                body_sections,
                project_title,
                allow_ai_images=context.settings.ai_images_enabled,
            )
        elif not planned and short_review:
            generator = f"{generator}:short_review_no_generic_fallback"

    proposals: list[tuple[dict[str, Any], dict[str, Any]]] = [
        *chart_proposals,
        *(
            (
                item.spec,
                {
                    "title": item.title,
                    "caption": item.caption,
                    "alt_text": item.alt_text,
                    "target_section_key": item.target_section_key,
                    "suggestion_reason": item.reason,
                    "source_section_keys": item.source_section_keys,
                },
            )
            for item in planned
        ),
    ]

    # 第三段：落库。
    async with context.session() as session:
        assets = await list_assets(session, context.project_id)
        created = []
        generation_candidates: list[uuid.UUID] = []
        ai_images = 0
        for spec, metadata in proposals[:MAX_PROPOSALS]:
            input_hash = visual_input_hash(spec)
            if input_hash in existing_hashes:
                # 同一条建议已经提过。哈希走语义投影，因此给 spec 加可选字段
                # 不会让历史资产失效、被重复提出。
                existing_visual = existing_by_hash.get(input_hash)
                if (
                    summary_only
                    and existing_visual is not None
                    and existing_visual.review_status == "pending"
                    and existing_visual.generation_status in {"proposed", "failed"}
                ):
                    generation_candidates.append(existing_visual.id)
                continue
            if spec["kind"] == "ai_image":
                if ai_images >= MAX_AI_IMAGES:
                    continue
                ai_images += 1
            visual = await create_visual(
                session,
                project_id=context.project_id,
                kind=spec["kind"],
                spec=spec,
                document_version=document_version,
                paper_snapshot_hash=snapshot,
                **metadata,
            )
            created.append(visual)
            if summary_only and visual.kind == "ai_image":
                generation_candidates.append(visual.id)
            if spec["kind"] == "chart":
                source_ref = str(spec["source_asset_ref"])
                source = next(
                    (asset for asset in assets if f"ua_{str(asset.id)[:8]}" == source_ref),
                    None,
                )
                if source is not None:
                    await add_visual_source(
                        session,
                        visual_id=visual.id,
                        user_asset=source,
                        source_hash=_source_hash(context, source),
                    )
            existing_hashes.add(visual.input_hash)
        deterministic_ids = [visual.id for visual in created if visual.kind in {"chart", "diagram"}]
        counts = {
            "chart": sum(item.kind == "chart" for item in created),
            "diagram": sum(item.kind == "diagram" for item in created),
            "ai_image": sum(item.kind == "ai_image" for item in created),
        }
        proposed = len(created)

    # 确定性预览无外部费用，建议后并发生成；上限 5 秒使 visuald
    # 不可用时不会长时卡住原有 draft-first 管线。AI proposal 不在此列表中，
    # 因此这条路径结构上无法调用 ImageProvider。
    previews = await asyncio.gather(
        *(
            generate_visual(context, visual_id, timeout_seconds=5.0)
            for visual_id in deterministic_ids
        )
    )
    auto_results: list[VisualOutcome] = []
    generation_deferred = 0
    if summary_only and auto_generate and generation_candidates and analyzed_prompt_ready:
        yunwu_config = context.settings.image_provider_config("yunwu")
        if context.settings.ai_images_enabled and image_provider_configured(yunwu_config):
            # 全流程的唯一一张付费图固定走 Yunwu。generate_visual 自己会完成网络重试、
            # 内容合规改写、画布修复和预检；它返回失败状态而不会向外抛异常。
            auto_results = [
                await generate_visual(
                    context,
                    generation_candidates[0],
                    provider_override="yunwu",
                )
            ]
            if auto_results[0].status != "ready":
                # 可选摘要图失败不应在视觉工作台留下红色报错卡，也不能让全流程失败；
                # provider attempt 仍完整保留用于排查，资产退回 proposed 供用户重试。
                await _defer_failed_auto_generation(context, generation_candidates[0])
                generation_deferred = 1
        else:
            generation_deferred = 1

    elif summary_only and auto_generate and generation_candidates:
        generation_deferred = 1

    if summary_only:
        counts = {"chart": 0, "diagram": 0, "ai_image": 1 if proposals else 0}
    return VisualPlanOutcome(
        proposed=proposed,
        chart_count=counts["chart"],
        diagram_count=counts["diagram"],
        ai_image_count=counts["ai_image"],
        preview_ready_count=sum(item.status == "ready" for item in previews),
        preview_failed_count=sum(item.status == "failed" for item in previews),
        auto_generated_count=sum(item.status == "ready" for item in auto_results),
        generation_deferred_count=generation_deferred,
        generator=generator,
    )


def _chart_proposals(
    assets: list[Any], sections: list[Any]
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """从已解析的表格素材确定性推导数据图表建议。

    这条路径刻意不经过模型：图表的坐标轴与数值必须能追回某一份上传素材，
    让模型参与就等于给它一个编造数据的机会。
    """
    proposals: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for asset in assets:
        parsed = asset.parsed_json or {}
        headers = [str(item) for item in (parsed.get("headers") or [])]
        rows = parsed.get("rows") or []
        if len(headers) < 2 or not rows:
            continue
        numeric = _numeric_columns(headers, rows)
        if not numeric:
            continue
        x = next((header for header in headers if header not in numeric), headers[0])
        y = next((header for header in numeric if header != x), numeric[0])
        spec = ChartSpec(
            chart_type="bar" if len(rows) <= 20 else "line",
            source_asset_ref=f"ua_{str(asset.id)[:8]}",
            x=x,
            y=[y],
            x_label=x,
            y_label=y,
        ).model_dump(mode="json")
        proposals.append(
            (
                spec,
                {
                    "title": f"{asset.title or '数据素材'}可视化",
                    "caption": f"{y} 随 {x} 的变化",
                    "alt_text": f"以 {x} 为横轴、{y} 为纵轴的数据图表",
                    "target_section_key": _result_section_key(sections),
                    "suggestion_reason": f"素材「{asset.title or '未命名'}」含可作图的数值列",
                    "source_section_keys": None,
                },
            )
        )
        if len(proposals) >= 4:
            break
    return proposals


def _summary_visual_proposal(
    project_title: str,
    *,
    abstract_section: Any | None,
    body_sections: list[Any],
) -> PlannedProposal:
    """为“一键全流程”构造唯一的论文摘要图，不引入虚构数字或结论。"""
    abstract = _image_safe_text(
        _section_excerpt(abstract_section) if abstract_section is not None else "",
        limit=900,
    )
    title = _image_safe_text(project_title, limit=140) or "the paper's central research topic"
    concepts = [
        _image_safe_text(item, limit=120)
        for item in re.split(r"[。！？.!?;；]\s*", abstract)
        if item.strip()
    ][:4]
    if not concepts:
        concepts = [
            _image_safe_text(getattr(row, "title", ""), limit=80)
            for row in body_sections[:4]
            if _image_safe_text(getattr(row, "title", ""), limit=80)
        ]
    subject = f"Graphical abstract of {title}"
    if abstract:
        subject = f"{subject}: {abstract[: max(0, 197 - len(subject))]}".rstrip(": ")
    summary_text = abstract or (
        "the central question, research approach, and main synthesis of the paper"
    )
    spec = AIImageSpec(
        prompt=(
            f"Create one publication-ready graphical abstract for {title}. "
            f"Faithfully synthesize this paper abstract without inventing measurements: "
            f"{summary_text}."
        ),
        size="1536x1024",
        quality="high",
        style="clean journal graphical abstract, restrained scientific palette, uncluttered",
        semantics=AIImageSemantics(
            subject=subject[:200],
            composition=(
                "one coherent left-to-right visual narrative connecting the research question, "
                "approach, core mechanism, and conclusion"
            ),
            elements=concepts,
            text_policy="auto",
            aspect_ratio="3:2",
        ),
    ).model_dump(mode="json")
    target = getattr(abstract_section, "section_key", None) or (
        getattr(body_sections[0], "section_key", None) if body_sections else None
    )
    source_keys = [
        key
        for key in (
            getattr(abstract_section, "section_key", None),
            *(getattr(row, "section_key", None) for row in body_sections[:4]),
        )
        if key
    ]
    return PlannedProposal(
        kind="ai_image",
        spec=spec,
        title="论文摘要图",
        caption="论文核心问题、研究路径与主要结论的视觉摘要",
        alt_text="以单幅横向学术插图概括论文核心问题、研究路径与主要结论",
        target_section_key=target,
        reason="“跑通全流程”仅生成一张可概览全文的论文摘要图",
        source_section_keys=source_keys,
    )


def _image_safe_text(value: Any, *, limit: int) -> str:
    """清除 VisualSpec 明确禁止的 URL/代码片段，同时保留论文语义。"""
    text = str(value or "")
    text = re.sub(r"https?://\S+", "", text, flags=re.IGNORECASE)
    text = text.replace("```", " ")
    text = re.sub(r"\b(?:import|exec|eval|subprocess)\b", "process", text, flags=re.IGNORECASE)
    return " ".join(text.split())[:limit].strip()


async def _defer_failed_auto_generation(context: JobContext, visual_id: uuid.UUID) -> None:
    """全流程的可选付费图失败后退回可重试状态；attempt 审计记录不删除。"""
    async with context.session() as session:
        visual = await get_visual(session, visual_id)
        if visual is not None and visual.generation_status == "failed":
            visual.generation_status = "proposed"
            visual.error_code = None
            visual.error_message = None


def _fallback_proposals(
    body_sections: list[Any],
    project_title: str,
    *,
    allow_ai_images: bool = False,
) -> list[PlannedProposal]:
    """文本模型不可用时的确定性建议。

    保留原有的两条：论文结构概览示意图 + 一张概念插图。它们信息量有限，
    但保证 `visual_plan` 在任何情况下都有产物（draft-first）。

    `allow_ai_images` 与规划器同源：AI 生图关闭时不能提插图建议，否则用户会拿到
    一张**永远点不动**的卡片——生成按钮被禁用，却看不出是功能没开还是坏了。
    """
    planned: list[PlannedProposal] = []
    if len(body_sections) >= 2:
        diagram = DiagramSpec(
            direction="LR",
            nodes=[
                {"id": f"s{index}", "label": row.title}
                for index, row in enumerate(body_sections, start=1)
            ],
            edges=[
                {"source": f"s{index}", "target": f"s{index + 1}"}
                for index in range(1, len(body_sections))
            ],
            width="full",
        ).model_dump(mode="json")
        planned.append(
            PlannedProposal(
                kind="diagram",
                spec=diagram,
                title="论文结构概览",
                caption="论文主要章节与论述流程",
                alt_text="从左到右连接主要章节的论文结构示意图",
                target_section_key=body_sections[0].section_key,
                reason="文本模型不可用，按章节顺序生成的结构概览",
                source_section_keys=[row.section_key for row in body_sections],
            )
        )

    if not allow_ai_images:
        return planned

    try:
        ai_spec = AIImageSpec(
            prompt=(
                f"A clean conceptual illustration for an academic paper on {project_title}. "
                "Restrained palette, soft even lighting, uncluttered background, "
                "publication quality."
            )
        ).model_dump(mode="json")
    except ValueError:
        # 项目标题里可能带着 URL 之类的内容；它不能混进提示词，
        # 也不应让一条可选建议拖垮整个 visual_plan。
        ai_spec = AIImageSpec(
            prompt=(
                "A clean conceptual illustration of scientific inquiry and collaboration, "
                "organic geometric forms, restrained palette, publication quality."
            )
        ).model_dump(mode="json")
    planned.append(
        PlannedProposal(
            kind="ai_image",
            spec=ai_spec,
            title="概念性研究插图",
            caption="研究主题的概念性视觉概览",
            alt_text="以抽象科学元素表达研究主题的概念插图",
            target_section_key=body_sections[0].section_key if body_sections else None,
            reason="文本模型不可用，按论文标题生成的概念插图",
            source_section_keys=[],
        )
    )
    return planned


def _section_excerpt(row: Any) -> str:
    """章节正文的纯文本摘录，供规划器理解「这节在讲什么」。

    只取段落文本，不送图表块与引用结构：规划器要判断的是内容语义。
    """
    body = row.body_ir_json or {}
    parts: list[str] = []
    for block in body.get("blocks") or []:
        if not isinstance(block, dict):
            continue
        for run in block.get("runs") or []:
            if isinstance(run, dict):
                value = run.get("v") if isinstance(run.get("v"), str) else run.get("text")
                if isinstance(value, str):
                    parts.append(value)
        if isinstance(block.get("text"), str):
            parts.append(block["text"])
    return " ".join(parts).strip()


def _paper_context(project_title: str, sections: list[Any]) -> str:
    """Build section-balanced paper context under the shared image-analysis budget."""
    return build_paper_context(
        project_title,
        [(str(row.section_key), str(row.title), _section_excerpt(row)) for row in sections],
    )


def _is_short_review(paper_type: str, sections: list[SectionBrief]) -> bool:
    if paper_type != "review":
        return False
    text = " ".join(section.excerpt for section in sections)
    cjk_count = len(re.findall(r"[\u4e00-\u9fff]", text))
    word_units = cjk_count if cjk_count else len(re.findall(r"\b\w+\b", text))
    return word_units < 3000


async def generate_visual(
    context: JobContext,
    visual_id: uuid.UUID,
    *,
    timeout_seconds: float | None = None,
    provider_override: str | None = None,
) -> VisualOutcome:
    if not context.settings.visuals_enabled:
        return VisualOutcome(str(visual_id), "failed", [], PROVIDER_NOT_CONFIGURED)
    async with context.session() as session:
        visual = await get_visual(session, visual_id)
        if visual is None or visual.project_id != context.project_id:
            raise ValueError("visual not found")
        if visual.review_status != "pending":
            raise ValueError("approved or rejected visuals cannot be generated in place")
        if visual.generation_status == "running":
            raise ValueError("visual generation is already running")
        visual.generation_status = "running"
        visual.error_code = None
        visual.error_message = None
        spec = parse_visual_spec(visual.spec_json)

    started = time.monotonic()
    provider_name = "visuald"
    provider_model: str | None = None
    request_id: str | None = None
    usage: dict[str, Any] = {}
    visuald = VisualdClient(
        context.settings.visuald_url,
        timeout_seconds=(
            float(timeout_seconds)
            if timeout_seconds is not None
            else float(context.settings.visuald_timeout_seconds)
        ),
    )
    provider: ImageProvider | None = None
    try:
        if isinstance(spec, ChartSpec):
            asset = await _source_asset(context, spec.source_asset_ref)
            parsed = asset.parsed_json or {}
            result = await asyncio.to_thread(
                visuald.render_chart_result,
                spec.model_dump(mode="json"),
                {"headers": parsed.get("headers") or [], "rows": parsed.get("rows") or []},
            )
        elif isinstance(spec, DiagramSpec):
            result = await asyncio.to_thread(
                visuald.render_diagram_result, spec.model_dump(mode="json")
            )
        else:
            if not context.settings.ai_images_enabled:
                raise ImageProviderError(
                    "AI images are disabled", code=PROVIDER_NOT_CONFIGURED, retryable=False
                )
            if not spec.refined_prompt or spec.prompt_override:
                raise ImageProviderError(
                    "AI image prompt has not been analyzed by the text model",
                    code=INVALID_REQUEST,
                    retryable=False,
                )
            image_config = context.settings.image_provider_config(provider_override)
            provider_name = image_config.provider
            provider_model = image_config.model
            provider = create_image_provider(image_config)
            image_request = ImageRequest(
                # 与 `VisualResponse.resolved_prompt` 同一个方法：确认框里
                # 展示的就是这里真正发出去的字符串。
                prompt=spec.render_prompt(),
                size=spec.size,
                quality=spec.quality,
                negative_prompt=spec.negative_prompt,
                seed=spec.seed,
            )
            generation = await _generate_image_with_compliance_retry(
                provider,
                image_request,
                runner=context.llm_runner(),
            )
            if generation.compliance_audit is not None:
                usage = {"compliance_retry": generation.compliance_audit}
            if (
                generation.first_rejection is not None
                and generation.compliance_audit is not None
                and generation.compliance_audit["retry_count"] == 1
            ):
                # 第一笔拒绝必须单独落 attempt；即使第二次成功，也不能把原始提示词、
                # 拒绝原因和实际发生过的重试覆盖掉。
                async with context.session() as session:
                    await record_visual_attempt(
                        session,
                        visual_id=visual_id,
                        provider=provider_name,
                        model=provider_model,
                        request_id=generation.first_rejection.request_id,
                        latency_ms=int((time.monotonic() - started) * 1000),
                        usage={"compliance_retry": generation.compliance_audit},
                        error_code=CONTENT_REJECTED,
                    )
            if generation.error is not None:
                raise generation.error
            if generation.generated is None:
                raise RuntimeError("image provider returned no result")
            generated = generation.generated
            provider_name = generated.provider
            provider_model = generated.model
            request_id = generated.request_id
            usage = {**generated.usage, **usage}
            result = await asyncio.to_thread(
                visuald.normalize_result,
                generated.data,
                target_size=spec.size,
            )

        visual_qa = result.provenance.get("visual_qa")
        if isinstance(visual_qa, dict) and not visual_qa.get("passed", False):
            issue_codes = ", ".join(
                str(item.get("code") or "visual_preflight_failed")
                for item in visual_qa.get("issues") or []
                if isinstance(item, dict)
            )
            raise ValueError(f"visual preflight failed: {issue_codes or 'unknown'}")

        store = make_object_store(context.settings)
        renditions: dict[str, Any] = {"_provenance": result.provenance}
        content_hash = None
        for rendition in result.renditions:
            digest = hashlib.sha256(rendition.data).hexdigest()
            content_hash = (
                digest if rendition.format == "png" or content_hash is None else content_hash
            )
            if context.owner_id is None:
                raise ValueError("project owner is unavailable")
            key = (
                f"users/{context.owner_id}/projects/{context.project_id}/"
                f"visuals/{visual_id}/v{visual.version}/"
                f"{digest}.{rendition.format}"
            )
            store.put(key, rendition.data, content_type=rendition.media_type)
            renditions[rendition.format] = {
                "object_key": key,
                "sha256": digest,
                "media_type": rendition.media_type,
                "width": rendition.width,
                "height": rendition.height,
            }
        latency_ms = int((time.monotonic() - started) * 1000)
        async with context.session() as session:
            visual = await get_visual(session, visual_id)
            if visual is None:
                raise ValueError("visual disappeared during generation")
            visual.generation_status = "ready"
            visual.provider = provider_name
            visual.model = provider_model
            visual.renditions_json = renditions
            visual.content_hash = content_hash
            await record_visual_attempt(
                session,
                visual_id=visual_id,
                provider=provider_name,
                model=provider_model,
                request_id=request_id,
                latency_ms=latency_ms,
                output_width=next((item.width for item in result.renditions if item.width), None),
                output_height=next(
                    (item.height for item in result.renditions if item.height), None
                ),
                usage=usage,
            )
        return VisualOutcome(str(visual_id), "ready", sorted(renditions.keys() - {"_provenance"}))
    except Exception as error:  # noqa: BLE001 - 失败必须落状态与 attempt
        # 裸的 `type(error).__name__` 不再允许进入 error_code：图表/示意图的失败
        # （visuald 不可达、源素材解析不唯一、对象存储写失败）此前全部以
        # `ValueError` / `ConnectError` 这类字符串落库，用户既看不懂也不知道能不能重试。
        info = classify_visual_error(error)
        code = info.code
        # provider 在失败前已读到 cf-ray / x-request-id；成功路径之外也要留痕。
        request_id = info.request_id or request_id
        latency_ms = int((time.monotonic() - started) * 1000)
        async with context.session() as session:
            visual = await get_visual(session, visual_id)
            if visual is not None:
                visual.generation_status = "failed"
                visual.error_code = code
                # 面向用户的可执行提示在前，脱敏的技术细节在后，供排查用。
                visual.error_message = (
                    f"{info.message}（{info.detail}）" if info.detail else info.message
                )[:500]
                await record_visual_attempt(
                    session,
                    visual_id=visual_id,
                    provider=provider_name,
                    model=provider_model,
                    request_id=request_id,
                    latency_ms=latency_ms,
                    usage=usage,
                    error_code=code,
                )
        return VisualOutcome(str(visual_id), "failed", [], code)
    finally:
        visuald.close()
        if provider is not None:
            provider.close()


async def _source_asset(context: JobContext, asset_ref: str):
    prefix = asset_ref.removeprefix("ua_")
    async with context.session() as session:
        assets = await list_assets(session, context.project_id)
        matches = [asset for asset in assets if str(asset.id).startswith(prefix)]
        if len(matches) != 1:
            raise ValueError("chart source asset does not resolve uniquely in this project")
        asset = await get_asset(session, matches[0].id)
        if asset is None or not isinstance(asset.parsed_json, dict):
            raise ValueError("chart source asset is not parsed")
        return asset


def _numeric_columns(headers: list[str], rows: list[list[Any]]) -> list[str]:
    result = []
    for index, header in enumerate(headers):
        values = [row[index] for row in rows if index < len(row) and row[index] not in (None, "")]
        if values and all(_is_number(value) for value in values):
            result.append(header)
    return result


def _source_hash(context: JobContext, asset: Any) -> str:
    if asset.object_key:
        try:
            data = make_object_store(context.settings).get(asset.object_key)
            return hashlib.sha256(data).hexdigest()
        except (FileNotFoundError, ValueError):
            pass
    import json

    return hashlib.sha256(
        json.dumps(asset.parsed_json or {}, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _is_number(value: Any) -> bool:
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


def _result_section_key(sections: list[Any]) -> str | None:
    preferred = ("results", "experiments", "result", "实验", "结果")
    for section in sections:
        text = f"{section.section_key} {section.title}".lower()
        if any(token in text for token in preferred):
            return section.section_key
    return (
        sections[-2].section_key
        if len(sections) >= 2
        else (sections[0].section_key if sections else None)
    )
