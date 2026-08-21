from __future__ import annotations

import base64
import io
import shutil
import zipfile
from types import SimpleNamespace

import pytest
from paperforge_worker.pipelines.export import (
    EVIDENCE_LEDGER_KEY,
    MAX_FIGURE_FILES,
    _build_ir,
    _enforce_binary_channel_limits,
    _ledger_ir,
    _markdown_bundle,
    _markdown_to_docx,
)
from pypdf import PdfWriter


def _png() -> bytes:
    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUB"
        "AScY42YAAAAASUVORK5CYII="
    )


def _pdf() -> bytes:
    output = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=144, height=72)
    writer.write(output)
    return output.getvalue()


def test_abstract_figure_is_preserved_as_front_matter_for_export() -> None:
    rows = [
        SimpleNamespace(
            body_ir_json={
                "key": "abstract",
                "level": 1,
                "title": "摘要",
                "blocks": [
                    {"type": "paragraph", "runs": [{"t": "text", "v": "摘要正文"}]},
                    {
                        "type": "figure",
                        "asset_ref": "va_summary",
                        "caption": "摘要图",
                        "alt_text": "论文摘要图",
                        "label": "fig:summary",
                        "width": "full",
                    },
                ],
            }
        ),
        SimpleNamespace(
            body_ir_json={
                "key": "introduction",
                "level": 1,
                "title": "引言",
                "blocks": [{"type": "paragraph", "runs": [{"t": "text", "v": "正文"}]}],
            }
        ),
    ]

    ir = _build_ir(
        rows,
        title="论文",
        language="zh",
        citation_style="gbt7714",
    )

    assert ir.meta.abstract == "摘要正文"
    assert [section.key for section in ir.sections] == ["abstract-visuals", "introduction"]
    assert ir.sections[0].title == ""
    assert ir.collect_asset_refs() == {"va_summary"}


def test_too_many_figures_are_dropped_instead_of_killing_the_whole_compile() -> None:
    """texd 超限时拒收**整个请求**，日志里没有 TeX 输出，修复轮次救不回来。

    所以超出的图必须在这一侧就摘掉，让渲染器排占位——少几张图可以，
    整篇论文没有 PDF 不行。
    """
    count = MAX_FIGURE_FILES + 5
    render_assets = {
        f"va_{index}": {"figure_path": f"figures/va_{index:03d}.png"} for index in range(count)
    }
    latex_files = {str(asset["figure_path"]): _png() for asset in render_assets.values()}

    warnings = _enforce_binary_channel_limits(render_assets, latex_files)

    assert len(latex_files) == MAX_FIGURE_FILES
    assert warnings[0]["reason"] == "figures_dropped_for_compile_limits"
    assert warnings[0]["count"] == 5
    # 被摘掉的图不能再留着 figure_path，否则 \includegraphics 会指向
    # 工程里并不存在的文件——编译照样挂。
    kept = set(latex_files)
    for asset in render_assets.values():
        path = asset.get("figure_path")
        assert path is None or path in kept


def test_oversized_figure_is_dropped_but_the_others_survive() -> None:
    render_assets = {
        "va_ok": {"figure_path": "figures/ok.png"},
        "va_huge": {"figure_path": "figures/huge.png"},
    }
    latex_files = {
        "figures/ok.png": _png(),
        "figures/huge.png": b"\x89PNG\r\n\x1a\n" + b"\0" * (17 * 1024 * 1024),
    }

    warnings = _enforce_binary_channel_limits(render_assets, latex_files)

    assert set(latex_files) == {"figures/ok.png"}
    assert warnings[0]["count"] == 1
    assert render_assets["va_ok"]["figure_path"] == "figures/ok.png"
    assert "figure_path" not in render_assets["va_huge"]


def test_figures_within_limits_are_left_completely_alone() -> None:
    render_assets = {"va_1": {"figure_path": "figures/a.png"}}
    latex_files = {"figures/a.png": _png()}
    assert _enforce_binary_channel_limits(render_assets, latex_files) == []
    assert set(latex_files) == {"figures/a.png"}
    assert render_assets["va_1"]["figure_path"] == "figures/a.png"


