"""M4 测试：素材确定性解析 + 数字一致性 lint。

这是「系统在任何路径下都不生成虚构实验数值」这条产品红线的测试面：
反例（正文里出现素材中没有的数值、无素材却写了数字）必须被标记出来。
"""

from __future__ import annotations

import io
from zipfile import ZipFile

import pytest
from ingest import parse_asset
from ingest.assets import infer_asset_kind, is_number, normalize_number, parse_table
from ingest.numlint import build_asset_index, lint_sections, lint_text

CSV = b"model,accuracy,f1\nours,0.913,0.887\nbaseline,0.842,0.815\n"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _asset(content: bytes = CSV, filename: str = "results.csv", mime: str = "text/csv"):
    return parse_asset(content=content, filename=filename, mime_type=mime)


# ---- 解析 ----


def test_csv_parses_into_headers_rows_and_numeric_index() -> None:
    asset = _asset()
    assert asset.kind == "result_table"
    assert asset.parsed["headers"] == ["model", "accuracy", "f1"]
    assert asset.parsed["rows"][0] == ["ours", "0.913", "0.887"]
    # 数值索引是数字注入与 lint 的事实来源，键为「行标签::列名」。
    assert asset.parsed["numeric_cells"]["ours::accuracy"] == "0.913"
    assert "0.913" in asset.parsed["numbers"]


def test_numbers_keep_original_precision() -> None:
    asset = parse_asset(
        content=b"metric,value\nlatency,12.500\n",
        filename="r.csv",
        mime_type="text/csv",
    )
    # 不做四舍五入：正文引用的数值必须与素材逐字符一致。
    assert asset.parsed["numeric_cells"]["latency::value"] == "12.500"


def test_tsv_is_detected_by_extension() -> None:
    asset = parse_asset(
        content=b"model\taccuracy\nours\t0.9\n",
        filename="r.tsv",
        mime_type="text/tab-separated-values",
    )
    assert asset.parsed["headers"] == ["model", "accuracy"]


def test_xlsx_result_table_is_parsed() -> None:
    content_types = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>'
    )
    sheet = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        "<sheetData>"
        '<row r="1"><c r="A1" t="inlineStr"><is><t>model</t></is></c>'
        '<c r="B1" t="inlineStr"><is><t>accuracy</t></is></c></row>'
        '<row r="2"><c r="A2" t="inlineStr"><is><t>ours</t></is></c>'
        '<c r="B2" t="inlineStr"><is><t>0.931</t></is></c></row>'
        "</sheetData></worksheet>"
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="R" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    buffer = io.BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/worksheets/sheet1.xml", sheet)

    asset = parse_asset(content=buffer.getvalue(), filename="r.xlsx", mime_type=XLSX_MIME)
    assert asset.kind == "result_table"
    assert "0.931" in asset.parsed["numbers"]


def test_note_records_numbers_as_citable_facts() -> None:
    asset = parse_asset(
        content="我们在 128 个样本上重复了 5 次实验。".encode(),
        filename="method.md",
        mime_type="text/markdown",
    )
    assert asset.kind == "method_note"
    assert "128" in asset.parsed["numbers"]


def test_bib_asset_is_marked_as_requiring_verification() -> None:
    """R1：.bib 只是线索，不能直接入库。"""
    asset = parse_asset(
        content=b"@article{k, title={T}, author={A, B}, year={2020}, doi={10.1/x}}",
        filename="refs.bib",
        mime_type="application/x-bibtex",
    )
    assert asset.kind == "bib"
    assert asset.parsed["verification_required"] is True
    assert asset.parsed["entries"][0]["doi"] == "10.1/x"


def test_figure_asset_records_metadata_only() -> None:
    asset = parse_asset(content=b"\x89PNG\r\n", filename="arch.png", mime_type="image/png")
    assert asset.kind == "figure"
    assert asset.parsed["type"] == "figure"


def test_empty_table_degrades_with_warning() -> None:
    asset = parse_table(content=b"", filename="empty.csv", mime_type="text/csv")
    assert not asset.ok
    assert asset.warnings


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("results.csv", "result_table"),
        ("data.xlsx", "result_table"),
        ("arch.png", "figure"),
        ("refs.bib", "bib"),
        ("train.py", "code"),
        ("notes.md", "method_note"),
    ],
)
def test_asset_kind_inference(filename: str, expected: str) -> None:
    assert infer_asset_kind(filename=filename, mime_type="application/octet-stream") == expected


