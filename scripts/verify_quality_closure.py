#!/usr/bin/env python3
"""Budgeted real-model writer/reviewer comparison on three explicitly synthetic fixtures.

No production database writes. This is a frozen-material generation/verification smoke test,
not expert evaluation or retrieval/queue/load acceptance. Both arms share one CNY 50 ledger.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

from llm_runtime import LLMRunner
from llm_runtime.experiment_budget import ExperimentBudget
from paperforge_worker.config import WorkerSettings
from paperforge_worker.pipelines.quality import _body_text
from paperforge_worker.pipelines.review_inputs import build_review_input
from paperforge_worker.pipelines.semantic_review import review_section
from paperforge_worker.pipelines.writing import WritingContext, write_section

FIXTURES = [
    (
        "table",
        "Controlled comparison of classification results",
        "This is a synthetic source, not a published study. On benchmark D, fixed test split, "
        "method A has accuracy 90 percent and method B has accuracy 85 percent. "
        "On benchmark E with the same evaluation policy, A has accuracy 70 percent and B "
        "has accuracy 80 percent. Thus the relative ordering changes with the benchmark. "
        "No confidence interval, statistical test, cross-validation, or external validation "
        "was reported. These measurements do not establish a universal ranking.",
    ),
    (
        "mechanism",
        "Evidence boundaries for a plant signaling mechanism",
        "This is a synthetic source, not a published study. In controlled laboratory plants, "
        "the supplied bacterial lipopolysaccharide treatment was associated with expression "
        "of defense-related genes. The experiment did not test volatile compounds, field "
        "conditions, or drought tolerance. Association with gene expression does not establish "
        "a causal signaling pathway. A separate synthetic experiment observed no consistent "
        "expression response under a different growth medium; the studies are not pooled.",
    ),
    (
        "user_data",
        "Descriptive analysis of supplied experimental observations",
        "Synthetic user-provided CSV: group,time_ms\nA,1\nA,3\nB,2\nB,4\n"
        "Deterministic calculation: group A mean 2 ms; group B mean 3 ms; each group n=2; "
        "sample standard deviation sqrt(2) ms in both groups. The observations are from one "
        "laboratory session. No significance test or population inference is supported. "
        "Measurement uncertainty, selection procedure and independent replication are unknown.",
    ),
]


async def run(args):
    if os.environ.get("LLM_PRICE_CURRENCY") != "CNY":
        raise RuntimeError("CNY configured pricing required; no conversion or unknown pricing")
    settings = WorkerSettings()
    config = settings.llm_config()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    budget = ExperimentBudget(Path(args.budget), "calibration")
    records, results = [], []

    def record(call):
        records.append(asdict(call))
        (output / "calls.json").write_text(json.dumps(records, ensure_ascii=False, default=str))

    runner = LLMRunner(config, on_call=record)
    (output / "fixtures.json").write_text(json.dumps(FIXTURES, ensure_ascii=False, indent=2))
    with budget.activate():
        for topic, title, text in FIXTURES:
            started = time.monotonic()
            source = {
                "evidence_id": "fixture-e1",
                "work_id": "fixture-work",
                "cite_key": "fixture2026",
                "title": title,
                "grade": "B_located_prose",
                "page": 1,
                "section_path": "Results",
                "text": text,
            }
            original = topic == "user_data"
            assets = (
                [
                    {
                        "_asset_ref": "ua_fixture",
                        "_asset_kind": "result_table",
                        "type": "table",
                        "filename": "observations.csv",
                        "text": text,
                        "headers": ["group", "mean_ms", "n"],
                        "rows": [["A", "2", "2"], ["B", "3", "2"]],
                        "numeric_cells": {
                            "A::mean_ms": "2",
                            "B::mean_ms": "3",
                            "A::n": "2",
                            "B::n": "2",
                        },
                        "numbers": ["1", "2", "3", "4"],
                    }
                ]
                if original
                else []
            )
            sections = [
                {
                    "key": key,
                    "title": name,
                    "kind": kind,
                    "target_words": target,
                    "question_id": "fixture-q" if kind == "body" else None,
                    "cite_keys": [] if original else ["fixture2026"],
                    "evidence_ids": [] if original else ["fixture-e1"],
                    "summary": (
                        "Use only the supplied synthetic evidence; explain findings and boundaries."
                    ),
                    "argument_points": ["What the evidence supports", "Limits of the inference"],
                }
                for key, name, kind, target in [
                    ("s1", title, "body", 250),
                    ("conclusion", "Conclusion", "frame", 100),
                    ("abstract", "Abstract", "frame", 100),
                ]
            ]
            context = WritingContext(
                language="en",
                paper_type="original" if original else "review",
                outline={
                    "sections": sections,
                    "evidence_policy_version": 2,
                    "sub_question_bundles": [
                        {"question_id": "fixture-q", "evidence": [] if original else [source]}
                    ],
                },
            )
            drafts, inputs = [], []
            for section in sections:
                draft = await write_section(
                    section=section,
                    cards={},
                    whitelist=set() if original else {"fixture2026"},
                    assets=assets,
                    context=context,
                    runner=runner,
                )
                drafts.append(draft)
                context.register(section["key"], draft)
                body = draft.to_ir_section().model_dump(mode="json")
                prose = _body_text(body)
                material = build_review_input(
                    section_key=section["key"],
                    question=section["title"],
                    prose=prose,
                    body=body,
                    evidence=[] if original else [source],
                    assets=assets,
                    whitelist={"fixture2026": "fixture-work"},
                    scope={
                        "project_id": f"fixture-{topic}",
                        "document_id": f"fixture-{topic}",
                        "section_role": section["kind"],
                    },
                )
                inputs.append(
                    {
                        "section_key": section["key"],
                        "question": section["title"],
                        "prose": prose,
                        "evidence": [] if original else [source],
                        "review_input": material,
                    }
                )
            first_draft_seconds = time.monotonic() - started
            (output / f"{topic}-manuscript.json").write_text(
                json.dumps([d.to_ir_section().model_dump(mode="json") for d in drafts], indent=2)
            )
            if args.arm == "baseline":
                judged = []
                for item in inputs:
                    if item["section_key"] != "s1":
                        continue  # Existing foreground path only reviews question-linked sections.
                    v = await review_section(**item, runner=runner, language="en")
                    judged.append(v.to_payload() if v else None)
                review = {
                    "findings": judged,
                    "sections_reviewed": sum(v is not None for v in judged),
                }
            else:
                from paperforge_worker.pipelines.manuscript_review import review_manuscript

                state = {}

                async def emit(name, payload, checkpoint=None, state=state):
                    state.update(checkpoint or {})

                async def stop():
                    return None

                ctx = SimpleNamespace(
                    project_id=f"fixture-{topic}",
                    settings=settings,
                    checkpoint=state,
                    llm_runner=lambda: runner,
                    emit=emit,
                    raise_if_stopped=stop,
                )
                # Acceptance artifacts stay in a temporary local store, never production MinIO.
                ctx.settings = settings.model_copy(
                    update={
                        "storage_backend": "filesystem",
                        "storage_fs_root": str(output / "objects"),
                    }
                )
                review = await review_manuscript(ctx, inputs, language="en")
                # One bounded continuation of the frozen snapshot exercises cache reuse.
                first_review = review
                review = await review_manuscript(ctx, inputs, language="en")
                (output / f"{topic}-initial-review.json").write_text(
                    json.dumps(first_review, indent=2)
                )
            results.append(
                {
                    "topic": topic,
                    "arm": args.arm,
                    "first_draft_seconds": first_draft_seconds,
                    "total_seconds": time.monotonic() - started,
                    "sections": len(drafts),
                    "has_body": [d.has_body for d in drafts],
                    "word_count": sum(d.word_count for d in drafts),
                    "review": review,
                }
            )
            (output / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))
            print(
                json.dumps(
                    {
                        "topic": topic,
                        "arm": args.arm,
                        "seconds": results[-1]["total_seconds"],
                        "budget": budget.report(),
                    }
                ),
                flush=True,
            )
    (output / "budget.json").write_text(json.dumps(budget.report(), indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arm", choices=["baseline", "current"], required=True)
    parser.add_argument("--budget", required=True)
    parser.add_argument("--output", required=True)
    asyncio.run(run(parser.parse_args()))
