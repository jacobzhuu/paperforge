from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from paper_ir.schema import (
    AlgorithmBlock,
    CiteRun,
    EquationBlock,
    FigureBlock,
    GroundingRun,
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
_TABLE_SOFT_BREAK_CHARS = frozenset(r"\/._-:;,+|=()[]{}")

# 列宽分配。
#
# 此前的规则是「除第一列外一律等宽」，外加一条 `len(headers) == 4` 的硬编码
# 布局。实测代价：一张 16 行 × 6 列的文献矩阵占了 **5 页**——等宽把
# 0.117\linewidth（约 1.9cm）分给了「未报告」这种三个字的列，也分给了
# 60 字的研究标题列，于是每一行都被撑成七八行高。
#
# 现在按各列的实际内容长度按比例分配：短列只拿够用的宽度，长列拿走剩下的。
# `_TABLE_DEMAND_CAP` 让超长列不至于吞掉整张表（超过它的部分反正都要折行，
# 多给宽度的边际收益很小）；上下限保证没有一列窄到连表头都排不下。
_TABLE_DEMAND_CAP = 48.0
# 下限 0.085\linewidth ≈ 1.36cm，刚好放得下 4 个 \footnotesize 汉字
# （「未结构化」「证据等级」这类枚举值/表头恰好是这个长度）。取 0.07 时它们会
# 断成「未结构 / 化」。
_MIN_COLUMN_FRACTION = 0.085
# `p{}` 宽度不含列间的 2\tabcolsep。渲染时把 \tabcolsep 显式压到 3pt
# （宽表的通行做法），每个列间距因此是 6pt；\linewidth 在 A4 + 2.5cm 页边距
# 下是 455pt，6/455 ≈ 0.0132，留 0.016 的余量。
_TABLE_COLSEP_PT = 3
_INTERCOLUMN_FRACTION = 0.016
_TABLE_CONTENT_FRACTION = 0.98
# 列数多的表格降一档字号：宽度压力随列数增长，\footnotesize 大约再省 20%。
_WIDE_TABLE_COLUMNS = 5


def _display_width(text: str) -> float:
    """CJK 与全角标点按两个西文字符宽度计。"""
    return sum(2.0 if ord(ch) > 0x2E7F else 1.0 for ch in text)


def _column_fractions(headers: list[str], rows: list[Any]) -> list[float]:
    r"""按内容长度给每列分配 ``\linewidth`` 的份额。

    需求量取该列**第 85 百分位**的单元格宽度（而非最大值）：证据台账里偶尔
    一条特别长的 locator 不该把整列撑宽，让其余几十行都变窄。
    """
    count = len(headers)
    available = max(0.4, _TABLE_CONTENT_FRACTION - _INTERCOLUMN_FRACTION * (count - 1))
    if count == 1:
        return [available]
    # 上限 = 其余各列都退到下限时剩下的宽度。用固定常数（曾是 0.34）会在窄表上
    # 白白丢掉版面：两列表的两列都顶到 0.34，加起来只占了 0.68\linewidth。
    ceiling = available - _MIN_COLUMN_FRACTION * (count - 1)

    demands: list[float] = []
    for index, header in enumerate(headers):
        widths = sorted(_display_width(str(row[index])) for row in rows if index < len(row))
        cell_demand = (
            widths[min(len(widths) - 1, int(len(widths) * 0.85))] if widths else 0.0
        )
        demands.append(min(_TABLE_DEMAND_CAP, max(_display_width(header), cell_demand)))

    total = sum(demands) or float(count)
    fractions = [available * demand / total for demand in demands]

    # 夹到上下限后，缺口/余量只在还没被夹住的列之间按比例调整——直接整体
    # 归一化会把刚夹上的列又推回界外。
    for _ in range(count):
        free = [
            index
            for index, value in enumerate(fractions)
            if _MIN_COLUMN_FRACTION < value < ceiling
        ]
        fractions = [min(ceiling, max(_MIN_COLUMN_FRACTION, value)) for value in fractions]
        drift = available - sum(fractions)
        if abs(drift) < 1e-4 or not free:
            break
        free_total = sum(fractions[index] for index in free)
        for index in free:
            fractions[index] += drift * fractions[index] / free_total
    return fractions


# 浮动体位置说明符。
#
# 此前一律是 `[t]`——只允许放在页顶。LaTeX 放不下就把浮动体推进延迟队列，
# 队列只在 `\clearpage` / 文末冲刷，于是图片全部堆到了论文最后。
#
# `!` 让 LaTeX 忽略「一页最多几个浮动体、正文至少占多少」这类限制，
# `h` 允许就地放置——作者在第几个 block 插的图，就尽量出现在那里。
FLOAT_PLACEMENT = "!htbp"
# 双栏浮动体（figure*/table*）只能上页顶或单独成页，`h`/`b` 对它们无效。
_DOUBLE_FLOAT_PLACEMENT = "!tp"


def _render_run(run: TextRun | CiteRun | GroundingRun | MathInlineRun | XRefRun) -> str:
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
    if isinstance(run, GroundingRun):
        return ""
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
    return base, FLOAT_PLACEMENT


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


def _escape_table_cell(value: Any) -> str:
    """转义单元格，并在技术标识符的分隔符后加入零宽断行点。

    普通正文可以依赖词间空格换行；证据台账却常含 URL、文件路径、公式源码和
    ``fig:...`` locator。它们在窄 ``p{}`` 列里会成为一个不可分词的超长盒子，
    产生 50--110pt 的 Overfull hbox。先按原字符切段、再逐段转义，避免直接在
    已转义 LaTeX 上做替换而破坏 ``\\textbackslash{}`` 等命令。
    """
    raw = str(value)[:240]
    parts: list[str] = []
    start = 0
    for index, character in enumerate(raw):
        if character not in _TABLE_SOFT_BREAK_CHARS:
            continue
        parts.append(latex_escape(raw[start : index + 1]))
        parts.append("\\allowbreak{}")
        start = index + 1
    parts.append(latex_escape(raw[start:]))
    # p 列的首个单词默认可能无法断词；零宽盒让 TeX 从单元格开头就能分行。
    return "\\hspace{0pt}" + "".join(parts)


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
    # ``TableSource.data`` 声明为 ``dict[str, object]``，直接取值会得到 ``object``，
    # 既不可迭代也不可索引。表格素材本来就是无模式的 JSON，这里统一按
    # ``Mapping[str, Any]`` 消费。
    asset: Mapping[str, Any] = (
        block.source.data if block.source.kind == "inline" else assets.get(ref)
    ) or {}
    headers = [str(h) for h in (asset.get("headers") or [])][:MAX_TABLE_COLUMNS_IN_PDF]
    rows: list[Any] = list(asset.get("rows") or [])

    if not headers or not rows:
        return (
            f"\\begin{{table}}[{FLOAT_PLACEMENT}]\n  \\centering\n"
            f"  \\caption{{{caption}}}\n  {label}\n"
            f"  \\todo{{缺少表格素材：{latex_escape(ref or 'inline')}}}\n\\end{{table}}"
        )

    fractions = _column_fractions(headers, rows)
    column_spec = (
        "@{}" + "".join(f"p{{{value:.3f}\\linewidth}}" for value in fractions) + "@{}"
    )
    body_size = "\\footnotesize" if len(headers) >= _WIDE_TABLE_COLUMNS else "\\small"
    header_row = (
        "    "
        + " & ".join(f"{{\\raggedright\\bfseries {_escape_table_cell(h)}\\par}}" for h in headers)
        + " \\\\"
    )
    row_lines: list[str] = []
    # longtable can page-break and repeat its header, therefore dropping rows
    # would silently destroy the evidence ledger.  IEEE's tabular fallback is
    # the only non-pageable path that retains an explicit safety limit.
    rendered_rows = rows if not twocolumn else rows[:MAX_TABLE_ROWS_IN_PDF]
    for row in rendered_rows:
        cells = [
            f"{{\\raggedright {_escape_table_cell(cell)}\\par}}" for cell in row[: len(headers)]
        ]
        cells += [""] * (len(headers) - len(cells))
        row_lines.append("    " + " & ".join(cells) + " \\\\")

    # 普通 table/tabular 是不可分页盒子；较长的文献矩阵会继续排到页面边界之外，
    # Tectonic 仍返回成功，PDF 却把后半截直接裁掉。单栏模板改用 longtable，
    # 让 LaTeX 只在行与行之间分页，并在续页重复表头。
    if not twocolumn:
        lines = [
            "\\begingroup",
            f"  {body_size}",
            f"  \\setlength{{\\tabcolsep}}{{{_TABLE_COLSEP_PT}pt}}",
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
        return "\n".join(lines)

    # IEEE 双栏模式不支持 longtable；保留单栏内的表格浮动体行为。
    #
    # 这里曾对 5 列以上的表改用 `tabularx`——但 **没有任何模板加载 tabularx**
    # （见 templates/*.tex.j2 与 warmup/*.tex），运行时必然
    # `Environment tabularx undefined` 而编译失败。它当初的用意是「让各列分享
    # 剩余宽度」，而 `_column_fractions` 现在已经按内容把宽度分完了，
    # 普通 `tabular` 就够。
    begin_env = f"  \\begin{{tabular}}{{{column_spec}}}"
    end_env = "  \\end{tabular}"
    # 列多的表在 IEEE 单栏里放不下：正文栏宽只有 ~8.8cm，六列平均下来每列不到
    # 1.5cm，实测每页几十个 Overfull hbox。跨栏浮动体是 LaTeX 给这种表准备的
    # 出口（栏宽 8.8cm → 版心 17.8cm），而且 `\linewidth` 在 `table*` 里就等于
    # `\textwidth`，上面按 `\linewidth` 算出的列宽不用改。
    #
    # 图片刻意**不**走这条路（见 `_float_env`：带星浮动体只能上页顶或浮动页，
    # 会漂到文末）。但一张单栏放不下的表根本没有别的选择：不跨栏就是溢出版心。
    environment, placement = (
        ("table*", _DOUBLE_FLOAT_PLACEMENT)
        if len(headers) >= _WIDE_TABLE_COLUMNS
        else ("table", FLOAT_PLACEMENT)
    )
    lines = [
        f"\\begin{{{environment}}}[{placement}]",
        "  \\centering",
        f"  {body_size}",
        f"  \\setlength{{\\tabcolsep}}{{{_TABLE_COLSEP_PT}pt}}",
        f"  \\caption{{{caption}}}{label}",
        begin_env,
        "    \\toprule",
        header_row,
        "    \\midrule",
        *row_lines,
        "    \\bottomrule",
        end_env,
        f"\\end{{{environment}}}",
    ]
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
    # 摘要中的图表会被导出器保留为一个无标题的前置区块。不能为了保住图片
    # 再输出一次“摘要”章节标题，也不能让空标题产生一个编号章节。
    parts = (
        [f"\\{cmd}{{{latex_escape(section.title)}}}\\label{{{label}}}"]
        if section.title.strip()
        else []
    )
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
    parts: list[str] = []
    appendix_started = False
    for section in ir.sections:
        if section.appendix and not appendix_started:
            parts.append("\\appendix")
            appendix_started = True
        parts.append(render_section(section, assets, twocolumn=twocolumn))
    return "\n\n".join(parts)
