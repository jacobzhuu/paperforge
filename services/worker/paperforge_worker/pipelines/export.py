"""RENDER / 导出阶段：PaperIR → LaTeX 工程 → texd 编译 → PDF + 产物入库（设计 §4.6）。

Draft-first：编译失败也交付 LaTeX 工程 zip + Markdown 预览 + 编译日志，
绝不因为 PDF 编不出来就什么都不给。

R3：refs.bib 由库内元数据确定性生成，只消费持久化的 bibtex_key。
"""

from __future__ import annotations

import hashlib
import io
import uuid
import zipfile
from dataclasses import dataclass, field
from typing import Any

from db import (
    get_project,
    get_writing_whitelist,
    latest_document,
    list_entries,
    list_sections,
    reference_metadata_payload,
)
from db.models.paper import ExportArtifact
from latex_render import (
    LatexProject,
    TexdClient,
    build_latex_project,
    compile_with_repair,
    error_context,
    resolve_template,
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

EXPORT_FORMATS = ("pdf", "latex_zip", "markdown", "bibtex", "docx")

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

    def to_payload(self) -> dict[str, Any]:
        return {
            "formats": self.formats,
            "compile_ok": self.compile_ok,
            "compile_rounds": self.compile_rounds,
            "repairs": self.repairs,
            "log_key": self.log_key,
            "template": self.template,
            "warnings": self.warnings,
        }


async def export_document(
    context: JobContext,
    *,
    formats: list[str] | None = None,
    store: Any,
) -> ExportOutcome:
    """渲染并导出。``store`` 是对象存储 seam（put/get）。"""
    outcome = ExportOutcome()
    wanted = [fmt for fmt in (formats or list(EXPORT_FORMATS)) if fmt in EXPORT_FORMATS]

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
        title = project.title
        language = project.language
        citation_style = project.citation_style
        template = resolve_template(project.venue_template)

    ir = _build_ir(
        rows,
        title=title,
        language=language,
        citation_style=citation_style,
    )
    # R2 收口：导出前再检查一次，渲染器永远拿不到白名单外的 cite key。
    stripped = ir.enforce_cite_key_whitelist(set(whitelist), strip=True)
    if stripped:
        outcome.warnings.append(
            {"stage": "export", "reason": "cite_keys_stripped", "count": len(stripped)}
        )

    outcome.template = template
    project_files = build_latex_project(
        ir,
        references=references,
        template=template,
        citation_style=citation_style,
    )
    outcome.warnings.extend(project_files.warnings)

    if "markdown" in wanted:
        markdown = render_markdown(ir, references=references, style=citation_style)
        outcome.formats["markdown"] = await _store(
            context, store, "markdown", markdown.encode("utf-8"), "md", document_version
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
            )
        except ValueError as error:
            outcome.warnings.append({"stage": "bibtex", "reason": str(error)[:200]})
    if "latex_zip" in wanted:
        outcome.formats["latex_zip"] = await _store(
            context, store, "latex_zip", _zip_project(project_files), "zip", document_version
        )

    if "docx" in wanted:
        markdown_text = render_markdown(ir, references=references, style=citation_style)
        docx = _markdown_to_docx(markdown_text)
        if docx is None:
            # pandoc 缺失是环境问题，不是内容问题：记降级标记，其余产物照常交付。
            outcome.warnings.append({"stage": "docx", "reason": "pandoc_unavailable"})
        else:
            outcome.formats["docx"] = await _store(
                context, store, "docx", docx, "docx", document_version
            )

    if "pdf" in wanted:
        await _compile_pdf(
            context,
            store=store,
            project_files=project_files,
            outcome=outcome,
            document_version=document_version,
        )
    return outcome


async def _compile_pdf(
    context: JobContext,
    *,
    store: Any,
    project_files: LatexProject,
    outcome: ExportOutcome,
    document_version: int,
) -> None:
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
        )
    except Exception as error:  # noqa: BLE001 - 编译失败绝不阻断导出
        logger.warning("compile crashed", extra={"error": type(error).__name__})
        outcome.warnings.append({"stage": "compile", "reason": type(error).__name__})
        return
    finally:
        client.close()

    outcome.compile_ok = result.ok
    outcome.compile_rounds = result.rounds
    outcome.repairs = result.repairs
    outcome.log_key = await _store(
        context, store, "compile_log", result.log.encode("utf-8"), "log", document_version
    )
    if result.ok and result.pdf:
        outcome.formats["pdf"] = await _store(
            context, store, "pdf", result.pdf, "pdf", document_version
        )
    else:
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
            _zip_project(LatexProject(files=result.files or project_files.files)),
            "zip",
            document_version,
        )


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
            path: content
            for path, content in files.items()
            if path.startswith("sections/")
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
        meta=PaperMeta(title=title, abstract=abstract, language=language),  # type: ignore[arg-type]
        sections=body,
        bibliography=Bibliography(style=citation_style),  # type: ignore[arg-type]
    )


def _markdown_to_docx(markdown: str) -> bytes | None:
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
        source.write_text(markdown, encoding="utf-8")
        try:
            proc = subprocess.run(
                ["pandoc", str(source), "-f", "markdown", "-o", str(target)],
                capture_output=True,
                timeout=120,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if proc.returncode != 0 or not target.exists():
            return None
        return target.read_bytes()


def _zip_project(project: LatexProject) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, content in sorted(project.files.items()):
            archive.writestr(path, content)
        archive.writestr(
            "README.txt",
            "PaperForge 导出的 LaTeX 工程。\n"
            "编译：tectonic main.tex（或 latexmk -pdf main.tex）。\n"
            "refs.bib 由文献库元数据确定性生成，请勿手工改动引用键。\n",
        )
    return buffer.getvalue()


async def _store(
    context: JobContext,
    store: Any,
    kind: str,
    data: bytes,
    suffix: str,
    document_version: int,
) -> str:
    digest = hashlib.sha256(data).hexdigest()
    key = f"projects/{context.project_id}/exports/v{document_version}/{kind}-{digest[:12]}.{suffix}"
    store.put(key, data)
    if kind != "compile_log":
        async with context.session() as session:
            session.add(
                ExportArtifact(
                    project_id=context.project_id,
                    document_version=document_version,
                    format=kind if kind in EXPORT_FORMATS else "latex_zip",
                    object_key=key,
                    content_hash=digest,
                )
            )
    return key


def artifact_id() -> uuid.UUID:
    return uuid.uuid4()
