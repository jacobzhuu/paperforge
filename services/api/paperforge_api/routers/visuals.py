from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from copy import deepcopy
from typing import Annotated, Any, Literal

from arq.connections import ArqRedis
from db import (
    activate_visual,
    active_visual_for_slot,
    add_visual_source,
    create_visual,
    document_snapshot_hash,
    get_section,
    get_visual,
    latest_document,
    latest_successful_visual_attempts,
    list_assets,
    list_sections,
    list_visuals,
    logical_visual_slot,
    sanitize_figure_caption,
    upsert_section,
    visual_input_hash,
)
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from paper_ir import FigureBlock, PaperIR, PaperMeta
from paper_ir.schema import Section as IRSection
from sqlalchemy.ext.asyncio import AsyncSession
from storage import make_object_store
from visuals import (
    AIImageSpec,
    ChartSpec,
    image_provider_configured,
    is_retryable,
    message_for,
    normalize_code,
    parse_visual_spec,
)

from paperforge_api.concurrency import require_section_unchanged
from paperforge_api.config import get_settings
from paperforge_api.deps import (
    authorize_project_request,
    get_queue,
    get_session,
)
from paperforge_api.deps import get_authorized_project as _require_project
from paperforge_api.jobs import start_job
from paperforge_api.schemas import (
    ApproveVisualRequest,
    CreateVisualRequest,
    DraftVisualRequest,
    DraftVisualResponse,
    JobResponse,
    RegenerateVisualRequest,
    UpdateVisualRequest,
    VisualErrorResponse,
    VisualResponse,
    VisualSummaryResponse,
)

router = APIRouter(
    prefix="/api/v1", tags=["visuals"], dependencies=[Depends(authorize_project_request)]
)
SessionDep = Annotated[AsyncSession, Depends(get_session)]
QueueDep = Annotated[ArqRedis | None, Depends(get_queue)]


