from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from paper_ir.schema import (
    AlgorithmBlock,
    CiteRun,
    EquationBlock,
    FigureBlock,
    ListBlock,
    MathInlineRun,
    PaperIR,
    ParagraphBlock,
    Section,
    TableBlock,
    TextRun,
    TodoBlock,
    XRefRun,
)

from latex_render.escape import latex_escape, latex_identifier

# PaperIR → LaTeX 渲染（设计 §4.6）。渲染器只消费 IR，不消费 LLM 原始输出。
#
# 表格与图由**代码**从 `user_asset.parsed_json` 确定性展开（设计 §4.4.2）：
# LLM 只写 caption 与分析文字，绝不书写单元格数值——这是「不编造实验数据」红线
# 在渲染层的落点。素材缺失时渲染显式 TODO 占位，而不是编一张表出来。

_SECTION_CMD = {1: "section", 2: "subsection", 3: "subsubsection"}
MAX_TABLE_ROWS_IN_PDF = 40
MAX_TABLE_COLUMNS_IN_PDF = 8


def _render_run(run: TextRun | CiteRun | MathInlineRun | XRefRun) -> str:
    if isinstance(run, TextRun):
        # 强调是**结构化标记**，正文照常转义后再包命令——LLM 无法借此注入
        # 任意 LaTeX（设计 §4.5：自由 LaTeX 只允许出现在 equation/algorithm）。
        text = latex_escape(run.v)
        for mark in run.marks:
            if mark == "bold":
                text = f"\\textbf{{{text}}}"
            elif mark == "italic":
                text = f"\\emph{{{text}}}"
        return text
    if isinstance(run, CiteRun):
        # cite 是原子节点；key 白名单在写作期已保证（R2），此处仅确定性展开。
        keys = [latex_identifier(key, prefix="cite") for key in run.keys]
        return f"\\cite{{{','.join(keys)}}}" if keys else ""
    if isinstance(run, MathInlineRun):
        return f"${run.v}$"
    if isinstance(run, XRefRun):
        return f"\\ref{{{latex_identifier(run.target, prefix='fig')}}}"
    return ""


def _render_block(block, assets: Mapping[str, dict[str, Any]] | None = None) -> str:
    if isinstance(block, ParagraphBlock):
        return "".join(_render_run(r) for r in block.runs)
    if isinstance(block, EquationBlock):
        label = f"\n\\label{{{latex_identifier(block.label, prefix='eq')}}}" if block.label else ""
        return f"\\begin{{equation}}{label}\n{block.latex}\n\\end{{equation}}"
    if isinstance(block, FigureBlock):
        return _render_figure(block, assets or {})
    if isinstance(block, TableBlock):
        return _render_table(block, assets or {})
    if isinstance(block, AlgorithmBlock):
        return f"\\begin{{algorithm}}\n{block.latex}\n\\end{{algorithm}}"
    if isinstance(block, TodoBlock):
        # 纯生成模式：实验结果以显式占位符呈现，不编造数据（产品红线）。
        return f"\\todo{{{latex_escape(block.text)}}}"
    if isinstance(block, ListBlock):
        env = "enumerate" if block.ordered else "itemize"
        if not block.items:
            return ""
        items = "\n".join(
            f"  \\item {''.join(_render_run(r) for r in item.runs)}" for item in block.items
        )
        return f"\\begin{{{env}}}\n{items}\n\\end{{{env}}}"
    return ""


def _render_figure(block: FigureBlock, assets: Mapping[str, dict[str, Any]]) -> str:
    asset = assets.get(block.asset_ref) or {}
    path = str(asset.get("figure_path") or asset.get("filename") or "")
    caption = latex_escape(block.caption)
    label = f"\n  \\label{{{latex_identifier(block.label, prefix='fig')}}}" if block.label else ""
    environment = "figure*" if block.width == "full" else "figure"
    image_width = "\\textwidth" if block.width == "full" else "\\linewidth"
    if not path:
        # 素材缺失：显式占位，绝不 \includegraphics 一个不存在的文件（编译必挂）。
        return (
            f"\\begin{{{environment}}}[t]\n  \\centering\n"
            f"  \\todo{{缺少图片素材：{latex_escape(block.asset_ref)}}}\n"
            f"  \\caption{{{caption}}}{label}\n\\end{{{environment}}}"
        )
    return (
        f"\\begin{{{environment}}}[t]\n  \\centering\n"
        f"  \\includegraphics[width={image_width}]{{{path}}}\n"
        f"  \\caption{{{caption}}}{label}\n\\end{{{environment}}}"
    )


def _render_table(block: TableBlock, assets: Mapping[str, dict[str, Any]]) -> str:
    """从 parsed_json 确定性渲染 booktabs 表格。

    单元格内容原样取自素材解析结果（只做 LaTeX 转义），因此正文表格与素材
    天然 100% 一致；LLM 无法在此处改动任何数值。
    """
    caption = latex_escape(block.caption)
    label = f"\n  \\label{{{latex_identifier(block.label, prefix='tab')}}}" if block.label else ""
    ref = block.source.ref or ""
    asset = assets.get(ref) or {}
    headers = [str(h) for h in (asset.get("headers") or [])][:MAX_TABLE_COLUMNS_IN_PDF]
    rows = asset.get("rows") or []

    if not headers or not rows:
        return (
            "\\begin{table}[t]\n  \\centering\n"
            f"  \\caption{{{caption}}}{label}\n"
            f"  \\todo{{缺少表格素材：{latex_escape(ref)}}}\n\\end{{table}}"
        )

    column_spec = "l" + "r" * (len(headers) - 1)
    lines = [
        "\\begin{table}[t]",
        "  \\centering",
        f"  \\caption{{{caption}}}{label}",
        f"  \\begin{{tabular}}{{{column_spec}}}",
        "    \\toprule",
        "    " + " & ".join(latex_escape(h) for h in headers) + " \\\\",
        "    \\midrule",
    ]
    for row in rows[:MAX_TABLE_ROWS_IN_PDF]:
        cells = [latex_escape(str(cell)) for cell in row[: len(headers)]]
        cells += [""] * (len(headers) - len(cells))
        lines.append("    " + " & ".join(cells) + " \\\\")
    lines += ["    \\bottomrule", "  \\end{tabular}", "\\end{table}"]
    if len(rows) > MAX_TABLE_ROWS_IN_PDF:
        lines.insert(
            -1,
            f"  % [paperforge] table truncated to {MAX_TABLE_ROWS_IN_PDF} rows "
            f"(source has {len(rows)})",
        )
    return "\n".join(lines)


def render_section(
    section: Section,
    assets: Mapping[str, dict[str, Any]] | None = None,
) -> str:
    """渲染单个章节（供工程装配按章节拆文件）。"""
    cmd = _SECTION_CMD.get(section.level, "section")
    label = latex_identifier(section.key, prefix="sec")
    parts = [f"\\{cmd}{{{latex_escape(section.title)}}}\\label{{{label}}}"]
    for block in section.blocks:
        parts.append(_render_block(block, assets))
    return "\n\n".join(p for p in parts if p)


def render_body(
    ir: PaperIR,
    assets: Mapping[str, dict[str, Any]] | None = None,
) -> str:
    """渲染 IR 的正文 body（不含导言区/模板骨架）。"""
    return "\n\n".join(render_section(s, assets) for s in ir.sections)
