"""M8 视觉建议与生成管线。

建议阶段只创建结构化 proposal，不调用图片 provider、不改 PaperIR；生成阶段才调用
visuald 或经用户确认后的 ImageProvider。
"""

from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from dataclasses import dataclass
from typing import Any

from db import (
    add_visual_source,
    create_visual,
    get_asset,
    get_project,
    get_visual,
    latest_document,
    list_assets,
    list_sections,
    list_visuals,
    record_visual_attempt,
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
    create_image_provider,
    parse_visual_spec,
)

from paperforge_worker.context import JobContext


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

    def to_payload(self) -> dict[str, int]:
        return {
            "proposed": self.proposed,
            "chart_count": self.chart_count,
            "diagram_count": self.diagram_count,
            "ai_image_count": self.ai_image_count,
            "preview_ready_count": self.preview_ready_count,
            "preview_failed_count": self.preview_failed_count,
        }


async def suggest_visuals(context: JobContext) -> VisualPlanOutcome:
    """确定性提出最多 6 条建议；绝不调用 ImageProvider 或修改章节 IR。"""
    if not context.settings.visuals_enabled:
        return VisualPlanOutcome(0, 0, 0, 0)
    async with context.session() as session:
        assets = await list_assets(session, context.project_id)
        project = await get_project(session, context.project_id)
        document = await latest_document(session, context.project_id)
        sections = await list_sections(session, document.id) if document else []
        existing = await list_visuals(session, context.project_id)
        existing_hashes = {item.input_hash for item in existing}

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
                    },
                )
            )
            if len(proposals) >= 4:
                break

        body_sections = [row for row in sections if row.section_key != "abstract"][:6]
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
            proposals.append(
                (
                    diagram,
                    {
                        "title": "论文结构概览",
                        "caption": "论文主要章节与论述流程",
                        "alt_text": "从左到右连接主要章节的论文结构示意图",
                        "target_section_key": body_sections[0].section_key,
                    },
                )
            )

        if len(proposals) < 6:
            project_title = project.title if project is not None else "the research topic"
            # AI 只创建概念性 proposal；此处没有 provider 调用。
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
            proposals.append(
                (
                    ai_spec,
                    {
                        "title": "概念性研究插图",
                        "caption": "研究主题的概念性视觉概览",
                        "alt_text": "以抽象科学元素表达研究主题的概念插图",
                        "target_section_key": body_sections[0].section_key
                        if body_sections
                        else None,
                    },
                )
            )

        created = []
        for spec, metadata in proposals[:6]:
            from db import visual_input_hash

            if visual_input_hash(spec) in existing_hashes:
                continue
            visual = await create_visual(
                session,
                project_id=context.project_id,
                kind=spec["kind"],
                spec=spec,
                document_version=document.version if document else None,
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
        proposed=len(created),
        chart_count=sum(item.kind == "chart" for item in created),
        diagram_count=sum(item.kind == "diagram" for item in created),
        ai_image_count=sum(item.kind == "ai_image" for item in created),
        preview_ready_count=sum(item.status == "ready" for item in previews),
        preview_failed_count=sum(item.status == "failed" for item in previews),
    )


async def generate_visual(
    context: JobContext,
    visual_id: uuid.UUID,
    *,
    timeout_seconds: float | None = None,
) -> VisualOutcome:
    if not context.settings.visuals_enabled:
        return VisualOutcome(str(visual_id), "failed", [], "visuals_disabled")
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
                    "AI images are disabled", code="ai_images_disabled", retryable=False
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
                    prompt=f"{spec.prompt}. Style: {spec.style}",
                    size=spec.size,
                    quality=spec.quality,
                ),
            )
            provider_name = generated.provider
            provider_model = generated.model
            request_id = generated.request_id
            usage = generated.usage
            result = await asyncio.to_thread(visuald.normalize_result, generated.data)

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
        code = error.code if isinstance(error, ImageProviderError) else type(error).__name__
        latency_ms = int((time.monotonic() - started) * 1000)
        async with context.session() as session:
            visual = await get_visual(session, visual_id)
            if visual is not None:
                visual.generation_status = "failed"
                visual.error_code = code
                visual.error_message = str(error)[:500]
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
