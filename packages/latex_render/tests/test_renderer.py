from latex_render import latex_escape, render_body
from paper_ir.schema import (
    CiteRun,
    EquationBlock,
    PaperIR,
    PaperMeta,
    ParagraphBlock,
    Section,
    TextRun,
    TodoBlock,
)


def test_escape_specials():
    assert latex_escape("a & b_c 100%") == r"a \& b\_c 100\%"


def test_unicode_science_characters_survive_instead_of_being_dropped():
    """Latin Modern 没有这些字形时 TeX **不报错**，只是把字符丢掉。

    结果是「编译成功、PDF 拿得到、化学式和数字却错了」——
    `H₂O` 变 `HO`，`10⁻³` 变 `10`。必须在渲染前换成等价 LaTeX。
    """
    assert latex_escape("H₂O") == "H$_{2}$O"
    assert latex_escape("π/4") == r"$\pi$/4"
    assert latex_escape("10⁻³ mol·L⁻¹") == r"10$^{-3}$ mol$\cdot$L$^{-1}$"
    assert latex_escape("α ≈ 0.05 ± 0.01") == r"$\alpha$ $\approx$ 0.05 $\pm$ 0.01"


def test_consecutive_subscripts_merge_into_one_group():
    """逐字符替换会得到 `$_1$$_2$`——双下标，TeX 报 `Double subscript` 直接挂。"""
    assert latex_escape("x₁₂") == "x$_{12}$"
    assert latex_escape("y⁻¹") == "y$^{-1}$"


def test_unicode_fallback_runs_after_escaping_not_before():
    """兜底产出的 `$`/`\\`/`{}` 是最终 LaTeX，不能再被转义器当正文处理。"""
    assert latex_escape("50% CO₂") == r"50\% CO$_{2}$"
    # 反斜杠仍然按正文转义，没有被兜底的 `\alpha` 之类污染。
    assert latex_escape("a\\b") == r"a\textbackslash{}b"


def test_zero_width_characters_are_removed():
    assert latex_escape("a​b﻿c") == "abc"


def test_render_body_cites_and_equation():
    ir = PaperIR(
        meta=PaperMeta(title="T"),
        sections=[
            Section(
                key="s1",
                level=1,
                title="引言",
                blocks=[
                    ParagraphBlock(
                        runs=[
                            TextRun(v="如前人所述 "),
                            CiteRun(keys=["wang2023survey"]),
                            TextRun(v="。"),
                        ]
                    ),
                    EquationBlock(latex="E = mc^2", label="eq:e"),
                ],
            )
        ],
    )
    body = render_body(ir)
    assert "\\section{引言}" in body
    assert "\\cite{wang2023survey}" in body
    assert "\\begin{equation}" in body
    assert "\\label{eq:e}" in body


def test_untitled_front_matter_renders_blocks_without_a_numbered_section() -> None:
    from paper_ir.schema import FigureBlock

    ir = PaperIR(
        meta=PaperMeta(title="T"),
        sections=[
            Section(
                key="abstract-visuals",
                title="",
                blocks=[
                    FigureBlock(
                        asset_ref="va_summary",
                        caption="摘要图",
                        alt_text="论文摘要图",
                        label="fig:summary",
                        width="full",
                    )
                ],
            )
        ],
    )
    body = render_body(
        ir,
        {"va_summary": {"figure_path": "figures/va_summary.png"}},
    )
    assert "\\section{" not in body
    assert "\\includegraphics" in body
    assert "figures/va_summary.png" in body


def test_todo_block_renders_placeholder_not_data():
    ir = PaperIR(
        meta=PaperMeta(title="T"),
        sections=[Section(key="exp", title="实验", blocks=[TodoBlock()])],
    )
    body = render_body(ir)
    assert "\\todo{待补充实验数据}" in body


def test_collect_cite_keys_for_r2_audit():
    ir = PaperIR(
        meta=PaperMeta(title="T"),
        sections=[
            Section(
                key="s1",
                title="a",
                blocks=[ParagraphBlock(runs=[CiteRun(keys=["a2020x", "b2021y"])])],
            )
        ],
    )
    assert ir.collect_cite_keys() == {"a2020x", "b2021y"}


