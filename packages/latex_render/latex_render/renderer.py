from __future__ import annotations

import re
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

# 浮动体位置说明符。
#
# 此前一律是 `[t]`——只允许放在页顶。LaTeX 放不下就把浮动体推进延迟队列，
# 队列只在 `\clearpage` / 文末冲刷，于是图片全部堆到了论文最后。
#
# `!` 让 LaTeX 忽略「一页最多几个浮动体、正文至少占多少」这类限制，
# `h` 允许就地放置——作者在第几个 block 插的图，就尽量出现在那里。
_FLOAT_PLACEMENT = "!htbp"
# 双栏浮动体（figure*/table*）只能上页顶或单独成页，`h`/`b` 对它们无效。
_DOUBLE_FLOAT_PLACEMENT = "!tp"


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


def _render_block(
    block,
    assets: Mapping[str, dict[str, Any]] | None = None,
    twocolumn: bool = False,
) -> str:
    if isinstance(block, ParagraphBlock):
        return "".join(_render_run(r) for r in block.runs)
    if isinstance(block, EquationBlock):
        label = f"\n\\label{{{latex_identifier(block.label, prefix='eq')}}}" if block.label else ""
        return f"\\begin{{equation}}{label}\n{block.latex}\n\\end{{equation}}"
    if isinstance(block, FigureBlock):
        return _render_figure(block, assets or {}, twocolumn)
    if isinstance(block, TableBlock):
        return _render_table(block, assets or {}, twocolumn=twocolumn)
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


def _float_env(base: str, width: str, twocolumn: bool) -> tuple[str, str]:
    """选定浮动体环境与位置说明符。

    **只有真正的双栏文档才用 `figure*`/`table*`。** 这是图片跑到文末的主因：
    带星环境是「跨栏浮动体」，LaTeX 只肯把它放在页顶或单独的浮动页，且走一条
    独立的延迟队列。而 `article` / `gbt7714` 都是单栏（见各自模板的
    `\\documentclass`），在那里用 `figure*` 没有任何跨栏收益，只白白继承了
    最苛刻的排版限制——`AIImageSpec.width` 又默认 `full`，于是每张 AI 插图
    都撞上这条路径，最后一起冲刷到论文末尾。
    """
    if width == "full" and twocolumn:
        return f"{base}*", _DOUBLE_FLOAT_PLACEMENT
    return base, _FLOAT_PLACEMENT


def _render_figure(
    block: FigureBlock,
    assets: Mapping[str, dict[str, Any]],
    twocolumn: bool = False,
) -> str:
    asset = assets.get(block.asset_ref) or {}
    path = str(asset.get("figure_path") or asset.get("filename") or "")
    caption = latex_escape(_clean_caption(block.caption))
    label = f"\n  \\label{{{latex_identifier(block.label, prefix='fig')}}}" if block.label else ""
    environment, placement = _float_env("figure", block.width, twocolumn)
    # 单栏文档里 width=full 指的是「占满正文宽度」，用 \linewidth 即可，
    # 不需要跨栏环境。
    image_width = "\\textwidth" if environment.endswith("*") else "\\linewidth"
    if not path:
        # 素材缺失：显式占位，绝不 \includegraphics 一个不存在的文件（编译必挂）。
        return (
            f"\\begin{{{environment}}}[{placement}]\n  \\centering\n"
            f"  \\todo{{缺少图片素材：{latex_escape(block.asset_ref)}}}\n"
            f"  \\caption{{{caption}}}{label}\n\\end{{{environment}}}"
        )
    return (
        f"\\begin{{{environment}}}[{placement}]\n  \\centering\n"
        f"  \\includegraphics[width={image_width},height=0.78\\textheight,"
        f"keepaspectratio]{{{path}}}\n"
        f"  \\caption{{{caption}}}{label}\n\\end{{{environment}}}"
    )


def _clean_caption(value: str) -> str:
    return re.sub(
        r"^\s*(?:(?:图|表)\s*[一二三四五六七八九十百0-9]+|(?:figure|fig\.?|table)\s*[A-Z]?\d+)\s*[.:：、\-—]?\s*",
        "",
        " ".join((value or "").split()),
        flags=re.IGNORECASE,
    ).strip()


