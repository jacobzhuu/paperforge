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