def test_typed_ir_r2_guard_reports_then_strips_with_editor_marker():
    ir = PaperIR(
        meta=PaperMeta(title="T"),
        sections=[
            Section(
                key="s1",
                title="a",
                blocks=[ParagraphBlock(runs=[CiteRun(keys=["ok", "ghost"])])],
            )
        ],
    )
    violations = ir.enforce_cite_key_whitelist({"ok"})
    assert violations[0].rejected_keys == ("ghost",)
    assert ir.collect_cite_keys() == {"ok", "ghost"}

    ir.enforce_cite_key_whitelist({"ok"}, strip=True)
    assert ir.collect_cite_keys() == {"ok"}
    assert ir.sections[0].citation_warnings[0].rejected_keys == ("ghost",)


def test_renderer_sanitizes_labels_and_cite_keys_defensively():
    ir = PaperIR(
        meta=PaperMeta(title="T"),
        sections=[
            Section(
                key=r"sec}{\\input{evil}",
                title="unsafe",
                blocks=[
                    ParagraphBlock(runs=[CiteRun(keys=[r"safe}{\\input{evil}"])]),
                    EquationBlock(latex="x=1", label=r"eq}{\\input{evil}"),
                ],
            )
        ],
    )
    body = render_body(ir)
    assert "\\input" not in body
    assert r"\cite{safe-input-evil}" in body


def test_text_marks_render_deterministically_and_stay_escaped():
    """强调是结构化标记，不是 LLM 写的 LaTeX：内容照常转义后才包命令。"""
    ir = PaperIR(
        meta=PaperMeta(title="T"),
        sections=[
            Section(
                key="s1",
                title="marks",
                blocks=[
                    ParagraphBlock(
                        runs=[
                            TextRun(v="普通"),
                            TextRun(v="加粗", marks=["bold"]),
                            TextRun(v="斜体", marks=["italic"]),
                            TextRun(v="both", marks=["bold", "italic"]),
                            # 带标记的文本里出现特殊字符，仍必须被转义。
                            TextRun(v="100% \\evil", marks=["bold"]),
                        ]
                    )
                ],
            )
        ],
    )
    body = render_body(ir)
    assert "\\textbf{加粗}" in body
    assert "\\emph{斜体}" in body
    assert "\\emph{\\textbf{both}}" in body
    # 转义先于包裹：正文里的 % 与反斜杠不能变成活的 LaTeX。
    assert "\\evil" not in body.replace("\\textbackslash", "")
    assert "\\%" in body


def test_list_block_renders_itemize_and_enumerate_with_citations():
    from paper_ir.schema import ListBlock, ListItem

    ir = PaperIR(
        meta=PaperMeta(title="T"),
        sections=[
            Section(
                key="s1",
                title="lists",
                blocks=[
                    ListBlock(
                        ordered=True,
                        items=[
                            ListItem(runs=[TextRun(v="第一条"), CiteRun(keys=["ok2024"])]),
                            ListItem(runs=[TextRun(v="第二条", marks=["bold"])]),
                        ],
                    ),
                    ListBlock(ordered=False, items=[ListItem(runs=[TextRun(v="要点")])]),
                    # 空列表不该渲染出一个空环境（编译会告警）。
                    ListBlock(ordered=False, items=[]),
                ],
            )
        ],
    )
    body = render_body(ir)
    assert "\\begin{enumerate}" in body and "\\end{enumerate}" in body
    assert "\\begin{itemize}" in body and "\\end{itemize}" in body
    assert "\\item 第一条\\cite{ok2024}" in body
    assert "\\item \\textbf{第二条}" in body
    assert body.count("\\begin{itemize}") == 1


def test_r2_whitelist_covers_citations_inside_list_items():
    """列表项里的引用同样要过白名单——只查 ParagraphBlock 会给 R2 留绕过口子。"""
    from paper_ir.schema import ListBlock, ListItem

    ir = PaperIR(
        meta=PaperMeta(title="T"),
        sections=[
            Section(
                key="s1",
                title="lists",
                blocks=[
                    ListBlock(
                        ordered=False,
                        items=[ListItem(runs=[CiteRun(keys=["ok", "ghost"])])],
                    )
                ],
            )
        ],
    )
    assert ir.collect_cite_keys() == {"ok", "ghost"}

    violations = ir.enforce_cite_key_whitelist({"ok"})
    assert len(violations) == 1
    assert violations[0].rejected_keys == ("ghost",)
    assert ".items[0]" in violations[0].path

    ir.enforce_cite_key_whitelist({"ok"}, strip=True)
    assert ir.collect_cite_keys() == {"ok"}
    body = render_body(ir)
    assert "ghost" not in body


