"""大纲 / 章节 / 引用审计 / Markdown 预览 路由（设计 §4.7）。"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Annotated, Any
from urllib.parse import quote

from arq.connections import ArqRedis
from db import (
    create_job,
    get_cards,
    get_section,
    get_writing_whitelist,
    latest_document,
    latest_outline,
    list_assets,
    list_citation_usage,
    list_entries,
    list_sections,
    list_visuals,
    reference_metadata_payload,
    replace_citation_usage,
    update_outline_tree,
    upsert_section,
)
from db.models.paper import ExportArtifact
from db.repositories.exports import list_export_artifacts
from fastapi import APIRouter, Depends, HTTPException, Response, status
from paper_ir import Bibliography, PaperIR, PaperMeta, ReferenceMetadata, render_markdown
from paper_ir.schema import Section as IRSection
from sqlalchemy.ext.asyncio import AsyncSession
from storage import make_object_store

from paperforge_api.config import get_settings
from paperforge_api.deps import (
    authorize_project_request,
    get_queue,
    get_session,
)
from paperforge_api.deps import get_authorized_project as _require_project
from paperforge_api.routers.projects import _job_response
from paperforge_api.schemas import (
    CitationAuditResponse,
    CitationAuditRow,
    ExportArtifactResponse,
    ExportRequest,
    IngestRequest,
    JobResponse,
    MarkdownResponse,
    OutlineResponse,
    QualityResponse,
    RefineRequest,
    RefineResponse,
    SectionResponse,
    SnowballRequest,
    UpdateOutlineRequest,
    UpdateSectionRequest,
    WriteRequest,
)

router = APIRouter(
    prefix="/api/v1", tags=["writing"], dependencies=[Depends(authorize_project_request)]
)

SessionDep = Annotated[AsyncSession, Depends(get_session)]
QueueDep = Annotated[ArqRedis | None, Depends(get_queue)]


# ---- 大纲 ----


@router.post(
    "/projects/{project_id}/outline/generate",
    response_model=JobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def generate_outline_endpoint(
    project_id: str,
    session: SessionDep,
    queue: QueueDep,
) -> JobResponse:
    project = await _require_project(session, project_id)
    job = await create_job(session, project_id=project.id, kind="outline")
    await _enqueue(queue, "run_outline_pipeline", str(project.id), str(job.id))
    return _job_response(job)


@router.get("/projects/{project_id}/outline", response_model=OutlineResponse)
async def get_outline_endpoint(project_id: str, session: SessionDep) -> OutlineResponse:
    project = await _require_project(session, project_id)
    outline = await latest_outline(session, project.id)
    if outline is None:
        return OutlineResponse(project_id=str(project.id), version=0, status="draft", tree={})
    return OutlineResponse(
        project_id=str(project.id),
        outline_id=str(outline.id),
        version=outline.version,
        status=outline.status,
        tree=outline.tree_json or {},
    )


@router.put("/projects/{project_id}/outline", response_model=OutlineResponse)
async def put_outline(
    project_id: str,
    request: UpdateOutlineRequest,
    session: SessionDep,
) -> OutlineResponse:
    """人工编辑大纲。

    R2 前置：这里会把章节 cite_keys 收敛到写作白名单内——手工编辑同样
    无法把白名单外的引用带进写作上下文。
    """
    project = await _require_project(session, project_id)
    outline = await latest_outline(session, project.id)
    if outline is None:
        raise HTTPException(status_code=404, detail="outline not generated yet")
    whitelist = set(await get_writing_whitelist(session, project.id))
    tree = _sanitize_outline_tree(request.tree, whitelist)
    await update_outline_tree(session, outline, tree, status=request.status)
    return OutlineResponse(
        project_id=str(project.id),
        outline_id=str(outline.id),
        version=outline.version,
        status=outline.status,
        tree=outline.tree_json or {},
    )


# ---- 写作 ----


@router.post(
    "/projects/{project_id}/generate",
    response_model=JobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def generate_full(
    project_id: str,
    session: SessionDep,
    queue: QueueDep,
) -> JobResponse:
    """一键全管线（kind=full）。"""
    project = await _require_project(session, project_id)
    job = await create_job(session, project_id=project.id, kind="full")
    await _enqueue(queue, "run_full_pipeline", str(project.id), str(job.id))
    return _job_response(job)


@router.post(
    "/projects/{project_id}/sections/generate",
    response_model=JobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def generate_sections(
    project_id: str,
    request: WriteRequest,
    session: SessionDep,
    queue: QueueDep,
) -> JobResponse:
    project = await _require_project(session, project_id)
    outline = await latest_outline(session, project.id)
    if outline is None:
        raise HTTPException(status_code=409, detail="generate an outline first")
    job = await create_job(session, project_id=project.id, kind="write")
    await _enqueue(
        queue,
        "run_write_pipeline",
        str(project.id),
        str(job.id),
        coherence=request.coherence,
    )
    return _job_response(job)


@router.get("/projects/{project_id}/sections", response_model=list[SectionResponse])
async def get_sections(project_id: str, session: SessionDep) -> list[SectionResponse]:
    project = await _require_project(session, project_id)
    document = await latest_document(session, project.id)
    if document is None:
        return []
    rows = await list_sections(session, document.id)
    return [_section_response(row) for row in rows]


@router.put("/projects/{project_id}/sections/{section_key}", response_model=SectionResponse)
async def put_section(
    project_id: str,
    section_key: str,
    request: UpdateSectionRequest,
    session: SessionDep,
) -> SectionResponse:
    """人工编辑章节。

    R2：编辑器提交的 body_ir 会再过一次白名单——前端 chip 已限制取值，
    这里是服务端的兜底，手工构造请求也无法引入幻觉引用。
    """
    project = await _require_project(session, project_id)
    document = await latest_document(session, project.id)
    if document is None:
        raise HTTPException(status_code=404, detail="document not generated yet")
    row = await get_section(session, document_id=document.id, section_key=section_key)
    if row is None:
        raise HTTPException(status_code=404, detail="section not found")

    whitelist = await get_writing_whitelist(session, project.id)
    try:
        ir_section = IRSection(**request.body_ir)
    except Exception as error:  # noqa: BLE001 - 结构非法直接 422
        raise HTTPException(status_code=422, detail=f"invalid section IR: {error}") from error

    ir = PaperIR(meta=PaperMeta(title=project.title), sections=[ir_section])
    ir.enforce_cite_key_whitelist(set(whitelist), strip=True)
    cleaned = ir.sections[0]
    cite_keys = sorted(ir.collect_cite_keys())
    asset_refs = sorted(ir.collect_asset_refs())

    updated = await upsert_section(
        session,
        document_id=document.id,
        section_key=section_key,
        title=request.title or cleaned.title,
        order_no=row.order_no,
        body_ir=cleaned.model_dump(mode="json"),
        cite_keys=cite_keys,
        asset_refs=asset_refs,
        status="edited",
        model=row.model,
    )
    await replace_citation_usage(
        session,
        project_id=project.id,
        section_id=updated.id,
        usages=[
            {"work_id": whitelist[key], "cite_key": key, "context_snippet": None}
            for key in cite_keys
            if key in whitelist
        ],
    )
    return _section_response(updated)


# ---- 引用审计与预览 ----


@router.get("/projects/{project_id}/citations/audit", response_model=CitationAuditResponse)
async def citations_audit(project_id: str, session: SessionDep) -> CitationAuditResponse:
    """引用审计报告（R2 结果）。

    ``hallucinated_cite_keys`` 恒为空即为 0 幻觉引用；非空表示某处绕过了
    白名单，属于必须修复的缺陷而非提示。
    """
    project = await _require_project(session, project_id)
    whitelist = await get_writing_whitelist(session, project.id)
    document = await latest_document(session, project.id)
    rows = await list_sections(session, document.id) if document else []
    usage = await list_citation_usage(session, project.id)

    used_keys: set[str] = set()
    warnings: list[dict[str, Any]] = []
    for row in rows:
        used_keys.update(row.cite_keys_json or [])
        body = row.body_ir_json or {}
        for warning in body.get("citation_warnings") or []:
            warnings.append({"section_key": row.section_key, **warning})

    return CitationAuditResponse(
        project_id=str(project.id),
        whitelist_size=len(whitelist),
        used_cite_keys=sorted(used_keys),
        hallucinated_cite_keys=sorted(used_keys - set(whitelist)),
        removed_citation_warnings=warnings,
        unused_cite_keys=sorted(set(whitelist) - used_keys),
        rows=[
            CitationAuditRow(
                cite_key=item.cite_key,
                section_id=str(item.section_id),
                work_id=str(item.work_id),
                context_snippet=item.context_snippet,
            )
            for item in usage
        ],
    )


@router.get("/projects/{project_id}/preview/markdown", response_model=MarkdownResponse)
async def markdown_preview(project_id: str, session: SessionDep) -> MarkdownResponse:
    """Markdown 预览：渲染器只消费持久化的 Section IR。"""
    project = await _require_project(session, project_id)
    document = await latest_document(session, project.id)
    if document is None:
        return MarkdownResponse(project_id=str(project.id), markdown="", word_count=0)

    rows = await list_sections(session, document.id)
    whitelist = await get_writing_whitelist(session, project.id)
    used_keys = {key for row in rows for key in (row.cite_keys_json or [])}
    references: list[ReferenceMetadata] = []
    for entry, work in await list_entries(session, project.id, status="selected"):
        if entry.bibtex_key and entry.bibtex_key in used_keys:
            payload = await reference_metadata_payload(session, work, bibtex_key=entry.bibtex_key)
            references.append(ReferenceMetadata(**payload))

    sections = [IRSection(**row.body_ir_json) for row in rows if row.body_ir_json]
    abstract_section = next((s for s in sections if s.key == "abstract"), None)
    body_sections = [s for s in sections if s.key != "abstract"]
    abstract_text = ""
    if abstract_section is not None:
        abstract_text = " ".join(
            run.get("v", "")
            for block in abstract_section.model_dump(mode="json").get("blocks", [])
            for run in block.get("runs", [])
            if run.get("t") == "text"
        ).strip()

    ir = PaperIR(
        meta=PaperMeta(
            title=project.title,
            abstract=abstract_text,
            language=project.language,  # type: ignore[arg-type]
        ),
        sections=body_sections,
        bibliography=Bibliography(style=project.citation_style),  # type: ignore[arg-type]
    )
    ir.enforce_cite_key_whitelist(set(whitelist), strip=True)
    asset_urls: dict[str, str] = {}
    for asset in await list_assets(session, project.id):
        url = f"/api/v1/projects/{project.id}/assets/{asset.id}/download"
        asset_urls[str(asset.id)] = url
        asset_urls[f"ua_{str(asset.id)[:8]}"] = url
    for visual in await list_visuals(session, project.id):
        renditions = visual.renditions_json or {}
        fmt = "png" if "png" in renditions else ("svg" if "svg" in renditions else None)
        if fmt:
            url = f"/api/v1/projects/{project.id}/visuals/{visual.id}/renditions/{fmt}"
            asset_urls[str(visual.id)] = url
            asset_urls[f"va_{str(visual.id)[:8]}"] = url
    markdown = render_markdown(
        ir,
        references=references,
        style=project.citation_style,
        asset_urls=asset_urls,
    )
    return MarkdownResponse(
        project_id=str(project.id),
        markdown=markdown,
        word_count=_count_words(markdown),
        document_version=document.version,
    )


# ---- 内部工具 ----


def _sanitize_outline_tree(tree: dict[str, Any], whitelist: set[str]) -> dict[str, Any]:
    sections = []
    for section in tree.get("sections") or []:
        if not isinstance(section, dict):
            continue
        sections.append(
            {
                **section,
                "cite_keys": [key for key in (section.get("cite_keys") or []) if key in whitelist],
            }
        )
    return {**tree, "sections": sections}


def _section_response(row) -> SectionResponse:
    body = row.body_ir_json or {}
    return SectionResponse(
        section_key=row.section_key,
        title=row.title,
        order_no=row.order_no,
        status=row.status,
        model=row.model,
        cite_keys=row.cite_keys_json or [],
        body_ir=body,
        citation_warnings=body.get("citation_warnings") or [],
        word_count=_count_section_words(body),
        updated_at=row.updated_at,
    )


def _count_section_words(body: dict[str, Any]) -> int:
    text = " ".join(
        run.get("v", "")
        for block in body.get("blocks", [])
        for run in block.get("runs", [])
        if run.get("t") == "text"
    )
    return _count_words(text)


def _count_words(text: str) -> int:
    import re

    cjk = len(re.findall(r"[一-鿿]", text))
    latin = len(re.findall(r"[A-Za-z][A-Za-z'-]*", text))
    return cjk + latin


async def _enqueue(queue: ArqRedis | None, function: str, *args: Any, **kwargs: Any) -> None:
    if queue is None:
        raise HTTPException(
            status_code=503,
            detail="task queue unavailable: worker Redis is not reachable",
        )
    await queue.enqueue_job(function, *args, **kwargs)


# ---- 导出（M3） ----


@router.post(
    "/projects/{project_id}/exports",
    response_model=JobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_export(
    project_id: str,
    request: ExportRequest,
    session: SessionDep,
    queue: QueueDep,
) -> JobResponse:
    project = await _require_project(session, project_id)
    document = await latest_document(session, project.id)
    if document is None:
        raise HTTPException(status_code=409, detail="generate the paper before exporting")
    job = await create_job(session, project_id=project.id, kind="compile")
    await _enqueue(
        queue,
        "run_export_pipeline",
        str(project.id),
        str(job.id),
        formats=request.formats,
    )
    return _job_response(job)


@router.get("/projects/{project_id}/exports", response_model=list[ExportArtifactResponse])
async def list_exports(project_id: str, session: SessionDep) -> list[ExportArtifactResponse]:
    project = await _require_project(session, project_id)
    rows = await list_export_artifacts(session, project.id)
    return [
        ExportArtifactResponse(
            id=str(row.id),
            format=row.format,
            document_version=row.document_version,
            object_key=row.object_key,
            content_hash=row.content_hash,
            created_at=row.created_at,
            download_url=f"/api/v1/projects/{project.id}/exports/{row.id}/download",
        )
        for row in rows
    ]


@router.get("/projects/{project_id}/exports/{artifact_id}/download")
async def download_export(
    project_id: str,
    artifact_id: str,
    session: SessionDep,
    disposition: str = "attachment",
) -> Response:
    """取产物。``disposition=inline`` 供页内预览用。

    预览与下载必须是两条不同的响应：`attachment` 会让 `<iframe src>` 变成一次
    下载，于是「打开导出中心」等于「凭空下载一个文件」，而预览框永远是空的。
    """
    project = await _require_project(session, project_id)
    try:
        artifact_uuid = uuid.UUID(artifact_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail="artifact not found") from error
    artifact = await session.get(ExportArtifact, artifact_uuid)
    if artifact is None or artifact.project_id != project.id or not artifact.object_key:
        raise HTTPException(status_code=404, detail="artifact not found")

    store = make_object_store(get_settings())
    try:
        data = store.get(artifact.object_key)
    except (FileNotFoundError, ValueError) as error:
        raise HTTPException(status_code=410, detail="artifact payload is gone") from error

    media_type, suffix = _MEDIA_TYPES.get(artifact.format, ("application/octet-stream", "bin"))
    mode = "inline" if disposition.lower() == "inline" else "attachment"
    return Response(
        content=data,
        media_type=media_type,
        headers={
            "Content-Disposition": _content_disposition(
                mode,
                export_filename(
                    project.title,
                    fmt=artifact.format,
                    suffix=suffix,
                    document_version=artifact.document_version,
                    created_at=artifact.created_at,
                ),
            ),
            # 产物按内容寻址（object_key 含 sha256 前缀），内容永不原地变化。
            "Cache-Control": "private, max-age=3600",
        },
    )


_FORMAT_FILENAME_TAG = {
    "compile_log": "编译日志",
    "latex_zip": "latex",
    "bibtex": "refs",
}

# 文件名里不许出现的字符：路径分隔符与 Windows 保留字符、控制字符、各类空白，
# 以及中英文标点。中日韩文字与字母数字保留——中文标题正是要读得懂的那部分。
_UNSAFE_FILENAME_CHARS = re.compile(
    r'[\x00-\x1f\x7f<>:"/\\|?*\s.,;:!\'’“”，。；：！？、·（）()\[\]{}【】《》〈〉…]+'
)
MAX_FILENAME_STEM = 60


def _slugify_title(title: str) -> str:
    """标题 → 文件名主干。保留中日韩文字与字母数字，其余压成连字符。"""
    cleaned = _UNSAFE_FILENAME_CHARS.sub("-", (title or "").strip())
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-.")
    if len(cleaned) > MAX_FILENAME_STEM:
        # 从右侧的连字符处截断，避免把词/短语切成半截。
        head = cleaned[:MAX_FILENAME_STEM]
        cut = head.rfind("-")
        cleaned = (head[:cut] if cut >= MAX_FILENAME_STEM // 2 else head).strip("-")
    return cleaned


def export_filename(
    title: str,
    *,
    fmt: str,
    suffix: str,
    document_version: int | None,
    created_at: datetime | None = None,
) -> str:
    """可读文件名：`标题-v2-20260726.pdf`。

    `paperforge-v1.pdf` 在下载目录里既认不出是哪篇论文，也认不出是哪一次导出——
    同一天导出三次就是三个 `paperforge-v1(1).pdf`。
    """
    parts = [_slugify_title(title) or "paperforge"]
    tag = _FORMAT_FILENAME_TAG.get(fmt)
    if tag:
        parts.append(tag)
    parts.append(f"v{document_version or 1}")
    if created_at is not None:
        parts.append(created_at.strftime("%Y%m%d"))
    return f"{'-'.join(parts)}.{suffix}"


def _content_disposition(mode: str, filename: str) -> str:
    """RFC 6266 / 5987 头部。

    HTTP 头只能承载 latin-1，中文标题因此必须走 ``filename*``；``filename``
    保留纯 ASCII 兜底（老浏览器与 curl -OJ）。把中文塞进 ``filename``
    会让 ASGI 编码头部时直接抛 UnicodeEncodeError——下载整个 500。
    """
    ascii_fallback = re.sub(r"[^A-Za-z0-9._-]+", "-", filename)
    ascii_fallback = re.sub(r"-{2,}", "-", ascii_fallback).strip("-.")
    if not re.match(r"^[A-Za-z0-9]", ascii_fallback):
        # 纯中文标题会被压成 `v2-20260726.pdf` 之类，加回可识别的前缀。
        ascii_fallback = f"paperforge-{ascii_fallback}"
    header = f'{mode}; filename="{ascii_fallback}"'
    if filename != ascii_fallback:
        header += f"; filename*=UTF-8''{quote(filename, safe='')}"
    return header


_MEDIA_TYPES = {
    "pdf": ("application/pdf", "pdf"),
    "latex_zip": ("application/zip", "zip"),
    "markdown": ("text/markdown; charset=utf-8", "md"),
    "markdown_bundle": ("application/zip", "zip"),
    "bibtex": ("application/x-bibtex; charset=utf-8", "bib"),
    "docx": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "docx",
    ),
    # 编译日志要能在浏览器里直接打开看，不该强制下载成二进制。
    "compile_log": ("text/plain; charset=utf-8", "log"),
}


# ---- M5：雪球 / 全文 / 质量 / 润色 ----


@router.post(
    "/projects/{project_id}/snowball",
    response_model=JobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_snowball(
    project_id: str,
    request: SnowballRequest,
    session: SessionDep,
    queue: QueueDep,
) -> JobResponse:
    """引文雪球扩展：邻居入库为 candidate，需用户圈选后才进写作白名单。"""
    project = await _require_project(session, project_id)
    job = await create_job(session, project_id=project.id, kind="search")
    await _enqueue(
        queue,
        "run_snowball_pipeline",
        str(project.id),
        str(job.id),
        direction=request.direction,
        max_seeds=request.max_seeds,
    )
    return _job_response(job)


@router.post(
    "/projects/{project_id}/ingest",
    response_model=JobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_ingest(
    project_id: str,
    request: IngestRequest,
    session: SessionDep,
    queue: QueueDep,
) -> JobResponse:
    """OA 全文获取 → 解析 → 全文级卡片。只走 OA/官方渠道，不绕 paywall。"""
    project = await _require_project(session, project_id)
    job = await create_job(session, project_id=project.id, kind="ingest")
    await _enqueue(
        queue,
        "run_ingest_pipeline",
        str(project.id),
        str(job.id),
        max_works=request.max_works,
    )
    return _job_response(job)


@router.post(
    "/projects/{project_id}/quality/generate",
    response_model=JobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_quality(
    project_id: str,
    session: SessionDep,
    queue: QueueDep,
) -> JobResponse:
    project = await _require_project(session, project_id)
    job = await create_job(session, project_id=project.id, kind="write")
    await _enqueue(queue, "run_quality_pipeline", str(project.id), str(job.id))
    return _job_response(job)


@router.get("/projects/{project_id}/quality", response_model=QualityResponse)
async def get_quality(project_id: str, session: SessionDep) -> QualityResponse:
    """同步计算质量报告的确定性部分（软校验需要 LLM，走任务）。"""
    from paperforge_worker.pipelines.quality import build_quality_report, count_words

    project = await _require_project(session, project_id)
    whitelist = await get_writing_whitelist(session, project.id)
    document = await latest_document(session, project.id)
    rows = await list_sections(session, document.id) if document else []
    entries = await list_entries(session, project.id, status="selected")
    cards = await get_cards(session, project.id)

    frame_keys = {"abstract", "introduction", "conclusion"}
    sections = [
        {
            "section_key": row.section_key,
            "title": row.title,
            "word_count": count_words(
                " ".join(
                    run.get("v", "")
                    for block in (row.body_ir_json or {}).get("blocks", [])
                    for run in block.get("runs", [])
                    if run.get("t") == "text"
                )
            ),
            "cite_keys": row.cite_keys_json or [],
            "kind": "frame" if row.section_key in frame_keys else "body",
        }
        for row in rows
    ]
    fulltext_used = sum(1 for card in cards.values() if card.fulltext_used)
    report = build_quality_report(
        sections=sections,
        whitelist_size=len(whitelist),
        publication_years=[w.publication_year for _e, w in entries if w.publication_year],
        fulltext_coverage=(fulltext_used / len(entries)) if entries else 0.0,
        scope=dict(project.scope_json or {}),
    )
    return QualityResponse(project_id=str(project.id), **report.to_payload())


@router.post("/projects/{project_id}/sections/{section_key}/refine", response_model=RefineResponse)
async def refine_text(
    project_id: str,
    section_key: str,
    request: RefineRequest,
    session: SessionDep,
) -> RefineResponse:
    """编辑器润色浮条（polisher 角色）。

    红线：润色**不得**新增引用键、不得改动任何数字——改动后一旦发现数字变化，
    直接放弃改写并原样返回（宁可不润色，也不让数字漂移）。
    """
    from llm_runtime import LLMRunner
    from paperforge_worker.pipelines.writing import extract_numbers

    from paperforge_api.config import get_settings as api_settings

    await _require_project(session, project_id)
    del section_key
    original = request.text.strip()
    if not original:
        raise HTTPException(status_code=422, detail="text must not be empty")

    runner = LLMRunner(api_settings().llm_config())
    if not runner.enabled:
        return RefineResponse(
            action=request.action,
            original=original,
            refined=original,
            changed=False,
            note="未配置 LLM provider：润色不可用，已原样返回",
        )

    goals = {
        "polish": "润色文字，使表达更准确流畅",
        "expand": "在不引入新事实的前提下展开论述",
        "shorten": "精简表达，保留全部论点",
        "academic_tone": "调整为学术书面语气",
    }
    response = await runner.agenerate(
        "polisher",
        system_prompt=(
            "你是学术论文润色助手。只输出改写后的正文纯文本，不要解释。"
            "绝对禁止：新增或修改任何数字、新增引用标记、引入原文没有的事实。"
        ),
        user_prompt=(
            f"目标：{goals.get(request.action, '润色')}\n"
            f"额外要求：{request.instruction or '无'}\n\n原文：\n{original}"
        ),
        max_output_tokens=2000,
        temperature=0.3,
        metadata={"stage": "refine", "action": request.action},
    )
    if response is None or not response.text.strip():
        return RefineResponse(
            action=request.action,
            original=original,
            refined=original,
            changed=False,
            note="润色调用失败，已原样返回",
        )

    refined = response.text.strip()
    if extract_numbers(refined) != extract_numbers(original):
        # 数字红线优先于文采。
        return RefineResponse(
            action=request.action,
            original=original,
            refined=original,
            changed=False,
            note="改写改动了正文数字，已放弃本次润色",
        )
    return RefineResponse(
        action=request.action,
        original=original,
        refined=refined,
        changed=refined != original,
    )
