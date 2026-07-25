"""PaperForge LaTeX 渲染：PaperIR → LaTeX 工程、模板库、编译与有界修复（设计 §4.6）。"""

from latex_render.compile import (
    ALLOWED_ENVIRONMENTS,
    MAX_REPAIR_ROUNDS,
    CompileOutcome,
    TexdClient,
    compile_with_repair,
    deterministic_repairs,
    error_context,
)
from latex_render.escape import latex_escape, latex_identifier
from latex_render.project import (
    DEFAULT_TEMPLATE,
    TEMPLATES,
    LatexProject,
    build_latex_project,
    render_markdown_fallback,
    resolve_template,
)
from latex_render.renderer import render_body, render_section

__all__ = [
    "ALLOWED_ENVIRONMENTS",
    "DEFAULT_TEMPLATE",
    "MAX_REPAIR_ROUNDS",
    "TEMPLATES",
    "CompileOutcome",
    "LatexProject",
    "TexdClient",
    "build_latex_project",
    "compile_with_repair",
    "deterministic_repairs",
    "error_context",
    "latex_escape",
    "latex_identifier",
    "render_body",
    "render_markdown_fallback",
    "render_section",
    "resolve_template",
]