def _figure_ir(width: str = "full") -> PaperIR:
    from paper_ir.schema import FigureBlock

    return PaperIR(
        meta=PaperMeta(title="T"),
        sections=[
            Section(
                key="intro",
                level=1,
                title="引言",
                blocks=[
                    ParagraphBlock(runs=[TextRun(v="正文一段。")]),
                    FigureBlock(
                        asset_ref="va_abc",
                        caption="流程示意",
                        alt_text="流程示意图",
                        label="fig:va_abc",
                        width=width,
                    ),
                    ParagraphBlock(runs=[TextRun(v="正文另一段。")]),
                ],
            )
        ],
    )


ASSETS = {"va_abc": {"figure_path": "figures/va_abc.png"}}


def test_single_column_never_emits_cross_column_floats():
    """`figure*` 是跨栏浮动体，只能上页顶或单独成页。

    单栏文档（article / gbt7714）里用它没有任何收益，却让每张 width=full 的图
    进入独立的延迟队列，最终一起冲刷到论文末尾——这正是「图片集中在文末」的
    直接成因，而 AIImageSpec.width 默认就是 full。
    """
    latex = render_body(_figure_ir("full"), ASSETS, twocolumn=False)
    assert "\\begin{figure*}" not in latex
    assert "\\begin{figure}" in latex
    # 单栏下 full 就是「占满正文宽度」。
    assert "width=\\linewidth" in latex


def test_two_column_still_uses_cross_column_floats_for_full_width():
    latex = render_body(_figure_ir("full"), ASSETS, twocolumn=True)
    assert "\\begin{figure*}" in latex
    assert "width=\\textwidth" in latex


def test_figures_are_allowed_to_stay_where_the_author_put_them():
    """`[t]` 只允许页顶；放不下就推迟，推迟的队列在文末冲刷。

    `!htbp` 里的 `h` 允许就地放置，`!` 让 LaTeX 忽略每页浮动体配额。
    """
    latex = render_body(_figure_ir("column"), ASSETS, twocolumn=False)
    assert "\\begin{figure}[!htbp]" in latex
    assert "[t]" not in latex


def test_cross_column_floats_get_a_placement_they_can_actually_honour():
    # h/b 对跨栏浮动体无效，给了也会被忽略——只保留 t/p 并加 `!`。
    latex = render_body(_figure_ir("full"), ASSETS, twocolumn=True)
    assert "\\begin{figure*}[!tp]" in latex


def test_missing_asset_placeholder_keeps_the_same_placement():
    latex = render_body(_figure_ir("column"), {}, twocolumn=False)
    assert "\\begin{figure}[!htbp]" in latex
    # 素材缺失仍是显式占位，绝不 \includegraphics 一个不存在的文件。
    assert "\\includegraphics" not in latex
    assert "\\todo{" in latex


def test_single_column_tables_are_page_breakable():
    from paper_ir.schema import TableBlock, TableSource

    ir = PaperIR(
        meta=PaperMeta(title="T"),
        sections=[
            Section(
                key="results",
                level=1,
                title="结果",
                blocks=[
                    TableBlock(
                        caption="主结果",
                        label="tab:main",
                        source=TableSource(kind="user_asset", ref="ua_1"),
                    )
                ],
            )
        ],
    )
    latex = render_body(ir, {"ua_1": {"headers": ["m", "s"], "rows": [["A", "0.8"]]}})
    assert "\\begin{longtable}" in latex
    assert "\\endfirsthead" in latex
    assert "\\endhead" in latex
    assert "\\begin{table}" not in latex


def test_two_column_tables_keep_the_supported_float_environment():
    from paper_ir.schema import TableBlock, TableSource

    ir = PaperIR(
        meta=PaperMeta(title="T"),
        sections=[
            Section(
                key="results",
                level=1,
                title="Results",
                blocks=[
                    TableBlock(
                        caption="Main results",
                        label="tab:main",
                        source=TableSource(kind="user_asset", ref="ua_1"),
                    )
                ],
            )
        ],
    )
    latex = render_body(
        ir,
        {"ua_1": {"headers": ["m", "s"], "rows": [["A", "0.8"]]}},
        twocolumn=True,
    )
    assert "\\begin{table}[!htbp]" in latex
    assert "\\begin{longtable}" not in latex


def test_inline_literature_matrix_renders_without_external_asset():
    from paper_ir.schema import TableBlock, TableSource

    ir = PaperIR(
        meta=PaperMeta(title="T"),
        sections=[
            Section(
                key="synthesis",
                title="Synthesis",
                blocks=[
                    TableBlock(
                        caption="Evidence matrix",
                        source=TableSource(
                            kind="inline",
                            data={
                                "headers": ["Study", "Evidence"],
                                "rows": [["A", "Located full text"]],
                            },
                        ),
                    )
                ],
            )
        ],
    )
    latex = render_body(ir, {})
    assert "Evidence matrix" in latex
    assert "Located full text" in latex
    assert "\\todo{" not in latex
    assert "p{0.277\\linewidth}" in latex
    assert "\\begin{longtable}" in latex