def test_markdown_bundle_contains_figures_and_provenance() -> None:
    bundle = _markdown_bundle(
        "# Paper\n\n![figure](figures/va_test.png)\n",
        {"va_test": ("figures/va_test.png", _png())},
        [{"asset_ref": "va_test", "kind": "chart"}],
    )
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        assert set(archive.namelist()) == {
            "paper.md",
            "figures/va_test.png",
            "visual-provenance.json",
        }
        assert archive.read("figures/va_test.png").startswith(b"\x89PNG")
        assert b'"kind": "chart"' in archive.read("visual-provenance.json")


@pytest.mark.skipif(shutil.which("pandoc") is None, reason="pandoc is not installed")
def test_docx_really_embeds_relative_figure() -> None:
    docx = _markdown_to_docx(
        "# Paper\n\n![accessible figure](figures/va_test.png)\n",
        {"va_test": ("figures/va_test.png", _png())},
    )
    assert docx is not None
    with zipfile.ZipFile(io.BytesIO(docx)) as archive:
        media = [name for name in archive.namelist() if name.startswith("word/media/")]
        assert media
        assert archive.read(media[0]).startswith(b"\x89PNG")


@pytest.mark.skipif(shutil.which("pandoc") is None, reason="pandoc is not installed")
def test_docx_converts_uploaded_pdf_figure_to_embedded_png() -> None:
    docx = _markdown_to_docx(
        "# Paper\n\n![PDF figure](figures/ua_test.pdf)\n",
        {"ua_test": ("figures/ua_test.pdf", _pdf())},
    )
    assert docx is not None
    with zipfile.ZipFile(io.BytesIO(docx)) as archive:
        media = [name for name in archive.namelist() if name.startswith("word/media/")]
        assert len(media) == 1
        assert media[0].endswith(".png")
        assert archive.read(media[0]).startswith(b"\x89PNG")


def test_the_evidence_ledger_is_rendered_as_its_own_document() -> None:
    """It must never reach the manuscript IR, only its own artifact.

    One real review shipped a 68-row ledger of verbatim source excerpts as an
    appendix: 29,313 characters against 24,091 for the whole paper, and a
    42-page PDF for 5,250 words of prose.
    """
    ledger_row = SimpleNamespace(
        section_key=EVIDENCE_LEDGER_KEY,
        body_ir_json={
            "key": EVIDENCE_LEDGER_KEY,
            "level": 1,
            "title": "附录：证据台账",
            "appendix": True,
            "blocks": [
                {
                    "type": "paragraph",
                    "runs": [{"t": "text", "v": "逐条证据与定位。"}],
                }
            ],
        },
    )
    ir = _ledger_ir([ledger_row], title="序列推荐攻击", language="zh")
    assert [section.key for section in ir.sections] == [EVIDENCE_LEDGER_KEY]
    assert ir.meta.title.endswith("证据台账")


def test_the_manuscript_ir_excludes_the_ledger_section() -> None:
    rows = [
        SimpleNamespace(
            section_key="s1",
            body_ir_json={
                "key": "s1",
                "level": 1,
                "title": "攻击类型",
                "blocks": [{"type": "paragraph", "runs": [{"t": "text", "v": "正文。"}]}],
            },
        ),
        SimpleNamespace(
            section_key=EVIDENCE_LEDGER_KEY,
            body_ir_json={
                "key": EVIDENCE_LEDGER_KEY,
                "level": 1,
                "title": "附录：证据台账",
                "appendix": True,
                "blocks": [{"type": "paragraph", "runs": [{"t": "text", "v": "台账。"}]}],
            },
        ),
    ]
    # Mirrors the filter export_document applies before building the IR.
    manuscript = [row for row in rows if row.section_key != EVIDENCE_LEDGER_KEY]
    ir = _build_ir(manuscript, title="t", language="zh", citation_style="gbt7714")
    assert [section.key for section in ir.sections] == ["s1"]