@router.post(
    "/projects/{project_id}/visuals/suggest",
    response_model=JobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def suggest(project_id: str, session: SessionDep, queue: QueueDep) -> JobResponse:
    project = await _require_project(session, project_id)
    _require_visuals_enabled()
    job = await start_job(
        session,
        queue,
        project_id=project.id,
        kind="visual",
        function="run_visual_suggest_pipeline",
    )
    return _job_response(job)


@router.get("/projects/{project_id}/visuals", response_model=list[VisualResponse])
async def get_visuals(
    project_id: str,
    session: SessionDep,
    kind: Literal["chart", "diagram", "ai_image"] | None = Query(default=None),
    generation_status: Literal["proposed", "queued", "running", "ready", "failed"] | None = Query(
        default=None
    ),
    review_status: Literal["pending", "approved", "rejected"] | None = Query(default=None),
) -> list[VisualResponse]:
    project = await _require_project(session, project_id)
    rows = await list_visuals(
        session,
        project.id,
        kind=kind,
        generation_status=generation_status,
        review_status=review_status,
    )
    snapshot = await _current_snapshot_hash(session, project.id)
    attempts = await latest_successful_visual_attempts(session, [row.id for row in rows])
    return [
        _visual_response(
            project.id,
            row,
            snapshot,
            generation_attempt=attempts.get(row.id),
        )
        for row in rows
    ]


@router.get("/projects/{project_id}/visuals/summary", response_model=VisualSummaryResponse)
async def visuals_summary(project_id: str, session: SessionDep) -> VisualSummaryResponse:
    """视觉状态计数。

    导航状态点、项目概览与导出提醒此前各自拉一遍完整视觉列表（含 spec 与
    renditions）才能显示一个数字。这里只回计数。

    计数在 Python 里做而不是发六条 COUNT 查询：单个项目的视觉资产是几十条量级，
    一次全表取回比六次往返更快，也让「待处理 / 可批准」这类跨两个状态字段的
    定义留在一处。
    """
    project = await _require_project(session, project_id)
    rows = await list_visuals(session, project.id)
    snapshot = await _current_snapshot_hash(session, project.id)

    # 工作台按 supersedes_id 折叠版本，汇总也必须使用同一口径。否则 v1 失败、
    # v2 已成功时，卡片显示“可批准”，项目导航却永久亮着“失败”红点。
    # 仍在正文里的旧批准版本通过 is_active 保留：用户可能正在预览一个尚未
    # 批准的 v2，此时正文中实际使用的 v1 仍应计入 approved。
    superseded_ids = {row.supersedes_id for row in rows if row.supersedes_id is not None}
    rows = [row for row in rows if row.id not in superseded_ids or row.is_active]

    summary = VisualSummaryResponse(project_id=str(project.id))
    for row in rows:
        if row.review_status == "approved":
            summary.approved += 1
            # `updated_at` 在批准那一刻被 TimestampMixin 刷新，比 created_at
            # 更接近「这张图什么时候进的正文」。
            stamp = getattr(row, "updated_at", None) or row.created_at
            if stamp and (summary.latest_approved_at is None or stamp > summary.latest_approved_at):
                summary.latest_approved_at = stamp
            if _is_stale(row, snapshot):
                summary.stale += 1
            continue
        if row.review_status == "rejected":
            summary.rejected += 1
        elif row.generation_status in {"queued", "running"}:
            summary.generating += 1
        elif row.generation_status == "failed":
            summary.failed += 1
        elif row.generation_status == "ready":
            # 已出预览、等着人点「批准并插入」。
            summary.ready += 1
        else:
            summary.pending += 1
        if _is_stale(row, snapshot):
            summary.stale += 1
    return summary


_DRAFT_PROMPT = """You draft ONE figure specification for an academic paper.
Output JSON only:
{
  "title": "short figure title",
  "caption": "figure caption in the paper's language",
  "alt_text": "accessibility description in the paper's language",
  "diagram": {"direction":"LR","nodes":[{"id":"n1","label":"..."}],
              "edges":[{"source":"n1","target":"n2"}]},
  "ai_image": {"subject":"...","composition":"...","elements":["...","..."]}
}
Rules:
- Fill ONLY the object matching the requested kind; omit the other one.
- caption and alt_text must be written in the same language as the paper title.
- For "ai_image": write what the figure shows, how it is arranged and what is in it.
  A separate step turns this brief into the final image prompt, so do not worry about
  phrasing, style keywords, or the output language here.
- For "diagram": 3-7 nodes, every edge must reference existing node ids."""

_QUANTITATIVE_INTENT = re.compile(
    r"比较|对比|趋势|变化|分布|差异|结果|性能|随.+变化|compare|comparison|trend|"
    r"distribution|difference|result|performance|over time",
    re.IGNORECASE,
)
_RELATIONAL_INTENT = re.compile(
    r"流程|步骤|机制|关系|架构|组件|分类|框架|路径|过程|flow|process|mechanism|"
    r"relationship|architecture|component|taxonomy|framework|pipeline",
    re.IGNORECASE,
)
_CONCEPTUAL_INTENT = re.compile(
    r"概念|意象|封面|插图|氛围|隐喻|concept|conceptual|illustration|metaphor|cover",
    re.IGNORECASE,
)
_ILLUSTRATED_STYLE_INTENT = re.compile(
    r"彩色|多彩|图标|icon|图标化|信息图|插画风|colorful|illustrated|infographic",
    re.IGNORECASE,
)


@router.post("/projects/{project_id}/visuals/draft", response_model=DraftVisualResponse)
async def draft_visual(
    project_id: str,
    request: DraftVisualRequest,
    session: SessionDep,
) -> DraftVisualResponse:
    """把一句话意图补成完整规格（图注 / 替代文本 / 构图）。

    只产出**草稿**：不落库、不排任务、更不会调用图像服务。用户还要看一眼、
    点「创建」，AI 插图之后还有一次生成确认。
    """
    project = await _require_project(session, project_id)
    _require_visuals_enabled()

    from llm_runtime import LLMRunner

    from paperforge_api.config import get_settings as api_settings

    document = await latest_document(session, project.id)
    rows = await list_sections(session, document.id) if document else []
    assets = await list_assets(session, project.id)
    intent = request.intent.strip() or "a figure that helps the reader follow this paper"

    target = _match_target_section(rows, request.target_section_key, intent)
    excerpt = _section_excerpt(target) if target else ""
    context_summary = _context_summary(target, excerpt)
    suggested_block_index = _suggested_block_index(target)
    chart_asset = _chart_asset(assets, request.source_asset_refs)
    settings = api_settings()
    image_config = settings.image_provider_config()
    ai_images_available = settings.ai_images_enabled and image_provider_configured(image_config)
    selected_kind, reason, warnings = _select_draft_kind(
        request.kind,
        intent,
        chart_asset=chart_asset,
        ai_images_available=ai_images_available,
    )
    target_key = target.section_key if target else request.target_section_key

    if selected_kind == "chart" and chart_asset is not None:
        return _deterministic_chart_draft(
            chart_asset,
            intent,
            target_section_key=target_key,
            suggested_block_index=suggested_block_index,
            reason=reason,
            context_summary=context_summary,
            warnings=warnings,
        )

    # “开始创作”只是创建一张可编辑、可确认的草稿，必须快速返回。此前 AI 插图
    # 会在这里串行等待“规格生成 + 提示词润色”两次文本模型调用；模型稍慢时，
    # Next.js 代理会先断开连接，用户只能看到“未知错误”，而 API 仍在后台空等。
    #
    # AI 草稿直接用论文标题、匹配章节和用户原话组成 provider-neutral 规格。用户
    # 在真正调用 Yunwu 前仍能看到并确认 render_prompt()，不会出现“确认 A、发送 B”。
    if selected_kind == "ai_image":
        base = _deterministic_draft(
            selected_kind,
            intent,
            target_section_key=target_key,
            suggested_block_index=suggested_block_index,
            reason=reason,
            context_summary=context_summary,
            warnings=warnings,
            paper_title=project.title,
            context_excerpt=excerpt,
        )
        analyzed_spec, analysis = await _analyze_ai_spec(
            project_title=project.title,
            rows=rows,
            user_intent=intent,
            spec=base.spec,
        )
        return DraftVisualResponse(
            kind="ai_image",
            title=analysis.title,
            caption=analysis.caption,
            alt_text=analysis.alt_text,
            spec=analyzed_spec.model_dump(mode="json"),
            target_section_key=target_key,
            suggested_block_index=suggested_block_index,
            reason=f"DeepSeek 已结合论文分节上下文分析本次意图；{reason}",
            context_summary=_full_paper_context_summary(project.title, rows),
            warnings=warnings,
            generator=f"llm:{analysis.model or 'deepseek'}",
        )

    section_list = "\n".join(
        f"- [{row.section_key}] {row.title}: {_section_excerpt(row)[:320]}" for row in rows[:12]
    )
    context = (
        f"Matched section content:\n{excerpt[:3000]}\n\nOther sections:\n{section_list}"
        if rows
        else "No paper body is available yet."
    )

    runner = LLMRunner(api_settings().llm_config())
    if runner.enabled:
        try:
            result = await asyncio.wait_for(
                runner.agenerate_json(
                    "planner",
                    system_prompt=_DRAFT_PROMPT,
                    user_prompt=(
                        f"Paper title: {project.title}\n"
                        f"Requested kind: {selected_kind}\n"
                        f"Target section: {target_key or 'unspecified'}\n"
                        f"Paper context:\n{context}\n\n"
                        f"What the author wants to show: {intent}"
                    ),
                    max_output_tokens=1200,
                    temperature=0.3,
                    metadata={"stage": "visual_draft"},
                ),
                timeout=8,
            )
        except Exception:
            result = None
        if result is not None and result.ok and isinstance(result.value, dict):
            drafted = _draft_from(
                result.value,
                selected_kind,
                target_section_key=target_key,
                suggested_block_index=suggested_block_index,
                reason=reason,
                context_summary=context_summary,
                warnings=warnings,
            )
            if drafted is not None:
                drafted.generator = f"llm:{result.model}"
                return drafted

    # 模型不可用时仍然给一份能提交的草稿——用户的意图原样落进描述里，
    # 而不是把他弹回一张空表单。
    return _deterministic_draft(
        selected_kind,
        intent,
        target_section_key=target_key,
        suggested_block_index=suggested_block_index,
        reason=reason,
        context_summary=context_summary,
        warnings=[*warnings, "智能规格暂不可用，已提供可编辑的确定性草稿。"],
    )


def _draft_from(
    payload: dict[str, Any],
    kind: str,
    *,
    target_section_key: str | None,
    suggested_block_index: int | None,
    reason: str,
    context_summary: str,
    warnings: list[str],
) -> DraftVisualResponse | None:
    from paperforge_worker.pipelines.visual_planner import _ai_image_spec, _diagram_spec

    caption = str(payload.get("caption") or "").strip()
    alt_text = str(payload.get("alt_text") or "").strip() or caption
    if not caption:
        return None
    spec = (
        _ai_image_spec(payload.get("ai_image"))
        if kind == "ai_image"
        else _diagram_spec(payload.get("diagram"))
    )
    if spec is None:
        return None
    return DraftVisualResponse(
        kind=kind,  # type: ignore[arg-type]
        title=str(payload.get("title") or caption)[:120],
        caption=caption,
        alt_text=alt_text,
        spec=spec,
        target_section_key=target_section_key,
        suggested_block_index=suggested_block_index,
        reason=reason,
        context_summary=context_summary,
        warnings=warnings,
    )


def _deterministic_draft(
    kind: str,
    intent: str,
    *,
    target_section_key: str | None,
    suggested_block_index: int | None,
    reason: str,
    context_summary: str,
    warnings: list[str],
    paper_title: str = "",
    context_excerpt: str = "",
) -> DraftVisualResponse:
    from visuals import AIImageSemantics, AIImageSpec, DiagramSpec

    if kind == "diagram":
        spec = DiagramSpec(
            direction="LR",
            nodes=[
                {"id": "n1", "label": "输入"},
                {"id": "n2", "label": "处理"},
                {"id": "n3", "label": "输出"},
            ],
            edges=[{"source": "n1", "target": "n2"}, {"source": "n2", "target": "n3"}],
        ).model_dump(mode="json")
        return DraftVisualResponse(
            kind="diagram",
            title=intent[:60],
            caption=intent[:200],
            alt_text=f"{intent[:150]}的示意图",
            spec=spec,
            target_section_key=target_section_key,
            suggested_block_index=suggested_block_index,
            reason=reason,
            context_summary=context_summary,
            warnings=warnings,
        )
    title_context = paper_title.strip()[:240]
    excerpt_context = " ".join(context_excerpt.split())[:500]
    subject = intent[:200]
    if intent.strip() in {"摘要图", "图形摘要", "论文摘要图", "graphical abstract"}:
        subject = (
            f"论文《{title_context}》的图形摘要" if title_context else "论文核心内容的图形摘要"
        )
    prompt_parts = [
        f"为学术论文《{title_context}》创作一张清晰、可发表的概念插图。" if title_context else "",
        f"核心表达：{intent}。",
        f"论文语境：{excerpt_context}。" if excerpt_context else "",
        "采用横向、层次清楚的图形摘要构图，使用克制配色和简洁背景，不编造数值或研究结论。",
    ]
    prompt = " ".join(part for part in prompt_parts if part)[:4000]
    try:
        spec = AIImageSpec(
            prompt=prompt,
            quality="high",
            semantics=AIImageSemantics(
                subject=subject,
                composition="横向图形摘要，核心主题居中，相关概念按清晰视觉层级展开",
                text_policy="auto",
                aspect_ratio="3:2",
            ),
        ).model_dump(mode="json")
    except ValueError:
        # 用户的意图里可能带着 URL 或代码片段。退回一句安全的通用描述，
        # 而不是把错误甩回界面。
        spec = AIImageSpec(
            prompt=(
                "A clean conceptual illustration of scientific inquiry, "
                "organic geometric forms, restrained palette"
            )
        ).model_dump(mode="json")
    return DraftVisualResponse(
        kind="ai_image",
        title=intent[:60],
        caption=intent[:200],
        alt_text=f"{intent[:150]}的概念插图",
        spec=spec,
        target_section_key=target_section_key,
        suggested_block_index=suggested_block_index,
        reason=reason,
        context_summary=context_summary,
        warnings=warnings,
    )


def _select_draft_kind(
    requested: str,
    intent: str,
    *,
    chart_asset: Any | None,
    ai_images_available: bool,
) -> tuple[str, str, list[str]]:
    warnings: list[str] = []
    if requested == "chart":
        if chart_asset is not None:
            return "chart", "已使用项目中可溯源的数值素材生成数据图表。", warnings
        warnings.append("没有找到可作图的数值素材；为避免编造数据，已改为示意图草稿。")
        return "diagram", "数据图表必须来自已解析的项目素材。", warnings
    if requested in {"diagram", "ai_image"}:
        if requested == "ai_image" and not ai_images_available:
            warnings.append("AI 插图生成当前未配置；草稿仍可保存，外部生成保持不可用。")
        return (
            requested,
            (
                "这类意图需要表达概念氛围，适合概念插图。"
                if requested == "ai_image"
                else "这类意图主要表达流程或关系，适合学术示意图。"
            ),
            warnings,
        )
    if chart_asset is not None and _QUANTITATIVE_INTENT.search(intent):
        return "chart", "检测到比较、趋势或分布意图，并找到可溯源的数值素材。", warnings
    if chart_asset is None and _QUANTITATIVE_INTENT.search(intent):
        warnings.append("没有找到可溯源的数值素材；为避免 AI 伪造数据，未自动生成数据图。")
        return "diagram", "量化图表必须来自已解析的项目素材。", warnings
    if ai_images_available:
        if _ILLUSTRATED_STYLE_INTENT.search(intent):
            return "ai_image", "检测到图标化视觉风格，将使用 AI 生成。", warnings
        if _CONCEPTUAL_INTENT.search(intent):
            return "ai_image", "检测到概念表达，将使用 AI 生成。", warnings
        if _RELATIONAL_INTENT.search(intent):
            return (
                "ai_image",
                "自动选择已采用 AI 生成可发表的概念流程插图。",
                warnings,
            )
        return (
            "ai_image",
            "自动选择已采用 AI 生成概念插图。",
            warnings,
        )
    if _RELATIONAL_INTENT.search(intent):
        return "diagram", "AI 生成当前不可用，已改用本地直出示意图。", warnings
    return "diagram", "未发现需要精确数据的信号，先用可编辑的示意图表达结构。", warnings


def _chart_asset(assets: list[Any], requested_refs: list[str]) -> Any | None:
    requested = {value.removeprefix("ua_") for value in requested_refs}
    for asset in assets:
        if requested and not any(str(asset.id).startswith(prefix) for prefix in requested):
            continue
        parsed = asset.parsed_json if isinstance(asset.parsed_json, dict) else {}
        headers = [str(value) for value in parsed.get("headers") or []]
        rows = parsed.get("rows") or []
        if len(headers) >= 2 and rows and _numeric_columns(headers, rows):
            return asset
    return None


def _numeric_columns(headers: list[str], rows: list[Any]) -> list[str]:
    numeric: list[str] = []
    for index, header in enumerate(headers):
        values = [row[index] for row in rows[:100] if isinstance(row, list) and index < len(row)]
        present = [value for value in values if value is not None and value != ""]
        if present and sum(_is_number(value) for value in present) / len(present) >= 0.8:
            numeric.append(header)
    return numeric


def _is_number(value: Any) -> bool:
    try:
        float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return False
    return True


def _deterministic_chart_draft(
    asset: Any,
    intent: str,
    *,
    target_section_key: str | None,
    suggested_block_index: int | None,
    reason: str,
    context_summary: str,
    warnings: list[str],
) -> DraftVisualResponse:
    parsed = asset.parsed_json
    headers = [str(value) for value in parsed.get("headers") or []]
    rows = parsed.get("rows") or []
    numeric = _numeric_columns(headers, rows)
    x = next((header for header in headers if header not in numeric), headers[0])
    y = next((header for header in numeric if header != x), numeric[0])
    chart_type = "line" if re.search(r"趋势|变化|随|trend|over time", intent, re.I) else "bar"
    spec = ChartSpec(
        chart_type=chart_type,
        source_asset_ref=f"ua_{str(asset.id)[:8]}",
        x=x,
        y=[y],
        x_label=x,
        y_label=y,
    ).model_dump(mode="json")
    return DraftVisualResponse(
        kind="chart",
        title=(intent or f"{asset.title or '数据素材'}可视化")[:120],
        caption=f"基于「{asset.title or '数据素材'}」展示 {y} 与 {x} 的关系",
        alt_text=f"以 {x} 为横轴、{y} 为纵轴的{('折线' if chart_type == 'line' else '柱状')}图",
        spec=spec,
        target_section_key=target_section_key,
        suggested_block_index=suggested_block_index,
        reason=reason,
        context_summary=context_summary,
        warnings=warnings,
        generator="deterministic",
    )


def _match_target_section(rows: list[Any], requested_key: str | None, intent: str) -> Any | None:
    if requested_key:
        exact = next((row for row in rows if row.section_key == requested_key), None)
        if exact is not None:
            return exact
    tokens = _intent_tokens(intent)
    if not rows or not tokens:
        return rows[0] if rows else None
    return max(
        rows,
        key=lambda row: sum(
            3 if token in str(row.title).casefold() else 1
            for token in tokens
            if token in f"{row.title} {_section_excerpt(row)}".casefold()
        ),
    )


def _intent_tokens(value: str) -> set[str]:
    latin = {token.casefold() for token in re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", value)}
    # 中文没有空格，按整段截取会把“展示根系断裂机制”变成一个无法命中正文的长词。
    # 取 2–4 字 n-gram，让“根系 / 断裂 / 防御方法”能稳定参与章节匹配。
    cjk: set[str] = set()
    for chunk in re.findall(r"[\u4e00-\u9fff]+", value):
        for size in (2, 3, 4):
            cjk.update(chunk[index : index + size] for index in range(len(chunk) - size + 1))
    return latin | cjk


def _section_excerpt(row: Any | None) -> str:
    if row is None:
        return ""
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


def _full_paper_context(project_title: str, rows: list[Any]) -> str:
    """Build the same section-balanced context used by background visual planning."""
    from paperforge_worker.pipelines.image_prompt import build_paper_context

    return build_paper_context(
        project_title,
        [
            (
                str(getattr(row, "section_key", "") or ""),
                str(getattr(row, "title", "") or ""),
                _section_excerpt(row),
            )
            for row in rows
        ],
    )


def _full_paper_context_summary(project_title: str, rows: list[Any]) -> str:
    if not rows:
        return "DeepSeek 已读取当前项目题目；论文正文尚为空。"
    source_characters = sum(len(_section_excerpt(row)) for row in rows)
    context = _full_paper_context(project_title, rows)
    sent_characters = len(context)
    if "middle content omitted" not in context:
        return f"DeepSeek 已读取当前论文全部 {len(rows)} 个章节（约 {source_characters} 字符）。"
    return (
        f"DeepSeek 已读取全部 {len(rows)} 个章节的均衡上下文"
        f"（原文约 {source_characters} 字符，本次发送 {sent_characters} 字符）。"
    )


def _image_intent_from_spec(spec: dict[str, Any], fallback: str = "") -> str:
    semantics = spec.get("semantics")
    semantics = semantics if isinstance(semantics, dict) else {}
    elements = semantics.get("elements") if isinstance(semantics.get("elements"), list) else []
    parts = [
        str(spec.get("prompt_override") or "").strip(),
        str(semantics.get("subject") or "").strip(),
        str(semantics.get("composition") or "").strip(),
        "；".join(str(item).strip() for item in elements if str(item).strip()),
        str(spec.get("style") or "").strip(),
        str(spec.get("prompt") or "").strip(),
        fallback.strip(),
    ]
    return "\n".join(part for part in parts if part)


async def _analyze_ai_spec(
    *,
    project_title: str,
    rows: list[Any],
    user_intent: str,
    spec: dict[str, Any],
) -> tuple[AIImageSpec, Any]:
    """强制通过 DeepSeek 论文上下文分析；失败时阻止未润色提示词进入 Yunwu。"""
    from llm_runtime import LLMRunner
    from paperforge_worker.pipelines.image_prompt import analyze_image_prompt

    runner = LLMRunner(get_settings().llm_config())
    analysis = await analyze_image_prompt(
        user_intent=user_intent,
        full_paper=_full_paper_context(project_title, rows),
        runner=runner,
        current_spec=spec,
    )
    if analysis is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "deepseek_image_prompt_failed",
                "message": (
                    "DeepSeek 未能完成论文上下文分析，请稍后重试；"
                    "系统不会把未分析的提示词直接发送给生图服务。"
                ),
            },
        )
    payload = deepcopy(spec)
    payload["prompt"] = analysis.prompt
    payload["refined_prompt"] = analysis.prompt
    payload.pop("prompt_override", None)
    payload["quality"] = payload.get("quality") or "high"
    payload["semantics"] = {
        "subject": analysis.subject,
        "composition": analysis.composition,
        "elements": list(analysis.elements),
        "text_policy": analysis.text_policy,
        "aspect_ratio": "3:2",
    }
    try:
        return AIImageSpec.model_validate(payload), analysis
    except ValueError as error:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "deepseek_image_prompt_invalid",
                "message": "DeepSeek 返回的生图规格未通过安全校验，请重试。",
            },
        ) from error


