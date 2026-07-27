from __future__ import annotations

import hashlib
import json
from pathlib import Path

from paperforge_worker.pipelines.export import inspect_pdf_layout
from pypdf import PdfReader

REPO_ROOT = Path(__file__).resolve().parents[3]
BASELINES = Path(__file__).parent / "fixtures" / "paper_quality_baselines.json"


def _pdf_with_text_at(y: int) -> bytes:
    """构造一个最小 PDF，用于验证页面外文字对象而不依赖外部生成器。"""
    content = (
        f"BT /F1 12 Tf 10 {y} Td "
        "(Visible text row) Tj ET"
    ).encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] "
            b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        ),
        (
            b"<< /Length "
            + str(len(content)).encode("ascii")
            + b" >>\nstream\n"
            + content
            + b"\nendstream"
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    pdf = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, item in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf += f"{index} 0 obj\n".encode("ascii") + item + b"\nendobj\n"
    xref = len(pdf)
    pdf += f"xref\n0 {len(objects) + 1}\n".encode("ascii")
    pdf += b"0000000000 65535 f \n"
    for offset in offsets[1:]:
        pdf += f"{offset:010d} 00000 n \n".encode("ascii")
    pdf += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n"
    ).encode("ascii")
    return bytes(pdf)


def test_pdf_layout_detects_text_below_the_page_boundary() -> None:
    layout = inspect_pdf_layout(
        _pdf_with_text_at(-20),
        compile_log="",
        expected_figure_count=0,
        source_constraints_ok=True,
    )
    assert layout["text_bounds_violation_count"] == 1
    assert any(item["code"] == "text_bounds_violation" for item in layout["blockers"])


def test_pdf_layout_accepts_text_inside_the_page_boundary() -> None:
    layout = inspect_pdf_layout(
        _pdf_with_text_at(80),
        compile_log="",
        expected_figure_count=0,
        source_constraints_ok=True,
    )
    assert layout["text_bounds_violation_count"] == 0
    assert not any(item["code"] == "text_bounds_violation" for item in layout["blockers"])


def test_three_review_artifacts_are_fixed_quality_baselines() -> None:
    rows = json.loads(BASELINES.read_text(encoding="utf-8"))
    assert [row["observed_readiness_score"] for row in rows] == [55, 33, 42]
    assert len({row["name"] for row in rows}) == 3
    for row in rows:
        assert row["expected_blockers"]
        artifact = REPO_ROOT / row["artifact"]
        if artifact.exists():
            pdf = artifact.read_bytes()
            assert hashlib.sha256(pdf).hexdigest() == row["sha256"]
            assert len(PdfReader(str(artifact)).pages) == row["observed_pages"]
            compile_log = (REPO_ROOT / row["compile_log"]).read_text(
                encoding="utf-8", errors="replace"
            )
            layout = inspect_pdf_layout(
                pdf,
                compile_log=compile_log,
                expected_figure_count=row["expected_figure_count"],
                source_constraints_ok=True,
            )
            assert [item["code"] for item in layout["blockers"]] == row[
                "expected_pdf_blockers"
            ]
