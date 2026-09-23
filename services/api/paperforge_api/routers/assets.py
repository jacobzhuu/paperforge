"""素材上传与解析路由（设计 §4.7 `POST /projects/{id}/assets`）。

上传即**确定性解析**入 `user_asset.parsed_json`：这是研究型论文管线里
「正文数字只能来自素材」的事实来源。LLM 不参与解析。
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Annotated, Any

from db import (
    asset_document_dependency_count,
    create_asset,
    delete_asset,
    get_asset,
    list_assets,
    list_jobs,
    parsed_asset_payloads,
    visual_source_dependency_count,
)
from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile, status
from ingest import parse_asset
from ingest.numlint import lint_sections
from sqlalchemy.ext.asyncio import AsyncSession
from storage import make_object_store

from paperforge_api.config import get_settings
from paperforge_api.deps import authorize_project_request, get_session
from paperforge_api.deps import get_authorized_project as _require_project
from paperforge_api.schemas import (
    AssetCapabilitiesResponse,
    AssetResponse,
    MaterialPreflightResponse,
    NumLintResponse,
)

router = APIRouter(
    prefix="/api/v1", tags=["assets"], dependencies=[Depends(authorize_project_request)]
)

SessionDep = Annotated[AsyncSession, Depends(get_session, scope="function")]

MAX_ASSET_BYTES = 32 * 1024 * 1024
PREFERRED_ASSET_EXTENSIONS = [
    ".csv",
    ".tsv",
    ".xlsx",
    ".pdf",
    ".docx",
    ".txt",
    ".md",
    ".py",
    ".r",
    ".ipynb",
    ".bib",
    ".png",
    ".jpg",
    ".svg",
]


@router.get("/assets/capabilities", response_model=AssetCapabilitiesResponse)
async def asset_capabilities() -> AssetCapabilitiesResponse:
    """素材限制的单一事实源，供上传前校验与帮助文案使用。"""
    return AssetCapabilitiesResponse(
        max_bytes=MAX_ASSET_BYTES,
        max_mib=MAX_ASSET_BYTES // (1024 * 1024),
        preferred_extensions=PREFERRED_ASSET_EXTENSIONS,
        accepts_unrecognized_as_method_note=True,
    )


@router.get(
    "/projects/{project_id}/assets/preflight",
    response_model=MaterialPreflightResponse,
)
async def material_preflight(project_id: str, session: SessionDep) -> MaterialPreflightResponse:
    project = await _require_project(session, project_id)
    if project.paper_type != "original":
        return MaterialPreflightResponse(ready=True)
    issues = _original_material_issues(await list_assets(session, project.id))
    return MaterialPreflightResponse(ready=not issues, issues=issues)


def _original_material_issues(assets: list[Any]) -> list[dict[str, str]]:
    has_results = False
    has_method = False
    for asset in assets:
        parsed = asset.parsed_json if isinstance(asset.parsed_json, dict) else {}
        if asset.kind in {"dataset", "result_table"}:
            has_results = (
                bool(parsed.get("rows") and (parsed.get("numeric_cells") or parsed.get("numbers")))
                or has_results
            )
        if asset.kind in {"method_note", "code"}:
            has_method = (
                bool(str(parsed.get("text") or asset.description or "").strip()) or has_method
            )
    issues: list[dict[str, str]] = []
    if not has_results:
        issues.append(
            {
                "code": "result_material_missing",
                "message": "请上传包含数据行和可解析数值的结果表或数据集",
            }
        )
    if not has_method:
        issues.append(
            {"code": "method_material_missing", "message": "请上传可解析的方法笔记或代码"}
        )
    return issues


@router.post(
    "/projects/{project_id}/assets",
    response_model=AssetResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_asset(
    project_id: str,
    session: SessionDep,
    file: Annotated[UploadFile, File()],
    kind: Annotated[str | None, Form()] = None,
    description: Annotated[str | None, Form()] = None,
) -> AssetResponse:
    project = await _require_project(session, project_id)
    content = await file.read(MAX_ASSET_BYTES + 1)
    if not content:
        raise HTTPException(status_code=422, detail="uploaded file is empty")
    if len(content) > MAX_ASSET_BYTES:
        raise HTTPException(status_code=413, detail="asset exceeds 32 MiB limit")

    filename = file.filename or "asset"
    parsed = await asyncio.to_thread(
        parse_asset,
        content=content,
        filename=filename,
        mime_type=file.content_type or "application/octet-stream",
        kind=kind,
    )

    store = await asyncio.to_thread(make_object_store, get_settings())
    object_key = (
        f"users/{project.owner_id}/projects/{project.id}/assets/"
        f"{uuid.uuid4()}-{_safe_name(filename)}"
    )
    await asyncio.to_thread(store.put, object_key, content)

    payload = dict(parsed.parsed)
    if parsed.kind == "figure":
        # 渲染期 \includegraphics 用的相对路径（导出时随工程一起投递）。
        payload["figure_path"] = f"figures/{_safe_name(filename)}"
    if parsed.warnings:
        payload["warnings"] = parsed.warnings

    try:
        asset = await create_asset(
            session,
            project_id=project.id,
            kind=parsed.kind,
            title=filename,
            description=description,
            object_key=object_key,
            parsed=payload or None,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return _asset_response(asset)


@router.get("/projects/{project_id}/assets", response_model=list[AssetResponse])
async def get_assets(project_id: str, session: SessionDep) -> list[AssetResponse]:
    project = await _require_project(session, project_id)
    return [_asset_response(asset) for asset in await list_assets(session, project.id)]


@router.delete(
    "/projects/{project_id}/assets/{asset_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_asset(project_id: str, asset_id: str, session: SessionDep) -> None:
    project = await _require_project(session, project_id)
    try:
        asset_uuid = uuid.UUID(asset_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail="asset not found") from error
    asset = await get_asset(session, asset_uuid)
    if asset is None or asset.project_id != project.id:
        raise HTTPException(status_code=404, detail="asset not found")
    if any(job.status in {"queued", "running"} for job in await list_jobs(session, project.id)):
        raise HTTPException(
            status_code=409,
            detail="asset cannot be deleted while a generation job is active",
        )
    if await asset_document_dependency_count(session, asset):
        raise HTTPException(
            status_code=409,
            detail="asset is used by the manuscript provenance and cannot be deleted",
        )
    if await visual_source_dependency_count(session, asset.id):
        raise HTTPException(
            status_code=409,
            detail="asset is used by a versioned visual and cannot be deleted",
        )
    await delete_asset(session, asset)


@router.get("/projects/{project_id}/assets/{asset_id}/download")
async def download_asset(project_id: str, asset_id: str, session: SessionDep) -> Response:
    project = await _require_project(session, project_id)
    try:
        asset_uuid = uuid.UUID(asset_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail="asset not found") from error
    asset = await get_asset(session, asset_uuid)
    if asset is None or asset.project_id != project.id or not asset.object_key:
        raise HTTPException(status_code=404, detail="asset not found")
    store = await asyncio.to_thread(make_object_store, get_settings())
    try:
        data = store.get(asset.object_key)
    except (FileNotFoundError, ValueError) as error:
        raise HTTPException(status_code=410, detail="asset payload is gone") from error
    return Response(
        content=data,
        media_type=_asset_media_type(data),
        headers={"Content-Disposition": f'attachment; filename="{asset.title or "asset"}"'},
    )


@router.get("/projects/{project_id}/numlint", response_model=NumLintResponse)
async def numlint_report(project_id: str, session: SessionDep) -> NumLintResponse:
    """数字一致性 lint（设计 §4.4.2 NUMLINT）。

    ``consistent=false`` 表示正文里存在无法在素材中找到出处的数值——
    这类数值必须被人工核对或改为 `\\todo{待补充实验数据}` 占位。
    """
    from db import latest_document, list_sections

    project = await _require_project(session, project_id)
    document = await latest_document(session, project.id)
    rows = await list_sections(session, document.id) if document else []
    sections = [
        {
            "section_key": row.section_key,
            "text": " ".join(
                run.get("v", "")
                for block in (row.body_ir_json or {}).get("blocks", [])
                for run in block.get("runs", [])
                if run.get("t") == "text"
            ),
        }
        for row in rows
    ]
    assets = await parsed_asset_payloads(session, project.id)
    report = lint_sections(sections, parsed_assets=assets)
    return NumLintResponse(project_id=str(project.id), **report.to_payload())


def _asset_response(asset: Any) -> AssetResponse:
    parsed = asset.parsed_json or {}
    return AssetResponse(
        id=str(asset.id),
        kind=asset.kind,
        title=asset.title,
        description=asset.description,
        created_at=asset.created_at,
        parsed_type=parsed.get("type"),
        row_count=parsed.get("row_count"),
        column_count=parsed.get("column_count"),
        number_count=len(parsed.get("numbers") or []),
        headers=parsed.get("headers") or [],
        preview_rows=(parsed.get("rows") or [])[:5],
        warnings=parsed.get("warnings") or [],
        asset_ref=f"ua_{str(asset.id)[:8]}",
    )


def _safe_name(name: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in name)
    return cleaned.strip("-") or "asset"


def _asset_media_type(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    return "application/octet-stream"