def _render_table(
    block: TableBlock,
    assets: Mapping[str, dict[str, Any]],
    *,
    twocolumn: bool = False,
) -> str:
    """从上传素材或内联比较矩阵确定性渲染 booktabs 表格。

    单元格内容原样取自结构化输入（只做 LaTeX 转义），LLM 不直接书写 LaTeX，
    也不能在渲染阶段改动任何数值。
    """
    caption = latex_escape(block.caption)
    label = f"\\label{{{latex_identifier(block.label, prefix='tab')}}}" if block.label else ""
    ref = block.source.ref or ""
    asset = block.source.data if block.source.kind == "inline" else assets.get(ref)
    asset = asset or {}
    headers = [str(h) for h in (asset.get("headers") or [])][:MAX_TABLE_COLUMNS_IN_PDF]
    rows = asset.get("rows") or []

    if not headers or not rows:
        return (
            f"\\begin{{table}}[{_FLOAT_PLACEMENT}]\n  \\centering\n"
            f"  \\caption{{{caption}}}\n  {label}\n"
            f"  \\todo{{缺少表格素材：{latex_escape(ref or 'inline')}}}\n\\end{{table}}"
        )

    if len(headers) == 1:
        column_spec = "@{}p{0.94\\linewidth}@{}"
    elif len(headers) == 4:
        # 文献矩阵常见的「研究/年份/方法/证据」布局：年份最窄、方法最宽，
        # 避免等宽 p 列把方法描述挤成大量断行。
        column_spec = (
            "@{}p{0.270\\linewidth}p{0.100\\linewidth}"
            "p{0.340\\linewidth}p{0.210\\linewidth}@{}"
        )
    else:
        first_width = 0.28 if len(headers) <= 4 else 0.2
        other_width = (0.92 - first_width) / (len(headers) - 1)
        column_spec = (
            f"@{{}}p{{{first_width:.3f}\\linewidth}}"
            + "".join(f"p{{{other_width:.3f}\\linewidth}}" for _ in headers[1:])
            + "@{}"
        )
    header_row = (
        "    "
        + " & ".join(f"{{\\raggedright\\bfseries {latex_escape(h)}\\par}}" for h in headers)
        + " \\\\"
    )
    row_lines: list[str] = []
    for row in rows[:MAX_TABLE_ROWS_IN_PDF]:
        cells = [
            f"{{\\raggedright {latex_escape(str(cell))}\\par}}" for cell in row[: len(headers)]
        ]
        cells += [""] * (len(headers) - len(cells))
        row_lines.append("    " + " & ".join(cells) + " \\\\")

    # 普通 table/tabular 是不可分页盒子；较长的文献矩阵会继续排到页面边界之外，
    # Tectonic 仍返回成功，PDF 却把后半截直接裁掉。单栏模板改用 longtable，
    # 让 LaTeX 只在行与行之间分页，并在续页重复表头。
    if not twocolumn:
        lines = [
            "\\begingroup",
            "  \\small",
            f"  \\begin{{longtable}}{{{column_spec}}}",
            f"    \\caption{{{caption}}}{label} \\\\",
            "    \\toprule",
            header_row,
            "    \\midrule",
            "    \\endfirsthead",
            "    \\toprule",
            header_row,
            "    \\midrule",
            "    \\endhead",
            "    \\midrule",
            "    \\endfoot",
            "    \\bottomrule",
            "    \\endlastfoot",
            *row_lines,
            "  \\end{longtable}",
            "\\endgroup",
        ]
        if len(rows) > MAX_TABLE_ROWS_IN_PDF:
            lines.insert(
                -2,
                f"  % [paperforge] table truncated to {MAX_TABLE_ROWS_IN_PDF} rows "
                f"(source has {len(rows)})",
            )
        return "\n".join(lines)

    # IEEE 双栏模式不支持 longtable；保留单栏内的表格浮动体行为。
    lines = [
        f"\\begin{{table}}[{_FLOAT_PLACEMENT}]",
        "  \\centering",
        "  \\small",
        f"  \\caption{{{caption}}}{label}",
        f"  \\begin{{tabular}}{{{column_spec}}}",
        "    \\toprule",
        header_row,
        "    \\midrule",
        *row_lines,
    ]
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
    *,
    twocolumn: bool = False,
) -> str:
    """渲染单个章节（供工程装配按章节拆文件）。

    `twocolumn` 决定 width=full 的图表是否使用跨栏浮动体。默认 False——
    单栏是更保守的假设：在双栏文档里少用一次 `figure*` 只是图窄一点，
    反过来在单栏文档里误用 `figure*` 会让图漂到文末。
    """
    cmd = _SECTION_CMD.get(section.level, "section")
    label = latex_identifier(section.key, prefix="sec")
    parts = [f"\\{cmd}{{{latex_escape(section.title)}}}\\label{{{label}}}"]
    for block in section.blocks:
        parts.append(_render_block(block, assets, twocolumn))
    return "\n\n".join(p for p in parts if p)


def render_body(
    ir: PaperIR,
    assets: Mapping[str, dict[str, Any]] | None = None,
    *,
    twocolumn: bool = False,
) -> str:
    """渲染 IR 的正文 body（不含导言区/模板骨架）。"""
    return "\n\n".join(render_section(s, assets, twocolumn=twocolumn) for s in ir.sections)
