from __future__ import annotations

import hashlib
import json
from pathlib import Path

from paperforge_worker.pipelines.export import inspect_pdf_layout
from pypdf import PdfReader

REPO_ROOT = Path(__file__).resolve().parents[3]
BASELINES = Path(__file__).parent / "fixtures" / "paper_quality_baselines.json"


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
