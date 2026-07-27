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
    AIImageSpec,
    ChartSpec,
    DiagramSpec,
    ImageProvider,
    ImageProviderConfig,
    ImageProviderError,
    ImageRequest,
    VisualdClient,
    classify_visual_error,
    create_image_provider,
    parse_visual_spec,
)
from visuals.errors import PROVIDER_NOT_CONFIGURED

from paperforge_worker.context import JobContext
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
            "generator": self.generator,
        }


async def suggest_visuals(context: JobContext) -> VisualPlanOutcome:
    """提出最多 6 条建议；**绝不**调用 ImageProvider 或修改章节 IR。

    两条来源：
      - 数据图表由已上传的结果表格确定性推导——模型不得凭空造数据图，
        图表的每个数字都必须能追回某一份素材；
      - 示意图与 AI 插图交给结构化规划器（`visual_planner`）。模型不可用、
        超时或输出不合法时，回退到「论文结构概览 + 概念插图」这套原有的
        确定性建议，`visual_plan` 永远有产物。

    规划调用**会**把章节摘要发给文本模型（记入 llm_call_log），但这条路径上
    结构上不存在任何 ImageProvider 调用——生图只可能由用户显式确认后触发。
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
        # 建议依据的正文指纹：正文一改，界面就能提示「建议基于旧版正文」。
        snapshot = document_snapshot_hash(sections) if sections else None
        document_version = document.version if document else None
        project_title = project.title if project is not None else "the research topic"
        paper_type = project.paper_type if project is not None else "review"

        chart_proposals = _chart_proposals(assets, sections)
        body_sections = [row for row in sections if row.section_key != "abstract"][:6]
        briefs = [
            SectionBrief(
                key=row.section_key,
                title=row.title,
                excerpt=_section_excerpt(row),
            )
            for row in body_sections
        ]
        short_review = _is_short_review(paper_type, briefs)

    # 第二段：规划。allow_ai_images 只控制**是否提出**插图建议——即使允许，
    # 也仍然只是 proposal，生图要用户再确认一次。
    planned, generator = await plan_visuals(
        sections=briefs,
        runner=context.llm_runner(),
        allow_ai_images=context.settings.ai_images_enabled,
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
        ai_images = 0
        for spec, metadata in proposals[:MAX_PROPOSALS]:
            if visual_input_hash(spec) in existing_hashes:
                # 同一条建议已经提过。哈希走语义投影，因此给 spec 加可选字段
                # 不会让历史资产失效、被重复提出。
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
    return VisualPlanOutcome(
        proposed=proposed,
        chart_count=counts["chart"],
        diagram_count=counts["diagram"],
        ai_image_count=counts["ai_image"],
        preview_ready_count=sum(item.status == "ready" for item in previews),
        preview_failed_count=sum(item.status == "failed" for item in previews),
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
                f"A clean academic conceptual illustration representing {project_title}, "
                "abstract scientific forms, no text and no quantitative content"
            )
        ).model_dump(mode="json")
    except ValueError:
        # 项目标题本身可能包含“准确率”等词；它不能绕过 AI 图禁区，
        # 也不应让一条可选建议拖垮整个 visual_plan。
        ai_spec = AIImageSpec(
            prompt=(
                "A clean abstract academic illustration of scientific inquiry and "
                "collaboration, organic geometric forms, no text or quantitative content"
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
            provider_name = context.settings.image_provider.strip().lower()
            provider_model = context.settings.image_model
            provider = create_image_provider(
                ImageProviderConfig(
                    provider=provider_name,
                    api_key=context.settings.image_api_key,
                    model=context.settings.image_model,
                    base_url=context.settings.image_base_url,
                    account_id=context.settings.image_account_id,
                    timeout_seconds=context.settings.image_timeout_seconds,
                    max_retries=context.settings.image_max_retries,
                )
            )
            generated = await asyncio.to_thread(
                provider.generate,
                ImageRequest(
                    # 与 `VisualResponse.resolved_prompt` 同一个方法：确认框里
                    # 展示的就是这里真正发出去的字符串。
                    prompt=spec.render_prompt(),
                    size=spec.size,
                    quality=spec.quality,
                ),
            )
            provider_name = generated.provider
            provider_model = generated.model
            request_id = generated.request_id
            usage = generated.usage
            result = await asyncio.to_thread(visuald.normalize_result, generated.data)

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