def _context_summary(row: Any | None, excerpt: str) -> str:
    if row is None:
        return "尚无正文，草稿仅依据论文题目与当前意图。"
    compact = " ".join(excerpt.split())
    return f"已匹配章节「{row.title}」" + (f"：{compact[:240]}" if compact else "（正文为空）")


def _suggested_block_index(row: Any | None) -> int | None:
    if row is None or not isinstance(row.body_ir_json, dict):
        return None
    return len(row.body_ir_json.get("blocks") or [])


@router.post(
    "/projects/{project_id}/visuals",
    response_model=VisualResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_visual_endpoint(
    project_id: str,
    request: CreateVisualRequest,
    session: SessionDep,
) -> VisualResponse:
    project = await _require_project(session, project_id)
    _require_visuals_enabled()
    try:
        spec = parse_visual_spec(request.spec)
    except Exception as error:  # noqa: BLE001 - pydantic contract error -> 422
        raise HTTPException(status_code=422, detail=str(error)) from error
    document = await latest_document(session, project.id)
    rows = await list_sections(session, document.id) if document else []
    snapshot = document_snapshot_hash(rows) if rows else None
    visual = await create_visual(
        session,
        project_id=project.id,
        kind=spec.kind,
        spec=spec.model_dump(mode="json"),
        title=request.title,
        caption=request.caption,
        alt_text=request.alt_text,
        target_section_key=request.target_section_key,
        suggested_block_index=request.suggested_block_index,
        document_version=document.version if document else None,
        # 只有经过 DeepSeek 的提示词才能声明自己绑定了当前全文；未润色的直接
        # API 草稿会在生成前由 prepare 端点补分析。
        paper_snapshot_hash=snapshot
        if isinstance(spec, AIImageSpec) and spec.refined_prompt
        else None,
    )
    if isinstance(spec, ChartSpec):
        asset, digest = await _resolve_chart_source(session, project.id, spec.source_asset_ref)
        await add_visual_source(session, visual_id=visual.id, user_asset=asset, source_hash=digest)
    return _visual_response(project.id, visual)


@router.patch("/projects/{project_id}/visuals/{visual_id}", response_model=VisualResponse)
async def update_visual_endpoint(
    project_id: str,
    visual_id: str,
    request: UpdateVisualRequest,
    session: SessionDep,
) -> VisualResponse:
    project = await _require_project(session, project_id)
    visual = await _require_visual(session, project.id, visual_id)
    if visual.review_status != "pending":
        raise HTTPException(status_code=409, detail="approved or rejected visual is immutable")
    sent = request.model_fields_set
    if "spec" in sent:
        if visual.generation_status not in {"proposed", "failed"}:
            raise HTTPException(status_code=409, detail="use regenerate after a preview exists")
        if request.spec is None:
            raise HTTPException(status_code=422, detail="spec cannot be null")
        if request.spec.get("kind") == "ai_image":
            document = await latest_document(session, project.id)
            rows = await list_sections(session, document.id) if document else []
            spec, analysis = await _analyze_ai_spec(
                project_title=project.title,
                rows=rows,
                user_intent=_image_intent_from_spec(request.spec, visual.caption),
                spec=request.spec,
            )
            if "caption" not in sent:
                visual.caption = sanitize_figure_caption(analysis.caption)
            if "alt_text" not in sent:
                visual.alt_text = analysis.alt_text
            visual.paper_snapshot_hash = document_snapshot_hash(rows) if rows else None
        else:
            try:
                spec = parse_visual_spec(request.spec)
            except Exception as error:  # noqa: BLE001
                raise HTTPException(status_code=422, detail=str(error)) from error
        if spec.kind != visual.kind:
            raise HTTPException(status_code=409, detail="visual kind cannot change")
        if isinstance(spec, ChartSpec):
            current = ChartSpec.model_validate(visual.spec_json)
            if spec.source_asset_ref != current.source_asset_ref:
                raise HTTPException(
                    status_code=409,
                    detail="use regenerate when changing a chart source asset",
                )
        visual.spec_json = spec.model_dump(mode="json")
        visual.input_hash = visual_input_hash(visual.spec_json)
        visual.generation_status = "proposed"
        visual.error_code = None
        visual.error_message = None
    for field in (
        "title",
        "caption",
        "alt_text",
        "target_section_key",
        "suggested_block_index",
    ):
        if field in sent:
            value = getattr(request, field)
            if field == "caption" and value is not None:
                value = sanitize_figure_caption(value)
            setattr(visual, field, value)
    await session.flush()
    return _visual_response(project.id, visual)


@router.post(
    "/projects/{project_id}/visuals/{visual_id}/prepare-generation",
    response_model=VisualResponse,
)
async def prepare_ai_generation(
    project_id: str,
    visual_id: str,
    session: SessionDep,
) -> VisualResponse:
    """为旧草稿或正文已变化的草稿补做 DeepSeek 论文上下文分析。

    前端在打开付费确认框前调用；这样确认框里展示的已经是 DeepSeek 结合当前分节上下文
    生成的最终提示词，而不是在用户确认以后再偷偷改写。
    """
    project = await _require_project(session, project_id)
    _require_visuals_enabled()
    visual = await _require_visual(session, project.id, visual_id)
    if visual.kind != "ai_image":
        raise HTTPException(status_code=409, detail="only AI images require prompt preparation")
    if visual.review_status != "pending" or visual.generation_status not in {"proposed", "failed"}:
        raise HTTPException(
            status_code=409, detail="visual cannot be prepared in its current state"
        )

    document = await latest_document(session, project.id)
    rows = await list_sections(session, document.id) if document else []
    snapshot = document_snapshot_hash(rows) if rows else None
    current = AIImageSpec.model_validate(visual.spec_json)
    already_current = (
        bool(current.refined_prompt)
        and not current.prompt_override
        and visual.paper_snapshot_hash == snapshot
    )
    if not already_current:
        analyzed, analysis = await _analyze_ai_spec(
            project_title=project.title,
            rows=rows,
            user_intent=_image_intent_from_spec(visual.spec_json, visual.caption),
            spec=visual.spec_json,
        )
        visual.spec_json = analyzed.model_dump(mode="json")
        visual.input_hash = visual_input_hash(visual.spec_json)
        visual.caption = sanitize_figure_caption(analysis.caption)
        visual.alt_text = analysis.alt_text
        visual.paper_snapshot_hash = snapshot
        visual.generation_status = "proposed"
        visual.error_code = None
        visual.error_message = None
        await session.flush()
    return _visual_response(project.id, visual, snapshot)


@router.post(
    "/projects/{project_id}/visuals/{visual_id}/generate",
    response_model=JobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def generate(
    project_id: str,
    visual_id: str,
    session: SessionDep,
    queue: QueueDep,
) -> JobResponse:
    project = await _require_project(session, project_id)
    _require_visuals_enabled()
    visual = await _require_visual(session, project.id, visual_id)
    if visual.review_status != "pending" or visual.generation_status in {"queued", "running"}:
        raise HTTPException(
            status_code=409, detail="visual cannot be generated in its current state"
        )
    if visual.kind == "ai_image" and not get_settings().ai_images_enabled:
        raise HTTPException(status_code=409, detail="AI image generation is disabled")
    if visual.kind == "ai_image":
        spec = AIImageSpec.model_validate(visual.spec_json)
        snapshot = await _current_snapshot_hash(session, project.id)
        if (
            not spec.refined_prompt
            or spec.prompt_override
            or visual.paper_snapshot_hash != snapshot
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "ai_prompt_requires_deepseek",
                    "message": "请先让 DeepSeek 结合当前论文全文生成最终提示词。",
                },
            )
    visual.generation_status = "queued"
    job = await start_job(
        session,
        queue,
        project_id=project.id,
        kind="visual",
        function="run_visual_generate_pipeline",
        visual_id=str(visual.id),
    )
    return _job_response(job)


@router.post("/projects/{project_id}/visuals/{visual_id}/approve", response_model=VisualResponse)
async def approve(
    project_id: str,
    visual_id: str,
    request: ApproveVisualRequest,
    session: SessionDep,
) -> VisualResponse:
    project = await _require_project(session, project_id)
    visual = await _require_visual(session, project.id, visual_id)
    if visual.review_status == "approved":
        return _visual_response(project.id, visual)
    if visual.review_status != "pending" or visual.generation_status != "ready":
        raise HTTPException(status_code=409, detail="only ready pending visuals can be approved")
    if not visual.caption.strip() or not visual.alt_text.strip():
        raise HTTPException(status_code=422, detail="caption and alt_text are required")
    document = await latest_document(session, project.id)
    if document is None:
        raise HTTPException(status_code=409, detail="generate the paper before inserting visuals")
    rows = await list_sections(session, document.id)
    target = await get_section(session, document_id=document.id, section_key=request.section_key)
    if target is None:
        raise HTTPException(status_code=404, detail="target section not found")
    require_section_unchanged(target, request.expected_section_updated_at)

    sections = [IRSection(**row.body_ir_json) for row in rows if row.body_ir_json]
    asset_ref = f"va_{str(visual.id)[:8]}"
    slot_key = logical_visual_slot(request.section_key, request.block_index, visual.figure_label)
    active_in_slot = await active_visual_for_slot(
        session,
        project_id=project.id,
        logical_slot_key=slot_key,
    )
    if any(
        isinstance(block, FigureBlock) and block.asset_ref == asset_ref
        for section in sections
        for block in section.blocks
    ):
        visual.review_status = "approved"
        visual.caption = sanitize_figure_caption(visual.caption)
        await activate_visual(
            session,
            visual,
            section_key=request.section_key,
            block_index=request.block_index,
        )
        return _visual_response(project.id, visual)

    replacement_id = (
        active_in_slot.id
        if active_in_slot is not None and active_in_slot.id != visual.id
        else visual.supersedes_id
    )
    replacement_ref = f"va_{str(replacement_id)[:8]}" if replacement_id else None
    target_ir = next((section for section in sections if section.key == target.section_key), None)
    if target_ir is None:
        raise HTTPException(status_code=422, detail="target section has invalid or missing IR")
    replaced = False
    if replacement_ref:
        for section in sections:
            for index, block in enumerate(section.blocks):
                if isinstance(block, FigureBlock) and block.asset_ref == replacement_ref:
                    section.blocks[index] = _figure_block(visual, asset_ref)
                    target_ir = section
                    target = next(row for row in rows if row.section_key == section.key)
                    replaced = True
                    break
            if replaced:
                break
    if not replaced:
        if request.block_index < 0 or request.block_index > len(target_ir.blocks):
            raise HTTPException(status_code=422, detail="block_index is outside the section")
        target_ir.blocks.insert(request.block_index, _figure_block(visual, asset_ref))

    try:
        whole = PaperIR(meta=PaperMeta(title=project.title), sections=sections)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    target_ir = next(section for section in whole.sections if section.key == target.section_key)
    section_only = PaperIR(meta=PaperMeta(title=project.title), sections=[target_ir])
    await upsert_section(
        session,
        document_id=document.id,
        section_key=target.section_key,
        title=target.title,
        order_no=target.order_no,
        body_ir=target_ir.model_dump(mode="json"),
        cite_keys=sorted(section_only.collect_cite_keys()),
        asset_refs=sorted(section_only.collect_asset_refs()),
        parent_key=target.parent_key,
        status="edited",
        model=target.model,
    )
    visual.caption = sanitize_figure_caption(visual.caption)
    visual.review_status = "approved"
    visual.target_section_key = target.section_key
    visual.suggested_block_index = request.block_index
    await activate_visual(
        session,
        visual,
        section_key=target.section_key,
        block_index=request.block_index,
    )
    return _visual_response(project.id, visual)


@router.post("/projects/{project_id}/visuals/{visual_id}/reject", response_model=VisualResponse)
async def reject(project_id: str, visual_id: str, session: SessionDep) -> VisualResponse:
    project = await _require_project(session, project_id)
    visual = await _require_visual(session, project.id, visual_id)
    if visual.review_status == "approved":
        raise HTTPException(status_code=409, detail="approved visual cannot be rejected")
    visual.review_status = "rejected"
    await session.flush()
    return _visual_response(project.id, visual)


@router.post(
    "/projects/{project_id}/visuals/{visual_id}/regenerate",
    response_model=VisualResponse,
    status_code=status.HTTP_201_CREATED,
)
async def regenerate(
    project_id: str,
    visual_id: str,
    request: RegenerateVisualRequest,
    session: SessionDep,
) -> VisualResponse:
    project = await _require_project(session, project_id)
    old = await _require_visual(session, project.id, visual_id)
    payload = deepcopy(request.spec or old.spec_json)
    analysis = None
    document = await latest_document(session, project.id)
    rows = await list_sections(session, document.id) if document else []
    snapshot = document_snapshot_hash(rows) if rows else None
    if old.kind == "ai_image":
        user_intent = (
            request.revision_instruction.strip()
            if request.revision_instruction and request.revision_instruction.strip()
            else _image_intent_from_spec(payload, old.caption)
        )
        spec, analysis = await _analyze_ai_spec(
            project_title=project.title,
            rows=rows,
            user_intent=user_intent,
            spec=payload,
        )
    elif request.revision_instruction and request.revision_instruction.strip():
        payload = _apply_revision_instruction(
            payload,
            old.kind,
            request.revision_instruction.strip(),
        )
        try:
            spec = parse_visual_spec(payload)
        except Exception as error:  # noqa: BLE001
            raise HTTPException(status_code=422, detail=str(error)) from error
    else:
        try:
            spec = parse_visual_spec(payload)
        except Exception as error:  # noqa: BLE001
            raise HTTPException(status_code=422, detail=str(error)) from error
    if spec.kind != old.kind:
        raise HTTPException(status_code=409, detail="visual kind cannot change across revisions")
    new = await create_visual(
        session,
        project_id=project.id,
        kind=old.kind,
        spec=spec.model_dump(mode="json"),
        title=old.title,
        caption=(
            request.caption
            if request.caption is not None
            else analysis.caption
            if analysis is not None
            else old.caption
        ),
        alt_text=(
            request.alt_text
            if request.alt_text is not None
            else analysis.alt_text
            if analysis is not None
            else old.alt_text
        ),
        target_section_key=old.target_section_key,
        suggested_block_index=old.suggested_block_index,
        document_version=document.version if document else old.document_version,
        version=old.version + 1,
        supersedes_id=old.id,
        figure_label=old.figure_label,
        paper_snapshot_hash=snapshot if isinstance(spec, AIImageSpec) else old.paper_snapshot_hash,
    )
    if isinstance(spec, ChartSpec):
        asset, digest = await _resolve_chart_source(session, project.id, spec.source_asset_ref)
        await add_visual_source(session, visual_id=new.id, user_asset=asset, source_hash=digest)
    return _visual_response(project.id, new)


def _apply_revision_instruction(
    payload: dict[str, Any], kind: str, instruction: str
) -> dict[str, Any]:
    """Apply common natural-language revisions while preserving deterministic data boundaries."""
    lowered = instruction.casefold()
    if kind == "diagram":
        if re.search(r"横向|从左到右|horizontal|left.to.right", lowered):
            payload["direction"] = "LR"
        elif re.search(r"纵向|从上到下|vertical|top.to.bottom", lowered):
            payload["direction"] = "TB"
        if re.search(r"减少节点|更简洁|精简|fewer nodes|simpl", lowered):
            nodes = payload.get("nodes") if isinstance(payload.get("nodes"), list) else []
            if len(nodes) > 4:
                keep = nodes[:3] + nodes[-1:]
                ids = {node.get("id") for node in keep if isinstance(node, dict)}
                payload["nodes"] = keep
                payload["edges"] = [
                    edge
                    for edge in (payload.get("edges") or [])
                    if isinstance(edge, dict)
                    and edge.get("source") in ids
                    and edge.get("target") in ids
                ]
        emphasis = re.search(
            r"(?:突出|强调|emphasize|highlight)\s*[：:]?\s*([^，,。.;]{1,40})",
            instruction,
            re.I,
        )
        if emphasis:
            term = emphasis.group(1).strip().casefold()
            for node in payload.get("nodes") or []:
                if isinstance(node, dict) and term in str(node.get("label") or "").casefold():
                    node["shape"] = "diamond"
    elif kind == "chart":
        if re.search(r"趋势|折线|line|trend", lowered):
            payload["chart_type"] = "line"
        elif re.search(r"比较|柱状|bar|compare", lowered):
            payload["chart_type"] = "bar"
        if re.search(r"黑白|灰度|grayscale|monochrome", lowered):
            payload["palette"] = "grayscale"
        if re.search(r"通栏|更宽|full.width|wide", lowered):
            payload["width"] = "full"
        # Deliberately never change source_asset_ref/x/y or any data value from prose.
    return payload


@router.get("/projects/{project_id}/visuals/{visual_id}/renditions/{format}")
async def rendition(
    project_id: str,
    visual_id: str,
    format: str,
    session: SessionDep,
    disposition: str = "inline",
) -> Response:
    project = await _require_project(session, project_id)
    visual = await _require_visual(session, project.id, visual_id)
    if format not in {"svg", "pdf", "png"}:
        raise HTTPException(status_code=404, detail="rendition not found")
    item = (visual.renditions_json or {}).get(format)
    if not isinstance(item, dict) or not item.get("object_key"):
        raise HTTPException(status_code=404, detail="rendition not found")
    store = make_object_store(get_settings())
    try:
        content = store.get(item["object_key"])
    except (FileNotFoundError, ValueError) as error:
        raise HTTPException(status_code=410, detail="rendition payload is gone") from error
    media_type = {"svg": "image/svg+xml", "pdf": "application/pdf", "png": "image/png"}[format]
    mode = "attachment" if disposition == "attachment" else "inline"
    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": f'{mode}; filename="visual-{visual.id}.{format}"',
            "Cache-Control": "private, max-age=3600",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _figure_block(visual: Any, asset_ref: str) -> FigureBlock:
    width = visual.spec_json.get("width", "column")
    return FigureBlock(
        asset_ref=asset_ref,
        caption=visual.caption,
        alt_text=visual.alt_text,
        label=visual.figure_label,
        width=width,
    )


async def _resolve_chart_source(
    session: AsyncSession, project_id: uuid.UUID, asset_ref: str
) -> tuple[Any, str]:
    prefix = asset_ref.removeprefix("ua_")
    matches = [
        asset
        for asset in await list_assets(session, project_id)
        if str(asset.id).startswith(prefix)
    ]
    if len(matches) != 1 or not isinstance(matches[0].parsed_json, dict):
        raise HTTPException(
            status_code=422, detail="chart source does not resolve to one parsed project asset"
        )
    asset = matches[0]
    if not asset.parsed_json.get("headers") or not asset.parsed_json.get("rows"):
        raise HTTPException(status_code=422, detail="chart source must be a parsed CSV/XLSX table")
    if asset.object_key:
        try:
            source = make_object_store(get_settings()).get(asset.object_key)
        except (FileNotFoundError, ValueError) as error:
            raise HTTPException(status_code=410, detail="chart source payload is gone") from error
    else:
        source = json.dumps(asset.parsed_json, sort_keys=True).encode()
    return asset, hashlib.sha256(source).hexdigest()


async def _current_snapshot_hash(session: AsyncSession, project_id: uuid.UUID) -> str | None:
    """当前正文指纹。没有文稿时返回 None——此时任何建议都谈不上「过期」。"""
    document = await latest_document(session, project_id)
    if document is None:
        return None
    return document_snapshot_hash(await list_sections(session, document.id))


def _is_stale(visual: Any, snapshot: str | None) -> bool:
    """建议是否基于旧版正文。

    只有两边都有指纹才判定。历史行没有 `paper_snapshot_hash`，它们一律**不**被
    标为过期——把「无从判断」显示成「已过期」会让用户去重做一批其实没问题的图。
    """
    recorded = getattr(visual, "paper_snapshot_hash", None)
    if not recorded or not snapshot:
        return False
    return recorded != snapshot


def _visual_error(visual: Any) -> VisualErrorResponse | None:
    """把落库的 code 翻成统一词表 + 可执行提示。

    历史行里存的是旧词表（`auth` / `moderation` / 甚至裸的 `ValueError`），
    `normalize_code` 在读侧统一映射，界面因此只需要认识一套 code。
    """
    if not visual.error_code:
        return None
    code = normalize_code(visual.error_code)
    stored = (visual.error_message or "").strip()
    canonical = message_for(code)
    return VisualErrorResponse(
        code=code,
        message=canonical,
        retryable=is_retryable(code),
        request_id=_latest_request_id(visual),
        # 落库的 message 已是「可执行提示（技术细节）」格式；与规范文案相同时
        # 不重复展示。
        detail=stored if stored and stored != canonical else None,
    )


def _latest_request_id(visual: Any) -> str | None:
    """最近一次尝试的 request id。

    `renditions_json['_provenance']` 是成功路径留下的；失败路径的 request id
    落在 `visual_generation_attempt` 上，列表接口不做联表，这里只取 provenance。
    """
    provenance = (visual.renditions_json or {}).get("_provenance")
    if isinstance(provenance, dict):
        value = provenance.get("request_id")
        if isinstance(value, str):
            return value
    return None


def _output_size(visual: Any) -> tuple[int | None, int | None]:
    """实际输出尺寸。

    Cloudflare 根本不接受尺寸参数，用户需要看到的是**真实拿到了什么**，
    而不是他当初在下拉框里选了什么。优先取 png，其次任意一个有尺寸的 rendition。
    """
    renditions = visual.renditions_json or {}
    for key in ("png", "svg", "pdf"):
        item = renditions.get(key)
        if isinstance(item, dict) and item.get("width") and item.get("height"):
            return int(item["width"]), int(item["height"])
    return None, None


def _resolved_prompt(spec: dict[str, Any]) -> str | None:
    """AI 插图最终下发的提示词。非 AI 图或 spec 不合法时返回 None。"""
    if spec.get("kind") != "ai_image":
        return None
    try:
        parsed = parse_visual_spec(spec)
    except Exception:  # noqa: BLE001 - 展示用途，解析失败不应让整个列表 500
        return None
    render = getattr(parsed, "render_prompt", None)
    return render() if callable(render) else None


def _visual_response(
    project_id: uuid.UUID,
    visual: Any,
    snapshot: str | None = None,
    *,
    generation_attempt: Any | None = None,
) -> VisualResponse:
    renditions = {}
    for fmt, item in (visual.renditions_json or {}).items():
        if fmt not in {"svg", "pdf", "png"} or not isinstance(item, dict):
            continue
        renditions[fmt] = {
            **item,
            "url": f"/api/v1/projects/{project_id}/visuals/{visual.id}/renditions/{fmt}",
        }
    width, height = _output_size(visual)
    return VisualResponse(
        id=str(visual.id),
        asset_ref=f"va_{str(visual.id)[:8]}",
        kind=visual.kind,
        generation_status=visual.generation_status,
        review_status=visual.review_status,
        title=visual.title,
        caption=visual.caption,
        caption_hint=_chart_caption_hint(visual.spec_json),
        alt_text=visual.alt_text,
        target_section_key=visual.target_section_key,
        suggested_block_index=visual.suggested_block_index,
        figure_label=visual.figure_label,
        spec=visual.spec_json,
        provider=visual.provider or getattr(generation_attempt, "provider", None),
        model=visual.model or getattr(generation_attempt, "model", None),
        error_code=visual.error_code,
        error_message=visual.error_message,
        error=_visual_error(visual),
        renditions=renditions,
        input_hash=visual.input_hash,
        content_hash=visual.content_hash,
        version=visual.version,
        supersedes_id=str(visual.supersedes_id) if visual.supersedes_id else None,
        output_width=width,
        output_height=height,
        resolved_prompt=_resolved_prompt(visual.spec_json),
        paper_snapshot_hash=getattr(visual, "paper_snapshot_hash", None),
        suggestion_reason=getattr(visual, "suggestion_reason", None),
        source_section_keys=list(getattr(visual, "source_section_keys", None) or []),
        stale=_is_stale(visual, snapshot),
        generated_at=getattr(generation_attempt, "created_at", None),
        created_at=visual.created_at,
    )


def _chart_caption_hint(spec: dict[str, Any]) -> str | None:
    if spec.get("kind") != "chart":
        return None
    notes: list[str] = []
    filters = spec.get("filters")
    if isinstance(filters, list) and filters:
        notes.append(f"已显式应用 {len(filters)} 个过滤条件")
    aggregation = spec.get("aggregation")
    if aggregation and aggregation != "none":
        notes.append(f"按 {aggregation} 聚合")
    sort = spec.get("sort")
    if sort and sort != "none":
        notes.append("横轴升序排列" if sort == "asc" else "横轴降序排列")
    if not notes:
        return None
    return "建议在图注中说明数据处理：" + "；".join(notes) + "。"


async def _require_visual(session: AsyncSession, project_id: uuid.UUID, visual_id: str):
    try:
        visual_uuid = uuid.UUID(visual_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail="visual not found") from error
    visual = await get_visual(session, visual_uuid)
    if visual is None or visual.project_id != project_id:
        raise HTTPException(status_code=404, detail="visual not found")
    return visual


def _require_visuals_enabled() -> None:
    if not get_settings().visuals_enabled:
        raise HTTPException(status_code=404, detail="visual generation is disabled")


def _job_response(job: Any) -> JobResponse:
    return JobResponse(
        id=str(job.id),
        project_id=str(job.project_id),
        kind=job.kind,
        status=job.status,
        stage=job.stage,
        progress=job.progress,
        checkpoint=job.checkpoint_json,
        error=job.error_json,
        created_at=job.created_at,
        finished_at=job.finished_at,
    )