def test_declared_kind_wins_over_inference() -> None:
    assert infer_asset_kind(filename="x.csv", mime_type="text/csv", declared="dataset") == "dataset"


@pytest.mark.parametrize(
    ("value", "expected"),
    [("0.913", True), ("1,234", True), ("95%", True), ("1e-3", True), ("n/a", False), ("", False)],
)
def test_number_detection(value: str, expected: bool) -> None:
    assert is_number(value) is expected


def test_thousands_separator_is_normalized() -> None:
    assert normalize_number("1,234") == "1234"


# ---- NUMLINT ----


def test_sourced_numbers_pass_lint() -> None:
    asset = _asset()
    report = lint_sections(
        [{"section_key": "results", "text": "我们的方法达到 0.913 的准确率，基线为 0.842。"}],
        parsed_assets=[asset.parsed],
    )
    assert report.consistent
    assert len(report.sourced) == 2


def test_fabricated_number_is_flagged() -> None:
    """反例：素材里没有 0.999，正文却写了——必须标记。"""
    asset = _asset()
    report = lint_sections(
        [{"section_key": "results", "text": "我们的方法达到 0.999 的准确率。"}],
        parsed_assets=[asset.parsed],
    )
    assert not report.consistent
    assert report.unsourced[0].value == "0.999"
    assert report.unsourced[0].section_key == "results"


def test_without_assets_every_experimental_number_is_unsourced() -> None:
    """无素材时任何实验数值都无出处——这正是必须写占位符的原因。"""
    report = lint_sections(
        [{"section_key": "results", "text": "准确率提升到 87.4%，F1 达到 0.912。"}],
        parsed_assets=[],
    )
    assert not report.consistent
    assert {f.value for f in report.unsourced} == {"87.4%", "0.912"}


def test_placeholder_text_produces_no_findings() -> None:
    """纯生成模式的正确写法：写「结果待补充」而不是编数字。"""
    report = lint_sections(
        [{"section_key": "results", "text": "实验结果待补充，本节仅给出实验设计。"}],
        parsed_assets=[],
    )
    assert report.consistent


def test_years_and_ordinals_are_ignored() -> None:
    report = lint_sections(
        [
            {
                "section_key": "intro",
                "text": "自 2020 年以来（见表 1 与图 2），该方向快速发展。",
            }
        ],
        parsed_assets=[],
    )
    assert report.consistent


def test_asset_index_covers_cells_and_numbers() -> None:
    index = build_asset_index([_asset().parsed])
    assert index["0.913"].startswith("results.csv")
    assert "0.815" in index


def test_lint_text_reports_context_for_review() -> None:
    findings = lint_text(
        "在测试集上准确率为 0.777。",
        section_key="s1",
        asset_index={},
    )
    unsourced = [f for f in findings if f.status == "unsourced"]
    assert unsourced and "0.777" in unsourced[0].context


def test_note_numbers_survive_the_raw_text_fallback() -> None:
    """上传时 mime 常是 application/octet-stream：回退路径也必须登记数值，
    否则笔记里的真实数字会被 NUMLINT 误判为编造。"""
    asset = parse_asset(
        content="我们在 128 个样本上重复 5 次实验。".encode(),
        filename="method.md",
        mime_type="application/octet-stream",
    )
    assert asset.kind == "method_note"
    assert "128" in asset.parsed["numbers"]

    report = lint_sections(
        [{"section_key": "method", "text": "实验在 128 个样本上完成。"}],
        parsed_assets=[asset.parsed],
    )
    assert report.consistent


def test_review_number_may_come_from_located_fulltext_evidence() -> None:
    report = lint_sections(
        [{"section_key": "review", "text": "Recovery increased by 27%."}],
        parsed_assets=[],
        paper_type="review",
        literature_evidence=[
            {
                "cite_key": "smith2020",
                "text": "Recovery increased by 27%.",
                "located": True,
            }
        ],
    )
    assert report.consistent
    assert report.sourced[0].source_asset == "fulltext:smith2020"


def test_chemical_name_number_is_ignored() -> None:
    report = lint_sections(
        [{"section_key": "background", "text": "吲哚-3-乙酸参与根系信号传导。"}],
        parsed_assets=[],
        paper_type="review",
    )
    assert report.consistent


def test_model_gene_and_version_identifiers_are_not_experimental_numbers() -> None:
    report = lint_sections(
        [
            {
                "section_key": "background",
                "text": "GPT-12、IL-17、p53、3D 与 version 2.1 are identifiers, not results.",
            }
        ],
        parsed_assets=[],
        paper_type="review",
    )
    assert report.consistent
