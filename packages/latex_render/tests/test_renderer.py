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
