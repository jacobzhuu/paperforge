#!/usr/bin/env python3
"""Live writer + TeX smoke test using an explicitly synthetic, source-bound fixture.

Run with the normal worker environment. No project, job or manuscript is changed.
Artifacts are written to --output-dir for manual inspection.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from latex_render import TexdClient, build_latex_project
from llm_runtime import LLMRunner
from paper_ir import PaperIR, PaperMeta, ReferenceMetadata, render_markdown
from paperforge_worker.config import WorkerSettings
from paperforge_worker.pipelines.scholarly_content import formula_catalog
from paperforge_worker.pipelines.writing import WritingContext, write_section


async def verify(args):
    settings = WorkerSettings(_env_file=args.env_file) if args.env_file else WorkerSettings()
    source = {
        "evidence_id": "fixture-e1",
        "work_id": "synthetic-fixture",
        "cite_key": "fixture2026",
        "title": "Synthetic acceptance fixture, not a published study",
        "grade": "B_located_prose",
        "page": 1,
        "section_path": "Methods",
        "text": r"This synthetic method minimizes the squared residual objective "
        r"$$L = \sum_{i=1}^{n} (y_i - \hat{y}_i)^2$$. "
        r"Here n is the number of observations, y_i is the observed response, "
        r"and \hat{y}_i is the predicted response. The observations are assumed "
        r"independent and the response is real valued. Each squared residual "
        r"contributes nonnegatively to the loss. Larger residuals contribute "
        r"more strongly. This objective alone does not establish robustness "
        r"to outliers or predictive generalization; those need separate evaluation.",
    }
    section = {
        "key": "methods",
        "kind": "body",
        "title": "Squared residual objective function",
        "question_id": "fixture-q",
        "cite_keys": ["fixture2026"],
        "target_words": 220,
        "summary": "Explain the supplied objective, define every variable and state assumptions. "
        "Include the central formula as a displayed equation. This is a synthetic fixture.",
        "argument_points": [
            "Define and explain the objective with its source formula",
            "Explain its assumptions and the limits of the conclusion",
        ],
    }
    context = WritingContext(
        language="en",
        outline={
            "sections": [section],
            "sub_question_bundles": [{"question_id": "fixture-q", "evidence": [source]}],
        },
        paper_type="review",
    )
    draft = await write_section(
        section=section,
        cards={},
        whitelist={"fixture2026"},
        context=context,
        runner=LLMRunner(settings.llm_config()),
    )
    assert draft.has_body, draft.failure_reason
    assert draft.equations, "Live writer did not select the central source formula"
    expected = {item["latex"] for item in formula_catalog([source], []).values()}
    assert all(item["latex"] in expected for item in draft.equations)
    assert all(item["explanation"] and item["evidence_ids"] for item in draft.equations)
    ir = PaperIR(
        meta=PaperMeta(title="PaperForge acceptance fixture (synthetic source)", language="en"),
        sections=[draft.to_ir_section()],
    )
    refs = [
        ReferenceMetadata(
            work_key="fixture",
            bibtex_key="fixture2026",
            title=source["title"],
            publication_year=2026,
        )
    ]
    project = build_latex_project(ir, references=refs)
    client = TexdClient(args.texd_url)
    try:
        outcome = client.compile(project.files, entrypoint=project.entrypoint)
    finally:
        client.close()
    assert outcome.ok, outcome.log
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    (output / "sample.pdf").write_bytes(outcome.pdf)
    (output / "sample.md").write_text(render_markdown(ir, references=refs))
    (output / "sample.json").write_text(ir.model_dump_json(indent=2))
    (output / "compile.log").write_text(outcome.log)
    result = {
        "model": draft.model,
        "equations": len(draft.equations),
        "formula_sources_valid": True,
        "pdf_bytes": len(outcome.pdf),
        "formula_issues": draft.generation.get("formula_issues", []),
    }
    (output / "result.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file")
    parser.add_argument("--texd-url", default="http://texd:8081")
    parser.add_argument("--output-dir", default="/tmp/paperforge-scholarly-acceptance")
    asyncio.run(verify(parser.parse_args()))
