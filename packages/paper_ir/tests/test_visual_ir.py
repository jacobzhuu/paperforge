import pytest
from paper_ir import FigureBlock, PaperIR, PaperMeta, ParagraphBlock, Section, TextRun, XRefRun
from paper_ir.markdown import render_markdown


def test_figure_and_xref_collect_and_render_deterministically() -> None:
    ir = PaperIR(
        meta=PaperMeta(title="Visual paper", language="zh"),
        sections=[
            Section(
                key="results",
                title="结果",
                blocks=[
                    ParagraphBlock(runs=[TextRun(v="如"), XRefRun(target="fig:va_deadbeef")]),
                    FigureBlock(
                        asset_ref="va_deadbeef",
                        caption="主要结果",
                        alt_text="三组结果的柱状图",
                        label="fig:va_deadbeef",
                        width="full",
                    ),
                ],
            )
        ],
    )
    assert ir.collect_asset_refs() == {"va_deadbeef"}
    markdown = render_markdown(
        ir,
        asset_urls={"va_deadbeef": "figures/va_deadbeef.png"},
    )
    assert "如图 1" in markdown
    assert "![三组结果的柱状图](figures/va_deadbeef.png)" in markdown
    assert "图 1　主要结果" in markdown


def test_duplicate_labels_are_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate PaperIR label"):
        PaperIR(
            meta=PaperMeta(title="duplicate"),
            sections=[
                Section(
                    key="s1",
                    title="one",
                    blocks=[FigureBlock(asset_ref="ua_11111111", label="fig:same")],
                ),
                Section(
                    key="s2",
                    title="two",
                    blocks=[FigureBlock(asset_ref="ua_22222222", label="fig:same")],
                ),
            ],
        )
