"""RENDER / 导出阶段：PaperIR → LaTeX 工程 → texd 编译 → PDF + 产物入库（设计 §4.6）。

Draft-first：编译失败也交付 LaTeX 工程 zip + Markdown 预览 + 编译日志，
绝不因为 PDF 编不出来就什么都不给。

R3：refs.bib 由库内元数据确定性生成，只消费持久化的 bibtex_key。
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import uuid
import zipfile
from dataclasses import dataclass, field
from typing import Any

from db import (
    get_project,
    get_writing_whitelist,
    latest_document,
    list_assets,
    list_entries,
    list_sections,
    list_visuals,
    reference_metadata_payload,
)
from db.models.paper import ExportArtifact
from latex_render import (
    LatexProject,
    TexdClient,
    build_latex_project,
    compile_with_repair,
    error_context,
    render_inline_bibliography,
    resolve_template,
    template_fallback_warning,
)
from observability import get_logger
from paper_ir import (
    Bibliography,
    PaperIR,
    PaperMeta,
    ReferenceMetadata,
    render_bibtex,
    render_markdown,
)
from paper_ir.schema import Section as IRSection

from paperforge_worker.context import JobContext

logger = get_logger(__name__)

EXPORT_FORMATS = ("pdf", "latex_zip", "markdown", "markdown_bundle", "bibtex", "docx")

# 编译日志不是用户「请求」的格式（不出现在 ExportRequest.formats 里），
# 但它必须可下载：PDF 编译失败时它是用户唯一能拿到的诊断材料。
LOG_FORMAT = "compile_log"

_LATEX_PATCH_PROMPT = """你是 LaTeX 编译修复助手。给定报错日志与出错文件内容，
只做**最小修补**：修正语法错误、去掉未定义命令。只输出 JSON：
{"files": {"相对路径": "修补后的完整文件内容"}}
禁止修改导言区（main.tex 的 \\documentclass 与 \\usepackage 区块），
禁止改动任何数字，禁止新增或删除引用键。"""


@dataclass
class ExportOutcome:
    formats: dict[str, str] = field(default_factory=dict)
    compile_ok: bool = False
    compile_rounds: int = 0
    repairs: list[dict[str, Any]] = field(default_factory=list)
    log_key: str | None = None
    template: str | None = None
    warnings: list[dict[str, Any]] = field(default_factory=list)
    # 书目是否排出来了。与 compile_ok 分开：引用全 [?] 的 PDF 也是「编译成功」的。
    bibliography_ok: bool = True
    readiness_status: str = "unassessed"
    layout_checks: dict[str, Any] = field(default_factory=dict)
    blockers: list[dict[str, Any]] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        return {
            "formats": self.formats,
            "compile_ok": self.compile_ok,
            "compile_rounds": self.compile_rounds,
            "repairs": self.repairs,
            "log_key": self.log_key,
            "template": self.template,
            "warnings": self.warnings,
            "bibliography_ok": self.bibliography_ok,
            "readiness_status": self.readiness_status,
            "layout_checks": self.layout_checks,
            "blockers": self.blockers,
        }


@dataclass(frozen=True)
class ExportQualityContext:
    profile: str = "draft"
    report_id: uuid.UUID | None = None
    readiness_status: str = "unassessed"
    paper_snapshot_hash: str | None = None


async def export_document(
    context: JobContext,
    *,
    formats: list[str] | None = None,
    store: Any,
    quality_profile: str = "draft",
    quality_report_id: str | None = None,
    readiness_status: str = "unassessed",
    paper_snapshot_hash: str | None = None,
) -> ExportOutcome:
    """渲染并导出。``store`` 是对象存储 seam（put/get）。"""
    outcome = ExportOutcome()
    wanted = [fmt for fmt in (formats or list(EXPORT_FORMATS)) if fmt in EXPORT_FORMATS]
    quality = ExportQualityContext(
        profile=quality_profile,
        report_id=uuid.UUID(quality_report_id) if quality_report_id else None,
        readiness_status=readiness_status,
        paper_snapshot_hash=paper_snapshot_hash,
    )
    outcome.readiness_status = readiness_status

    async with context.session() as session:
        project = await get_project(session, context.project_id)
        if project is None:
            outcome.warnings.append({"stage": "export", "reason": "project_not_found"})
            return outcome
        document = await latest_document(session, context.project_id)
        if document is None:
            outcome.warnings.append({"stage": "export", "reason": "no_document"})
            return outcome
        rows = await list_sections(session, document.id)
        whitelist = await get_writing_whitelist(session, context.project_id)
        used_keys = {key for row in rows for key in (row.cite_keys_json or [])}
        references: list[ReferenceMetadata] = []
        for entry, work in await list_entries(session, context.project_id, status="selected"):
            if entry.bibtex_key and entry.bibtex_key in used_keys:
                payload = await reference_metadata_payload(
                    session, work, bibtex_key=entry.bibtex_key
                )
                references.append(ReferenceMetadata(**payload))
        document_version = document.version
        title = project.publication_title or project.title
        authors = project.authors_json or []
        author_details = getattr(project, "author_details_json", None) or []
        keywords = project.keywords_json or []
        language = project.language
        citation_style = project.citation_style
        requested_template = project.venue_template
        template = resolve_template(requested_template)
        user_assets = await list_assets(session, context.project_id)
        visual_assets = await list_visuals(session, context.project_id, active_only=True)

    ir = _build_ir(
        rows,
        title=title,
        language=language,
        citation_style=citation_style,
        authors=authors,
        author_details=author_details,
        keywords=keywords,
    )
    # R2 收口：导出前再检查一次，渲染器永远拿不到白名单外的 cite key。
    stripped = ir.enforce_cite_key_whitelist(set(whitelist), strip=True)
    if stripped:
        outcome.warnings.append(
            {"stage": "export", "reason": "cite_keys_stripped", "count": len(stripped)}
        )

    outcome.template = template
    fallback = template_fallback_warning(requested_template)
    if fallback:
        outcome.warnings.append(fallback)
    render_assets, latex_figures, markdown_figures, provenance = _resolve_visual_files(
        ir,
        user_assets=user_assets,
        visual_assets=visual_assets,
        store=store,
    )
    project_files = build_latex_project(
        ir,
        references=references,
        template=template,
        citation_style=citation_style,
        assets=render_assets,
        figure_files=latex_figures,
    )
    project_files.with_file(
        "visual-provenance.json",
        json.dumps(provenance, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    outcome.warnings.extend(project_files.warnings)
    referenced_asset_refs = ir.collect_asset_refs()
    visual_preflight_issues = []
    for visual in visual_assets:
        if f"va_{str(visual.id)[:8]}" not in referenced_asset_refs:
            continue
        qa = (visual.renditions_json or {}).get("_provenance", {}).get("visual_qa")
        if not isinstance(qa, dict) or not qa.get("passed", False):
            visual_preflight_issues.append(
                {
                    "code": "visual_preflight_missing_or_failed",
                    "message": f"视觉 {visual.figure_label} 未通过内容边界、留白、字号与宽高比预检",
                    "visual_id": str(visual.id),
                }
            )

    if "markdown" in wanted:
        markdown = render_markdown(
            ir,
            references=references,
            style=citation_style,
            asset_urls={ref: path for ref, (path, _) in markdown_figures.items()},
        )
        outcome.formats["markdown"] = await _store(
            context,
            store,
            "markdown",
            markdown.encode("utf-8"),
            "md",
            document_version,
            quality=quality,
        )
    if "markdown_bundle" in wanted:
        markdown = render_markdown(
            ir,
            references=references,
            style=citation_style,
            asset_urls={ref: path for ref, (path, _) in markdown_figures.items()},
        )
        bundle = _markdown_bundle(markdown, markdown_figures, provenance)
        outcome.formats["markdown_bundle"] = await _store(
            context,
            store,
            "markdown_bundle",
            bundle,
            "zip",
            document_version,
            quality=quality,
        )
    if "bibtex" in wanted and references:
        try:
            outcome.formats["bibtex"] = await _store(
                context,
                store,
                "bibtex",
                render_bibtex(references).encode("utf-8"),
                "bib",
                document_version,
                quality=quality,
            )
        except ValueError as error:
            outcome.warnings.append({"stage": "bibtex", "reason": str(error)[:200]})
    if "latex_zip" in wanted:
        outcome.formats["latex_zip"] = await _store(
            context,
            store,
            "latex_zip",
            _zip_project(project_files),
            "zip",
            document_version,
            quality=quality,
        )

    if "docx" in wanted:
        markdown_text = render_markdown(
            ir,
            references=references,
            style=citation_style,
            asset_urls={ref: path for ref, (path, _) in markdown_figures.items()},
        )
        docx = _markdown_to_docx(markdown_text, markdown_figures)
        if docx is None:
            # pandoc 缺失是环境问题，不是内容问题：记降级标记，其余产物照常交付。
            outcome.warnings.append({"stage": "docx", "reason": "pandoc_unavailable"})
        else:
            outcome.formats["docx"] = await _store(
                context,
                store,
                "docx",
                docx,
                "docx",
                document_version,
                quality=quality,
            )

    if "pdf" in wanted:
        await _compile_pdf(
            context,
            store=store,
            project_files=project_files,
            outcome=outcome,
            document_version=document_version,
            # BibTeX 在沙箱里取不到 .bst 时的确定性兜底（R3 不变：条目仍由
            # 库内元数据生成，LLM 不参与）。没有它，PDF 会「编译成功」但
            # 全文引用都是 [?]。
            inline_bibliography=render_inline_bibliography(references, style=citation_style),
            expected_figure_count=sum(
                1
                for section in ir.sections
                for block in section.blocks
                if getattr(block, "type", None) == "figure"
            ),
            visual_preflight_issues=visual_preflight_issues,
            quality=quality,
        )
    return outcome


async def _compile_pdf(
    context: JobContext,
    *,
    store: Any,
    project_files: LatexProject,
    outcome: ExportOutcome,
    document_version: int,
    inline_bibliography: str = "",
    expected_figure_count: int = 0,
    visual_preflight_issues: list[dict[str, Any]] | None = None,
    quality: ExportQualityContext | None = None,
) -> None:
    quality = quality or ExportQualityContext()
    client = TexdClient(
        context.settings.texd_url,
        timeout_seconds=float(context.settings.texd_timeout_seconds),
    )
    try:
        import asyncio

        result = await asyncio.to_thread(
            compile_with_repair,
            project_files.files,
            client=client,
            entrypoint=project_files.entrypoint,
            patcher=_make_patcher(context),
            inline_bibliography=inline_bibliography or None,
            binary_files=project_files.binary_files,
        )
    except Exception as error:  # noqa: BLE001 - 编译失败绝不阻断导出
        logger.warning("compile crashed", extra={"error": type(error).__name__})
        outcome.warnings.append({"stage": "compile", "reason": type(error).__name__})
        outcome.readiness_status = "needs_revision" if quality.profile == "submission" else "draft"
        outcome.layout_checks = {
            "passed": False,
            "status": "compile_crashed",
            "blockers": [
                {"code": "pdf_compile_crashed", "message": "PDF 编译服务异常，无法完成版面验收"}
            ],
        }
        outcome.blockers = list(outcome.layout_checks["blockers"])
        await _update_quality_after_layout(
            context,
            quality=quality,
            readiness_status=outcome.readiness_status,
            layout_checks=outcome.layout_checks,
            blockers=outcome.blockers,
        )
        return
    finally:
        client.close()

    outcome.compile_ok = result.ok
    outcome.compile_rounds = result.rounds
    outcome.repairs = result.repairs
    outcome.bibliography_ok = result.bibliography_ok
    if any(repair.get("kind") == "inline_bibliography" for repair in result.repairs):
        # 用户有权知道成品用的是兜底书目而不是投稿方的 .bst 排版。
        outcome.warnings.append({"stage": "bibliography", "reason": "inline_bibliography_fallback"})
    elif not result.bibliography_ok:
        # 兜底也没用上（比如根本没有参考文献元数据）：引用会是 [?]，必须说清。
        outcome.warnings.append({"stage": "bibliography", "reason": "citations_unresolved"})
    if result.ok and result.pdf:
        source_constraints_ok = (
            sum(
                content.count("height=0.78\\textheight,keepaspectratio")
                for path, content in project_files.files.items()
                if path.startswith("sections/")
            )
            >= expected_figure_count
        )
        outcome.layout_checks = inspect_pdf_layout(
            result.pdf,
            compile_log=result.log,
            expected_figure_count=expected_figure_count,
            source_constraints_ok=source_constraints_ok,
            visual_preflight_issues=visual_preflight_issues or [],
        )
        layout_passed = bool(outcome.layout_checks.get("passed"))
        if quality.profile == "submission":
            outcome.readiness_status = "submission_ready" if layout_passed else "needs_revision"
            if not layout_passed:
                outcome.blockers = list(outcome.layout_checks.get("blockers") or [])
        await _update_quality_after_layout(
            context,
            quality=quality,
            readiness_status=outcome.readiness_status,
            layout_checks=outcome.layout_checks,
            blockers=outcome.blockers,
        )
        final_quality = ExportQualityContext(
            profile=quality.profile,
            report_id=quality.report_id,
            readiness_status=outcome.readiness_status,
            paper_snapshot_hash=quality.paper_snapshot_hash,
        )
        outcome.log_key = await _store(
            context,
            store,
            "compile_log",
            result.log.encode("utf-8"),
            "log",
            document_version,
            quality=final_quality,
        )
        outcome.formats["pdf"] = await _store(
            context,
            store,
            "pdf",
            result.pdf,
            "pdf",
            document_version,
            quality=final_quality,
        )
    else:
        outcome.readiness_status = "needs_revision" if quality.profile == "submission" else "draft"
        outcome.layout_checks = {
            "passed": False,
            "status": "compile_failed",
            "blockers": [
                {"code": "pdf_compile_failed", "message": "PDF 编译失败，无法完成版面验收"}
            ],
        }
        outcome.blockers = list(outcome.layout_checks["blockers"])
        await _update_quality_after_layout(
            context,
            quality=quality,
            readiness_status=outcome.readiness_status,
            layout_checks=outcome.layout_checks,
            blockers=outcome.blockers,
        )
        failed_quality = ExportQualityContext(
            profile=quality.profile,
            report_id=quality.report_id,
            readiness_status=outcome.readiness_status,
            paper_snapshot_hash=quality.paper_snapshot_hash,
        )
        outcome.log_key = await _store(
            context,
            store,
            "compile_log",
            result.log.encode("utf-8"),
            "log",
            document_version,
            quality=failed_quality,
        )
        # Draft-first：PDF 编不出来，交付修复后的工程 + 日志，前端展示报错行。
        outcome.warnings.append(
            {
                "stage": "compile",
                "reason": "compile_failed",
                "errors": error_context(result.log),
            }
        )
        outcome.formats["latex_zip"] = await _store(
            context,
            store,
            "latex_zip",
            _zip_project(
                LatexProject(
                    files=result.files or project_files.files,
                    binary_files=project_files.binary_files,
                )
            ),
            "zip",
            document_version,
            quality=failed_quality,
        )


def inspect_pdf_layout(
    pdf: bytes,
    *,
    compile_log: str,
    expected_figure_count: int,
    source_constraints_ok: bool,
    visual_preflight_issues: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """逐页检查缺字、题注、图文边界、异常空白页与引用解析。"""
    from pypdf import PdfReader

    blockers: list[dict[str, Any]] = []
    try:
        reader = PdfReader(io.BytesIO(pdf))
        page_texts = [(page.extract_text() or "") for page in reader.pages]
        image_counts: list[int] = []
        for page in reader.pages:
            try:
                image_counts.append(len(list(page.images)))
            except Exception:  # noqa: BLE001 - 个别 PDF image stream 无法解码时保守计 0
                image_counts.append(0)
    except Exception as error:  # noqa: BLE001 - 损坏 PDF 结构化返回，不泄漏异常
        return {
            "passed": False,
            "status": "pdf_unreadable",
            "blockers": [
                {"code": "pdf_unreadable", "message": f"PDF 无法解析：{type(error).__name__}"}
            ],
        }

    image_boxes: list[dict[str, Any]] = []
    image_bounds_violations: list[dict[str, Any]] = []
    try:
        import pypdfium2 as pdfium
        from pypdfium2 import raw as pdfium_c

        pdfium_document = pdfium.PdfDocument(pdf)
        for page_index in range(len(pdfium_document)):
            page = pdfium_document[page_index]
            page_width, page_height = page.get_width(), page.get_height()
            for image_index, item in enumerate(
                page.get_objects(filter=[pdfium_c.FPDF_PAGEOBJ_IMAGE]), start=1
            ):
                left, bottom, right, top = item.get_pos()
                box = {
                    "page": page_index + 1,
                    "image": image_index,
                    "box": [round(left, 2), round(bottom, 2), round(right, 2), round(top, 2)],
                    "page_size": [round(page_width, 2), round(page_height, 2)],
                }
                image_boxes.append(box)
                width = right - left
                height = top - bottom
                if (
                    left < -1
                    or bottom < -1
                    or right > page_width + 1
                    or top > page_height + 1
                    or width > page_width - 24
                    or height > page_height * 0.82
                ):
                    image_bounds_violations.append(box)
            page.close()
        pdfium_document.close()
    except Exception as error:  # noqa: BLE001 - 结构检查失败时保守阻断投稿
        image_bounds_violations.append(
            {"code": "image_bounds_check_failed", "error": type(error).__name__}
        )

    # TeX 会允许一个不可分页的 tabular 继续排到页面底部之外，并仍以成功状态
    # 生成 PDF。文本抽取也可能读到这些位于负坐标的内容，所以只看页数/日志会漏检。
    # 直接检查 PDF 页面文字对象的几何边界，捕获表格或正文被页面裁切的情况。
    text_bounds_violations: list[dict[str, Any]] = []
    text_bounds_check_error: str | None = None
    try:
        import pypdfium2 as pdfium
        from pypdfium2 import raw as pdfium_c

        pdfium_document = pdfium.PdfDocument(pdf)
        for page_index in range(len(pdfium_document)):
            page = pdfium_document[page_index]
            page_width, page_height = page.get_width(), page.get_height()
            for text_index, item in enumerate(
                page.get_objects(filter=[pdfium_c.FPDF_PAGEOBJ_TEXT]), start=1
            ):
                left, bottom, right, top = item.get_pos()
                if (
                    left < -1
                    or bottom < -1
                    or right > page_width + 1
                    or top > page_height + 1
                ):
                    text_bounds_violations.append(
                        {
                            "page": page_index + 1,
                            "text_object": text_index,
                            "box": [
                                round(left, 2),
                                round(bottom, 2),
                                round(right, 2),
                                round(top, 2),
                            ],
                            "page_size": [round(page_width, 2), round(page_height, 2)],
                        }
                    )
            page.close()
        pdfium_document.close()
    except Exception as error:  # noqa: BLE001 - 无法验证文字边界时投稿模式保守阻断
        text_bounds_check_error = type(error).__name__

    reference_page = next(
        (
            index
            for index, text in enumerate(page_texts)
            if re.search(r"(?:^|\n)\s*(?:References|Bibliography|参考文献)\s*(?:\n|$)", text, re.I)
        ),
        None,
    )
    caption_numbers = [
        match.group(1)
        for text in page_texts
        for match in re.finditer(r"(?:Figure|Fig\.|图)\s*([0-9]+)", text, re.I)
    ]
    caption_count = len(caption_numbers)
    empty_pages = [
        index + 1
        for index, (text, images) in enumerate(zip(page_texts, image_counts, strict=True))
        if len("".join(text.split())) < 20 and images == 0
    ]
    images_after_references = (
        sum(image_counts[reference_page:])
        if reference_page is not None and expected_figure_count
        else 0
    )
    missing_glyphs = bool(
        re.search(
            r"Missing character:|Unicode character .* not set up|does not contain requested Script",
            compile_log,
            re.I,
        )
    )
    unresolved_citations = bool(
        re.search(r"Citation .* undefined|There were undefined references", compile_log, re.I)
        or any("[?]" in text or "??" in text for text in page_texts)
    )
    if missing_glyphs:
        blockers.append({"code": "missing_glyphs", "message": "编译日志检测到 Unicode 缺字"})
    if unresolved_citations:
        blockers.append(
            {"code": "unresolved_citations", "message": "PDF 中存在未解析引用或交叉引用"}
        )
    if caption_count != expected_figure_count:
        blockers.append(
            {
                "code": "figure_caption_count_mismatch",
                "message": "PDF 题注数量与 FigureBlock 数量不一致",
                "expected": expected_figure_count,
                "actual": caption_count,
            }
        )
    if len(caption_numbers) != len(set(caption_numbers)):
        blockers.append({"code": "duplicate_figure_numbers", "message": "PDF 存在重复图号"})
    if images_after_references:
        blockers.append(
            {
                "code": "figures_after_references",
                "message": "参考文献起始页或其后仍有图片",
                "count": images_after_references,
            }
        )
    if empty_pages:
        blockers.append(
            {
                "code": "abnormal_blank_pages",
                "message": "PDF 存在异常空白页",
                "pages": empty_pages,
            }
        )
    if not source_constraints_ok:
        blockers.append({"code": "image_bounds_unconstrained", "message": "部分图片缺少宽高双约束"})
    if image_bounds_violations:
        blockers.append(
            {
                "code": "image_bounds_violation",
                "message": "PDF 中存在越出版心或高度异常的图片",
                "items": image_bounds_violations,
            }
        )
    if text_bounds_check_error:
        blockers.append(
            {
                "code": "text_bounds_check_failed",
                "message": "PDF 文字边界检查失败",
                "error": text_bounds_check_error,
            }
        )
    elif text_bounds_violations:
        blockers.append(
            {
                "code": "text_bounds_violation",
                "message": "PDF 中存在越出页面边界的文字，可能有表格或正文被裁切",
                "count": len(text_bounds_violations),
                "items": text_bounds_violations[:20],
            }
        )
    blockers.extend(visual_preflight_issues or [])
    return {
        "passed": not blockers,
        "status": "passed" if not blockers else "failed",
        "page_count": len(page_texts),
        "expected_figure_count": expected_figure_count,
        "caption_count": caption_count,
        "caption_numbers": caption_numbers,
        "reference_start_page": reference_page + 1 if reference_page is not None else None,
        "image_count_by_page": image_counts,
        "images_after_references": images_after_references,
        "empty_pages": empty_pages,
        "missing_glyphs": missing_glyphs,
        "unresolved_citations": unresolved_citations,
        "image_bounds_constrained": source_constraints_ok,
        "image_boxes": image_boxes,
        "image_bounds_violations": image_bounds_violations,
        "text_bounds_violations": text_bounds_violations[:20],
        "text_bounds_violation_count": len(text_bounds_violations),
        "text_bounds_check_error": text_bounds_check_error,
        "visual_preflight_issues": visual_preflight_issues or [],
        "blockers": blockers,
    }


async def _update_quality_after_layout(
    context: JobContext,
    *,
    quality: ExportQualityContext,
    readiness_status: str,
    layout_checks: dict[str, Any],
    blockers: list[dict[str, Any]],
) -> None:
    if quality.report_id is None:
        return
    from db.models.paper import QualityReportRecord

    async with context.session() as session:
        report = await session.get(QualityReportRecord, quality.report_id)
        if report is None or report.project_id != context.project_id:
            return
        report.layout_checks_json = layout_checks
        if quality.profile == "submission":
            report.readiness_status = readiness_status
            existing = [
                item
                for item in (report.blockers_json or [])
                if not str(item.get("code") or "").startswith(
                    (
                        "pdf_",
                        "figure_",
                        "duplicate_",
                        "figures_",
                        "abnormal_",
                        "image_",
                        "text_",
                        "missing_glyph",
                        "unresolved_",
                    )
                )
            ]
            report.blockers_json = existing + blockers
        metrics = dict(report.metrics_json or {})
        metrics["layout_checks"] = layout_checks
        metrics["readiness_status"] = report.readiness_status
        metrics["blockers"] = report.blockers_json or []
        report.metrics_json = metrics
        await session.flush()


def _make_patcher(context: JobContext):
    """LLM 最小修补器：确定性修复无计可施时才被调用。"""

    def patcher(files: dict[str, str], log: str) -> dict[str, str] | None:
        runner = context.llm_runner()
        if not runner.enabled:
            return None
        errors = error_context(log)
        if not errors:
            return None
        # 只把出错的章节文件交给模型，导言区不进上下文——防止模型改坏模板。
        candidates = {
            path: content for path, content in files.items() if path.startswith("sections/")
        }
        if not candidates:
            return None
        prompt = "\n\n".join(
            [
                f"Compile errors: {errors}",
                *[f"### {path}\n{content[:4000]}" for path, content in candidates.items()],
            ]
        )
        response = runner.generate(
            "writer",
            system_prompt=_LATEX_PATCH_PROMPT,
            user_prompt=prompt,
            max_output_tokens=3000,
            temperature=0.0,
            metadata={"stage": "latex_repair"},
        )
        if response is None:
            return None
        try:
            from llm_runtime import clean_and_parse_json

            parsed = clean_and_parse_json(response.text)
        except (ValueError, TypeError):
            return None
        patched = parsed.get("files") if isinstance(parsed, dict) else None
        if not isinstance(patched, dict):
            return None
        merged = dict(files)
        for path, content in patched.items():
            # 只接受章节文件的修补：导言区与 refs.bib 不容 LLM 触碰（R3）。
            if path in candidates and isinstance(content, str):
                merged[path] = content
        return merged if merged != files else None

    return patcher


def _build_ir(
    rows: list[Any],
    *,
    title: str,
    language: str,
    citation_style: str,
    authors: list[str] | None = None,
    author_details: list[dict[str, Any]] | None = None,
    keywords: list[str] | None = None,
) -> PaperIR:
    sections = [IRSection(**row.body_ir_json) for row in rows if row.body_ir_json]
    abstract_section = next((s for s in sections if s.key == "abstract"), None)
    abstract = ""
    if abstract_section is not None:
        abstract = " ".join(
            run.get("v", "")
            for block in abstract_section.model_dump(mode="json").get("blocks", [])
            for run in block.get("runs", [])
            if run.get("t") == "text"
        ).strip()
    body = [s for s in sections if s.key != "abstract"]
    return PaperIR(
        meta=PaperMeta(
            title=title,
            authors=authors or [],
            author_details=author_details or [],
            abstract=abstract,
            keywords=keywords or [],
            language=language,  # type: ignore[arg-type]
        ),
        sections=body,
        bibliography=Bibliography(style=citation_style),  # type: ignore[arg-type]
    )


def _resolve_visual_files(
    ir: PaperIR,
    *,
    user_assets: list[Any],
    visual_assets: list[Any],
    store: Any,
) -> tuple[
    dict[str, dict[str, Any]],
    dict[str, bytes],
    dict[str, tuple[str, bytes]],
    list[dict[str, Any]],
]:
    """把 IR 中实际使用的 ua/va 引用解析成编译与 Markdown 所需文件。

    只遍历 ``collect_asset_refs`` 的结果；未批准的 proposal 即使存在数据库中，
    只要没有进入 PaperIR 就不会被导出。
    """
    user_index: dict[str, Any] = {}
    for asset in user_assets:
        user_index[str(asset.id)] = asset
        user_index[f"ua_{str(asset.id)[:8]}"] = asset
    visual_index: dict[str, Any] = {}
    for visual in visual_assets:
        visual_index[str(visual.id)] = visual
        visual_index[f"va_{str(visual.id)[:8]}"] = visual

    render_assets: dict[str, dict[str, Any]] = {}
    latex_files: dict[str, bytes] = {}
    markdown_files: dict[str, tuple[str, bytes]] = {}
    provenance: list[dict[str, Any]] = []

    for asset_ref in sorted(ir.collect_asset_refs()):
        user_asset = user_index.get(asset_ref)
        if user_asset is not None and user_asset.object_key:
            try:
                content = store.get(user_asset.object_key)
            except (FileNotFoundError, ValueError):
                continue
            parsed = user_asset.parsed_json or {}
            if parsed.get("headers") and parsed.get("rows"):
                # 表格素材不需要复制进 LaTeX 工程；渲染器直接消费已解析的结构化数据。
                render_assets[asset_ref] = dict(parsed)
                provenance.append(
                    {
                        "asset_ref": asset_ref,
                        "kind": "uploaded_table",
                        "source_object_key": user_asset.object_key,
                        "sha256": hashlib.sha256(content).hexdigest(),
                    }
                )
                continue
            suffix = _image_suffix(content)
            if suffix is None:
                continue
            path = f"figures/{_safe_asset_stem(asset_ref)}.{suffix}"
            render_assets[asset_ref] = {**(user_asset.parsed_json or {}), "figure_path": path}
            latex_files[path] = content
            markdown_files[asset_ref] = (path, content)
            provenance.append(
                {
                    "asset_ref": asset_ref,
                    "kind": "uploaded",
                    "source_object_key": user_asset.object_key,
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
            )
            continue

        visual = visual_index.get(asset_ref)
        if visual is None:
            continue
        renditions = visual.renditions_json or {}
        latex_format = "png" if visual.kind == "ai_image" else "pdf"
        latex_item = renditions.get(latex_format) or renditions.get("png")
        markdown_item = renditions.get("png") or latex_item
        if not isinstance(latex_item, dict) or not isinstance(markdown_item, dict):
            continue
        try:
            latex_content = store.get(str(latex_item["object_key"]))
            markdown_content = store.get(str(markdown_item["object_key"]))
        except (FileNotFoundError, KeyError, ValueError):
            continue
        latex_suffix = _image_suffix(latex_content)
        markdown_suffix = _image_suffix(markdown_content)
        if latex_suffix is None or markdown_suffix is None:
            continue
        stem = _safe_asset_stem(asset_ref)
        latex_path = f"figures/{stem}.{latex_suffix}"
        markdown_path = f"figures/{stem}.{markdown_suffix}"
        render_assets[asset_ref] = {"figure_path": latex_path}
        latex_files[latex_path] = latex_content
        markdown_files[asset_ref] = (markdown_path, markdown_content)
        provenance.append(
            {
                "asset_ref": asset_ref,
                "visual_id": str(visual.id),
                "kind": visual.kind,
                "version": visual.version,
                "provider": visual.provider,
                "model": visual.model,
                "input_hash": visual.input_hash,
                "content_hash": visual.content_hash,
                "spec": visual.spec_json,
                "renditions": renditions,
            }
        )
    return render_assets, latex_files, markdown_files, provenance


def _image_suffix(content: bytes) -> str | None:
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if content.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if content.startswith(b"%PDF-"):
        return "pdf"
    return None


def _safe_asset_stem(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in value)
    return cleaned.strip("-") or "figure"


def _markdown_to_docx(
    markdown: str,
    figures: dict[str, tuple[str, bytes]] | None = None,
) -> bytes | None:
    """Markdown → docx（pandoc，设计 §4.6 次要导出：国内投稿场景）。

    pandoc 不可用时返回 None——导出整体不失败，只少一种格式（draft-first）。
    """
    import shutil
    import subprocess
    import tempfile
    from pathlib import Path

    if shutil.which("pandoc") is None:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "paper.md"
        target = Path(tmp) / "paper.docx"
        for _asset_ref, (relative_path, content) in (figures or {}).items():
            docx_path = relative_path
            docx_content = content
            if _image_suffix(content) == "pdf":
                converted = _pdf_first_page_png(content)
                if converted is None:
                    return None
                docx_path = f"{relative_path.rsplit('.', 1)[0]}.png"
                docx_content = converted
                markdown = markdown.replace(f"]({relative_path})", f"]({docx_path})")
            image_path = Path(tmp) / docx_path
            image_path.parent.mkdir(parents=True, exist_ok=True)
            image_path.write_bytes(docx_content)
        source.write_text(markdown, encoding="utf-8")
        try:
            proc = subprocess.run(
                ["pandoc", str(source), "-f", "markdown", "-o", str(target)],
                capture_output=True,
                cwd=tmp,
                timeout=120,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if proc.returncode != 0 or not target.exists():
            return None
        return target.read_bytes()


def _pdf_first_page_png(content: bytes) -> bytes | None:
    """把作为 FigureBlock 使用的上传 PDF 首页规范化为 DOCX 可嵌入的 PNG。"""
    import pypdfium2 as pdfium

    try:
        document = pdfium.PdfDocument(content)
        if len(document) == 0:
            return None
        page = document[0]
        bitmap = page.render(scale=300 / 72)
        image = bitmap.to_pil().convert("RGB")
        output = io.BytesIO()
        image.save(output, format="PNG", optimize=True, dpi=(300, 300))
        return output.getvalue()
    except (ValueError, RuntimeError, OSError):
        return None


def _zip_project(project: LatexProject) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, content in sorted(project.files.items()):
            archive.writestr(path, content)
        for path, content in sorted(project.binary_files.items()):
            archive.writestr(path, content)
        archive.writestr(
            "README.txt",
            "PaperForge 导出的 LaTeX 工程。\n"
            "编译：tectonic main.tex（或 latexmk -pdf main.tex）。\n"
            "refs.bib 由文献库元数据确定性生成，请勿手工改动引用键。\n",
        )
    return buffer.getvalue()


def _markdown_bundle(
    markdown: str,
    figures: dict[str, tuple[str, bytes]],
    provenance: list[dict[str, Any]],
) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("paper.md", markdown)
        for _asset_ref, (path, content) in sorted(figures.items()):
            archive.writestr(path, content)
        archive.writestr(
            "visual-provenance.json",
            json.dumps(provenance, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
    return buffer.getvalue()


async def _store(
    context: JobContext,
    store: Any,
    kind: str,
    data: bytes,
    suffix: str,
    document_version: int,
    *,
    quality: ExportQualityContext | None = None,
) -> str:
    quality = quality or ExportQualityContext()
    digest = hashlib.sha256(data).hexdigest()
    if context.owner_id is None:
        raise ValueError("project owner is unavailable")
    key = (
        f"users/{context.owner_id}/projects/{context.project_id}/exports/"
        f"v{document_version}/{kind}-{digest[:12]}.{suffix}"
    )
    store.put(key, data)
    # 编译日志此前被刻意跳过登记，于是文件躺在对象存储里但 `GET /exports` 列不出、
    # 也没有下载地址——而界面在编译失败时明确承诺「已交付 LaTeX 工程与编译日志」。
    # 承诺一份取不到的诊断材料，恰恰是在最需要建立信任的时刻失信。
    # `export_artifact.format` 是无约束的 String(16)，登记它不需要迁移。
    if kind in EXPORT_FORMATS or kind == LOG_FORMAT:
        async with context.session() as session:
            session.add(
                ExportArtifact(
                    project_id=context.project_id,
                    document_version=document_version,
                    format=kind,
                    object_key=key,
                    content_hash=digest,
                    quality_report_id=quality.report_id,
                    quality_profile=quality.profile,
                    readiness_status=quality.readiness_status,
                    paper_snapshot_hash=quality.paper_snapshot_hash,
                )
            )
    return key


def artifact_id() -> uuid.UUID:
    return uuid.uuid4()
