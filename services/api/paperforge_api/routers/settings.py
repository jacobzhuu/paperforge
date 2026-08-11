"""设置与版本历史（设计 §4.8 设置页 / M6）。

模型角色映射按 `role → provider+model` 呈现（设计 §4.9）；成本面板读 `llm_call_log`。
密钥永不回传：接口只返回「是否已配置」。
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from db import (
    create_document,
    get_writing_whitelist,
    invalidate_quality_reports_for_project,
    latest_document,
    list_sections,
    project_llm_cost,
    replace_citation_usage,
    upsert_section,
)
from db.models.paper import Outline, PaperDocument, PaperSection
from fastapi import APIRouter, Depends, HTTPException, status
from llm_runtime import DEFAULT_ROLE_MODELS
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from visuals import image_provider_capabilities, image_provider_configured

from paperforge_api.config import get_settings
from paperforge_api.deps import authorize_project_request, get_session
from paperforge_api.deps import get_authorized_project as _require_project
from paperforge_api.schemas import (
    DocumentVersionResponse,
    ImageProviderCapabilitiesResponse,
    OutlineVersionResponse,
    RoleModelResponse,
    SettingsResponse,
    VersionHistoryResponse,
)

router = APIRouter(
    prefix="/api/v1", tags=["settings"], dependencies=[Depends(authorize_project_request)]
)

SessionDep = Annotated[AsyncSession, Depends(get_session)]

ROLES = (
    "planner",
    "extractor",
    "reranker",
    "evidence_classifier",
    "evidence_classifier_fallback",
    "writer",
    "polisher",
    "verifier",
)
ROLE_DESCRIPTION = {
    "planner": "SCOPE / 大纲 / 主题聚类（中档，严格 JSON 校验 + 确定性回退）",
    "extractor": "文献卡片抽取（便宜 + 长上下文，按 work+source_hash 跨项目缓存）",
    "reranker": "检索结果 top-N 重排（便宜，仅输出排序与理由）",
    "evidence_classifier": "问题—证据语义判定（Flash 档，保留推理能力）",
    "evidence_classifier_fallback": "证据分类异常复核（强模型档，仅空结果时调用）",
    "writer": "章节写作与连贯性 pass（最强档，结构化输出）",
    "polisher": "编辑器内润色 / 改写 / 学术语气（强档）",
    "verifier": "引用语义软校验、数字 lint 辅助（便宜，仅出提示）",
}


@router.get("/settings", response_model=SettingsResponse)
async def read_settings() -> SettingsResponse:
    """返回运行时配置概览。**不回传任何密钥**，只报告是否已配置。"""
    settings = get_settings()
    config = settings.llm_config()
    roles = [
        RoleModelResponse(
            role=role,
            model=config.model_for_role(role),
            default_model=DEFAULT_ROLE_MODELS.get(role, ""),
            description=ROLE_DESCRIPTION.get(role, ""),
        )
        for role in ROLES
    ]
    image_config = settings.image_provider_config()
    # 能力声明只描述**提供商能做什么**，不含任何凭据；界面按它渲染表单，
    # 因此不会再出现「选了横向 3:2 却拿到方图」这类不会生效的选项。
    capabilities = image_provider_capabilities(image_config)

    return SettingsResponse(
        llm_provider=settings.llm_default_provider,
        llm_api_key_configured=bool(settings.llm_openai_api_key.strip()),
        llm_enabled=settings.llm_default_provider.strip().lower() not in {"", "noop"},
        roles=roles,
        scholar_contact_email_configured=bool(settings.scholar_contact_email.strip()),
        storage_backend=settings.storage_backend,
        visuals_enabled=settings.visuals_enabled,
        ai_images_enabled=settings.ai_images_enabled,
        image_provider=image_config.provider,
        image_model=image_config.model,
        image_api_key_configured=bool(image_config.api_key.strip()),
        image_provider_configured=image_provider_configured(image_config),
        image_capabilities=(
            ImageProviderCapabilitiesResponse(
                provider=capabilities.provider,
                model=capabilities.model,
                supported_sizes=list(capabilities.supported_sizes),
                supported_aspect_ratios=list(capabilities.supported_aspect_ratios),
                quality_modes=list(capabilities.quality_modes),
                prompt_max_length=capabilities.prompt_max_length,
                supports_negative_prompt=capabilities.supports_negative_prompt,
                supports_seed=capabilities.supports_seed,
                fixed_output_size=capabilities.fixed_output_size,
                cost_estimate_available=capabilities.cost_estimate_available,
                note=capabilities.note,
            )
            if capabilities is not None
            else None
        ),
    )


@router.get("/projects/{project_id}/versions", response_model=VersionHistoryResponse)
async def version_history(project_id: str, session: SessionDep) -> VersionHistoryResponse:
    """大纲与文稿的版本历史（每次生成都建新版本，可回溯）。"""
    project = await _require_project(session, project_id)
    outlines = list(
        (
            await session.scalars(
                select(Outline)
                .where(Outline.project_id == project.id)
                .order_by(Outline.version.desc())
            )
        ).all()
    )
    documents = list(
        (
            await session.scalars(
                select(PaperDocument)
                .where(PaperDocument.project_id == project.id)
                .order_by(PaperDocument.version.desc())
            )
        ).all()
    )
    current = await latest_document(session, project.id)
    section_counts = {
        document_id: int(count)
        for document_id, count in (
            await session.execute(
                select(PaperDocument.id, func.count(PaperSection.id))
                .outerjoin(PaperSection, PaperSection.document_id == PaperDocument.id)
                .where(PaperDocument.project_id == project.id)
                .group_by(PaperDocument.id)
            )
        ).all()
    }

    return VersionHistoryResponse(
        project_id=str(project.id),
        outlines=[
            OutlineVersionResponse(
                id=str(row.id),
                version=row.version,
                status=row.status,
                section_count=len((row.tree_json or {}).get("sections") or []),
                created_at=row.created_at,
            )
            for row in outlines
        ],
        documents=[
            DocumentVersionResponse(
                id=str(row.id),
                version=row.version,
                status=row.status,
                section_count=section_counts.get(row.id),
                is_current=current is not None and row.id == current.id,
                created_at=row.created_at,
            )
            for row in documents
        ],
    )


@router.post(
    "/projects/{project_id}/versions/{document_id}/restore",
    response_model=DocumentVersionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def restore_document_version(
    project_id: str,
    document_id: str,
    session: SessionDep,
) -> DocumentVersionResponse:
    """Clone an old document into a new current version; never overwrite history."""
    project = await _require_project(session, project_id)
    try:
        source_uuid = uuid.UUID(document_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail="document version not found") from error
    source = await session.get(PaperDocument, source_uuid)
    if source is None or source.project_id != project.id:
        raise HTTPException(status_code=404, detail="document version not found")
    current = await latest_document(session, project.id)
    if current is not None and current.id == source.id:
        raise HTTPException(status_code=409, detail="document version is already current")
    source_rows = await list_sections(session, source.id)
    restored = await create_document(
        session,
        project_id=project.id,
        outline_id=source.outline_id,
        status="draft",
    )
    whitelist = await get_writing_whitelist(session, project.id)
    for row in source_rows:
        copied = await upsert_section(
            session,
            document_id=restored.id,
            section_key=row.section_key,
            title=row.title,
            parent_key=row.parent_key,
            order_no=row.order_no,
            body_ir=row.body_ir_json,
            cite_keys=list(row.cite_keys_json or []),
            asset_refs=list(row.asset_refs_json or []),
            status=row.status,
            model=row.model,
        )
        await replace_citation_usage(
            session,
            project_id=project.id,
            section_id=copied.id,
            usages=[
                {"work_id": whitelist[key], "cite_key": key, "context_snippet": None}
                for key in row.cite_keys_json or []
                if key in whitelist
            ],
        )
    await invalidate_quality_reports_for_project(session, project.id)
    return DocumentVersionResponse(
        id=str(restored.id),
        version=restored.version,
        status=restored.status,
        section_count=len(source_rows),
        is_current=True,
        created_at=restored.created_at,
    )


@router.get("/projects/{project_id}/cost/detail")
async def cost_detail(project_id: str, session: SessionDep) -> dict[str, Any]:
    """成本面板：按角色聚合的 LLM 调用记账（设计 §4.9）。"""
    from db.models.paper import LlmCallLog
    from db.repositories.jobs import unpriced_call_count
    from sqlalchemy import func

    project = await _require_project(session, project_id)
    rows = (
        await session.execute(
            select(
                LlmCallLog.role,
                LlmCallLog.model,
                LlmCallLog.provider,
                func.count(LlmCallLog.id),
                func.coalesce(func.sum(LlmCallLog.input_tokens), 0),
                func.coalesce(func.sum(LlmCallLog.output_tokens), 0),
                func.coalesce(func.sum(LlmCallLog.cost_estimate), 0.0),
                func.coalesce(func.avg(LlmCallLog.latency_ms), 0),
                func.count(LlmCallLog.error_code),
                unpriced_call_count(),
            )
            .where(LlmCallLog.project_id == project.id)
            .group_by(LlmCallLog.role, LlmCallLog.model, LlmCallLog.provider)
            .order_by(func.count(LlmCallLog.id).desc())
        )
    ).all()
    totals = await project_llm_cost(session, project.id)
    from db.models.paper import VisualAsset, VisualGenerationAttempt

    image_row = (
        await session.execute(
            select(
                func.count(VisualGenerationAttempt.id),
                func.count(VisualGenerationAttempt.error_code),
                func.coalesce(func.sum(VisualGenerationAttempt.cost_estimate), 0.0),
                func.count(1).filter(
                    VisualGenerationAttempt.error_code.is_(None),
                    VisualGenerationAttempt.cost_estimate.is_(None),
                ),
            )
            .join(VisualAsset, VisualAsset.id == VisualGenerationAttempt.visual_id)
            .where(VisualAsset.project_id == project.id)
        )
    ).one()
    image_sizes = (
        await session.execute(
            select(
                VisualGenerationAttempt.provider,
                VisualGenerationAttempt.model,
                VisualGenerationAttempt.output_width,
                VisualGenerationAttempt.output_height,
                func.count(VisualGenerationAttempt.id),
                func.count(VisualGenerationAttempt.error_code),
                func.coalesce(func.sum(VisualGenerationAttempt.cost_estimate), 0.0),
            )
            .join(VisualAsset, VisualAsset.id == VisualGenerationAttempt.visual_id)
            .where(VisualAsset.project_id == project.id)
            .group_by(
                VisualGenerationAttempt.provider,
                VisualGenerationAttempt.model,
                VisualGenerationAttempt.output_width,
                VisualGenerationAttempt.output_height,
            )
            .order_by(func.count(VisualGenerationAttempt.id).desc())
        )
    ).all()
    return {
        "project_id": str(project.id),
        "totals": totals,
        "by_role": [
            {
                "role": row[0],
                "model": row[1],
                "provider": row[2],
                "call_count": int(row[3] or 0),
                "input_tokens": int(row[4] or 0),
                "output_tokens": int(row[5] or 0),
                "cost_estimate": float(row[6] or 0.0),
                "avg_latency_ms": int(row[7] or 0),
                "failed_call_count": int(row[8] or 0),
                "unpriced_call_count": int(row[9] or 0),
            }
            for row in rows
        ],
        "images": {
            "call_count": int(image_row[0] or 0),
            "failed_call_count": int(image_row[1] or 0),
            "cost_estimate": float(image_row[2] or 0.0),
            # 三个图像 provider 都声明 cost_estimate_available=False，所以这里
            # 目前恒等于成功次数。照实报出来，好过让面板显示生图免费。
            "unpriced_call_count": int(image_row[3] or 0),
            "by_size": [
                {
                    "provider": row[0],
                    "model": row[1],
                    "width": row[2],
                    "height": row[3],
                    "call_count": int(row[4] or 0),
                    "failed_call_count": int(row[5] or 0),
                    "cost_estimate": float(row[6] or 0.0),
                }
                for row in image_sizes
            ],
        },
    }