def test_six_column_longtable_reserves_intercolumn_spacing_and_keeps_all_rows():
    from paper_ir.schema import TableBlock, TableSource

    rows = [[f"Study {index}", "task", "data", "model", "F1", "p.2"] for index in range(45)]
    ir = PaperIR(
        meta=PaperMeta(title="T"),
        sections=[
            Section(
                key="ledger",
                title="Evidence ledger",
                blocks=[
                    TableBlock(
                        caption="Ledger",
                        source=TableSource(
                            kind="inline", data={"headers": list("ABCDEF"), "rows": rows}
                        ),
                    )
                ],
            )
        ],
    )
    latex = render_body(ir, {})
    assert "p{0.200\\linewidth}" in latex
    assert "Study 44" in latex


def test_table_cells_add_safe_breakpoints_to_urls_paths_and_latex_source() -> None:
    from paper_ir.schema import TableBlock, TableSource

    ir = PaperIR(
        meta=PaperMeta(title="T"),
        sections=[
            Section(
                key="ledger",
                title="Ledger",
                blocks=[
                    TableBlock(
                        caption="Evidence",
                        source=TableSource(
                            kind="inline",
                            data={
                                "headers": ["Study", "Locator"],
                                "rows": [
                                    [
                                        "A",
                                        r"https://example.org/very_long_path/file_name.tex\\alpha_beta",
                                    ]
                                ],
                            },
                        ),
                    )
                ],
            )
        ],
    )
    latex = render_body(ir, {})

    assert latex.count("\\allowbreak{}") >= 8
    assert r"\textbackslash{}\allowbreak{}alpha\_\allowbreak{}beta" in latex
    assert r"\hspace{0pt}https:\allowbreak{}" in latex


def test_build_project_picks_column_layout_from_the_template():
    from latex_render.project import build_latex_project, is_two_column

    assert is_two_column("ieeetran") is True
    assert is_two_column("ieee") is True
    assert is_two_column("article") is False
    assert is_two_column("gbt7714") is False
    # cn_journal / cn_thesis 都落到单栏的 gbt7714 骨架上。
    assert is_two_column("cn_journal") is False

    single = build_latex_project(_figure_ir("full"), template="article", assets=ASSETS)
    body = "".join(v for k, v in single.files.items() if k.startswith("sections/"))
    assert "\\begin{figure*}" not in body

    double = build_latex_project(_figure_ir("full"), template="ieeetran", assets=ASSETS)
    body = "".join(v for k, v in double.files.items() if k.startswith("sections/"))
    assert "\\begin{figure*}" in body


def test_figure_has_dual_size_constraints_and_caption_number_is_cleaned():
    ir = _figure_ir("column")
    figure = ir.sections[0].blocks[1]
    figure.caption = "Figure 1: Evidence flow"
    latex = render_body(ir, ASSETS)
    assert "height=0.78\\textheight,keepaspectratio" in latex
    assert "\\caption{Evidence flow}" in latex
    assert "Figure 1" not in latex


def test_templates_flush_figures_before_bibliography():
    from latex_render.project import build_latex_project
    from paper_ir import ReferenceMetadata

    refs = [
        ReferenceMetadata(
            work_key="w1",
            bibtex_key="smith2020evidence",
            title="Evidence",
            publication_year=2020,
        )
    ]
    for template in ("article", "gbt7714", "ieeetran"):
        project = build_latex_project(
            _figure_ir("column"),
            template=template,
            references=refs,
        )
        main = project.files["main.tex"]
        assert main.index("\\clearpage") < main.index("\\bibliography{refs}")


def test_appendix_sections_start_latex_appendix_once():
    from latex_render.project import build_latex_project

    ir = PaperIR(
        meta=PaperMeta(title="T"),
        sections=[
            Section(key="body", title="Body"),
            Section(key="ledger", title="Evidence ledger", appendix=True),
            Section(key="notes", title="Additional notes", appendix=True),
        ],
    )
    assert render_body(ir, {}).count("\\appendix") == 1
    project = build_latex_project(ir, template="article")
    sections = "".join(v for key, v in project.files.items() if key.startswith("sections/"))
    assert sections.count("\\appendix") == 1
