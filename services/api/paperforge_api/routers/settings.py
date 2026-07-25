"""设置与版本历史（设计 §4.8 设置页 / M6）。

模型角色映射按 `role → provider+model` 呈现（设计 §4.9）；成本面板读 `llm_call_log`。
密钥永不回传：接口只返回「是否已配置」。
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from db import (
    get_project,
    latest_document,
    list_sections,
    project_llm_cost,
)
from db.models.paper import Outline, PaperDocument
from fastapi import APIRouter, Depends, HTTPException
from llm_runtime import DEFAULT_ROLE_MODELS
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from paperforge_api.config import get_settings
from paperforge_api.deps import get_session
from paperforge_api.schemas import (
    DocumentVersionResponse,
    OutlineVersionResponse,
    RoleModelResponse,
    SettingsResponse,
    VersionHistoryResponse,
)

router = APIRouter(prefix="/api/v1", tags=["settings"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]

ROLES = ("planner", "extractor", "reranker", "writer", "polisher", "verifier")
ROLE_DESCRIPTION = {
    "planner": "SCOPE / 大纲 / 主题聚类（中档，严格 JSON 校验 + 确定性回退）",
    "extractor": "文献卡片抽取（便宜 + 长上下文，按 work+source_hash 跨项目缓存）",
    "reranker": "检索结果 top-N 重排（便宜，仅输出排序与理由）",
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
    return SettingsResponse(
        llm_provider=settings.llm_default_provider,
        llm_base_url=settings.llm_openai_base_url,
        llm_api_key_configured=bool(settings.llm_openai_api_key.strip()),
        llm_enabled=settings.llm_default_provider.strip().lower() not in {"", "noop"},
        roles=roles,
        scholar_contact_email=settings.scholar_contact_email or None,
        semantic_scholar_key_configured=bool(settings.semantic_scholar_api_key.strip()),
        storage_backend=settings.storage_backend,
        texd_url=settings.texd_url,
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
    section_counts: dict[uuid.UUID, int] = {}
    if current is not None:
        section_counts[current.id] = len(await list_sections(session, current.id))

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


@router.get("/projects/{project_id}/cost/detail")
async def cost_detail(project_id: str, session: SessionDep) -> dict[str, Any]:
    """成本面板：按角色聚合的 LLM 调用记账（设计 §4.9）。"""
    from db.models.paper import LlmCallLog
    from sqlalchemy import func

    project = await _require_project(session, project_id)
    rows = (
        await session.execute(
            select(
                LlmCallLog.role,
                LlmCallLog.model,
                func.count(LlmCallLog.id),
                func.coalesce(func.sum(LlmCallLog.input_tokens), 0),
                func.coalesce(func.sum(LlmCallLog.output_tokens), 0),
                func.coalesce(func.sum(LlmCallLog.cost_estimate), 0.0),
                func.coalesce(func.avg(LlmCallLog.latency_ms), 0),
                func.count(LlmCallLog.error_code),
            )
            .where(LlmCallLog.project_id == project.id)
            .group_by(LlmCallLog.role, LlmCallLog.model)
            .order_by(func.count(LlmCallLog.id).desc())
        )
    ).all()
    totals = await project_llm_cost(session, project.id)
    return {
        "project_id": str(project.id),
        "totals": totals,
        "by_role": [
            {
                "role": row[0],
                "model": row[1],
                "call_count": int(row[2] or 0),
                "input_tokens": int(row[3] or 0),
                "output_tokens": int(row[4] or 0),
                "cost_estimate": float(row[5] or 0.0),
                "avg_latency_ms": int(row[6] or 0),
                "failed_call_count": int(row[7] or 0),
            }
            for row in rows
        ],
    }


async def _require_project(session: AsyncSession, project_id: str):
    try:
        project_uuid = uuid.UUID(project_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail="project not found") from error
    project = await get_project(session, project_uuid)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return project
