"""PaperIR → LaTeX 工程（设计 §4.6）。

`paper_ir → jinja2 模板 → LaTeX 工程(main.tex + sections/ + figures/ + refs.bib)`。
模板 = 主 .tex 骨架 + 环境白名单 + 宏包锁定清单，**LLM 不可修改导言区**：
模板文件是仓库资产，渲染时只往固定槽位填内容。

refs.bib 由 `paper_ir.bibtex.render_bibtex` 从库内元数据确定性生成（R3），
LLM 在任何环节都不书写参考文献条目。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from paper_ir import PaperIR, ReferenceMetadata, render_bibtex

from latex_render.escape import latex_escape
from latex_render.renderer import render_body, render_section

TEMPLATE_DIR = Path(__file__).parent / "templates"

# 模板名 → 骨架文件。venue_template 落在这些别名上（设计 §4.6 模板库 V1）。
TEMPLATES: dict[str, str] = {
    "article": "article.tex.j2",
    "ieeetran": "ieeetran.tex.j2",
    "ieee": "ieeetran.tex.j2",
    "gbt7714": "gbt7714.tex.j2",
    "zh": "gbt7714.tex.j2",
}
DEFAULT_TEMPLATE = "article"

# 引用样式 → BibTeX 书目风格（模板内可再降级）。
BIBSTYLE_BY_CITATION_STYLE = {
    "ieee": "IEEEtran",
    "apa": "apalike",
    "author_year": "plainnat",
    "gbt7714": "unsrt",
}


@dataclass
class LatexProject:
    """一个可编译的 LaTeX 工程：相对路径 → 文本内容。"""

    files: dict[str, str] = field(default_factory=dict)
    entrypoint: str = "main.tex"
    template: str = DEFAULT_TEMPLATE
    warnings: list[dict[str, Any]] = field(default_factory=list)

    def with_file(self, path: str, content: str) -> LatexProject:
        self.files[path] = content
        return self

    def to_payload(self) -> dict[str, Any]:
        return {"files": self.files, "entrypoint": self.entrypoint}


@lru_cache(maxsize=1)
def _environment() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
        autoescape=False,  # 输出是 LaTeX，不是 HTML；转义由 latex_escape 负责
    )


def resolve_template(name: str | None) -> str:
    key = (name or "").strip().lower()
    return key if key in TEMPLATES else DEFAULT_TEMPLATE


def build_latex_project(
    ir: PaperIR,
    *,
    references: Iterable[ReferenceMetadata] = (),
    template: str | None = None,
    citation_style: str | None = None,
    assets: Mapping[str, dict[str, Any]] | None = None,
    figure_files: Mapping[str, bytes] | None = None,
) -> LatexProject:
    """把 IR 渲染成完整工程。

    章节各成一个 `sections/*.tex` 并由 main.tex `\\input`——编译报错的行号
    能直接对应到章节，修复轮次才有的放矢。
    """
    template_name = resolve_template(template)
    project = LatexProject(template=template_name)
    refs = list(references)

    section_files: list[str] = []
    for index, section in enumerate(ir.sections):
        rel = f"sections/{index:02d}-{_safe_stem(section.key)}.tex"
        project.with_file(rel, render_section(section, assets) + "\n")
        section_files.append(rel)

    body = "\n".join(f"\\input{{{path[:-4]}}}" for path in section_files)

    if refs:
        try:
            project.with_file("refs.bib", render_bibtex(refs))
        except ValueError as error:
            # R3：缺持久化 key 属于契约破坏——不静默生成书目，改为交付无参考文献版本。
            project.warnings.append({"stage": "bibtex", "reason": str(error)[:200]})
            refs = []

    style = (citation_style or ir.bibliography.style or "author_year").lower()
    main = _environment().get_template(TEMPLATES[template_name]).render(
        title=latex_escape(ir.meta.title),
        authors=latex_escape(" \\and ".join(ir.meta.authors)) if ir.meta.authors else "",
        abstract=latex_escape(ir.meta.abstract),
        keywords=latex_escape(", ".join(ir.meta.keywords)),
        keywords_label="Keywords:" if ir.meta.language != "zh" else "关键词：",
        language=ir.meta.language,
        body=body,
        has_bibliography=bool(refs),
        bibstyle=BIBSTYLE_BY_CITATION_STYLE.get(style, "plainnat"),
    )
    project.with_file("main.tex", main)

    # 图片二进制由调用方投递（texd 的 files 只收文本，图片走 base64 通道）。
    for name in (figure_files or {}):
        project.warnings.append({"stage": "assets", "reason": f"binary figure pending: {name}"})
    return project


def render_markdown_fallback(ir: PaperIR) -> str:
    """编译彻底失败时的兜底预览（draft-first：永远交付得出东西）。"""
    from paper_ir import render_markdown

    return render_markdown(ir)


def _safe_stem(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in value).strip("-")
    return cleaned or "section"


__all__ = [
    "BIBSTYLE_BY_CITATION_STYLE",
    "DEFAULT_TEMPLATE",
    "TEMPLATES",
    "LatexProject",
    "build_latex_project",
    "render_body",
    "render_markdown_fallback",
    "resolve_template",
]
