"""Behavioral counterexamples for source-bound repairs and incremental verification."""

import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from paperforge_worker.pipelines.claim_repair import replace_claim
from paperforge_worker.pipelines.claim_review import batches
from paperforge_worker.pipelines.field_provenance import validate_fields
from paperforge_worker.pipelines.manuscript_review import summary_material
from paperforge_worker.pipelines.review_contract import validate_claim_checks
from paperforge_worker.pipelines.semantic_review import review_section


def material(n=10):
    return {
        "section_key": "s",
        "body_hash": "snapshot",
        "scope": {"project_id": "p"},
        "claims": [
            {
                "claim_id": f"c{i}",
                "text": f"Finding {i}",
                "resolved_evidence_ids": [f"e{i}"],
                "source_refs": [],
            }
            for i in range(n)
        ],
        "evidence": [{"evidence_id": f"e{i}", "text": f"Source finding {i}"} for i in range(n)],
    }


def verdict(claims):
    return {
        "answers_question": "full",
        "support": "sufficient",
        "calibration": "matched",
        "synthesis_mode": "synthesized",
        "diagnosis": "none",
        "gap_declared": False,
        "unsupported_claims": [],
        "claim_checks": [
            {
                "claim_id": c["claim_id"],
                "evidence_ids": c["resolved_evidence_ids"],
                "status": "supported",
                "reason": "Exact source match",
            }
            for c in claims
        ],
    }


async def test_one_uncertain_claim_preserves_other_results_without_retry(monkeypatch):
    from paperforge_worker.pipelines import semantic_review as sr

    m = material(3)
    p = verdict(m["claims"])
    p["claim_checks"][1]["status"] = "uncertain"
    judge = AsyncMock(return_value=p)
    monkeypatch.setattr(sr, "_judge", judge)
    v = await review_section(
        section_key="s", question="Q", prose="P", evidence=[], runner=None, review_input=m
    )
    assert not v.acceptable
    assert [c["status"] for c in v.claim_checks] == ["supported", "uncertain", "supported"]
    assert judge.await_count == 1


async def test_missing_claim_only_is_retried(monkeypatch):
    from paperforge_worker.pipelines import semantic_review as sr

    m = material(3)
    first = verdict(m["claims"][:2])
    judge = AsyncMock(side_effect=[first, verdict(m["claims"][2:])])
    monkeypatch.setattr(sr, "_judge", judge)
    v = await review_section(
        section_key="s", question="Q", prose="P", evidence=[], runner=None, review_input=m
    )
    assert v.acceptable and len(v.claim_checks) == 3
    retry = json.loads(judge.call_args.kwargs["user_prompt"].split("\n", 1)[1])
    assert [c["claim_id"] for c in retry["claims"]] == ["c2"]


async def test_large_single_claim_does_not_hide_other_claims(monkeypatch):
    from paperforge_worker.pipelines import semantic_review as sr

    m = material(2)
    m["evidence"][0]["text"] = "x" * 50000
    judge = AsyncMock(return_value=verdict(m["claims"][1:]))
    monkeypatch.setattr(sr, "_judge", judge)
    v = await review_section(
        section_key="s", question="Q", prose="P", evidence=[], runner=None, review_input=m
    )
    assert [c["status"] for c in v.claim_checks] == ["unassessed", "supported"]
    assert not v.acceptable and judge.await_count == 1


async def test_completed_batches_resume_without_model_calls(tmp_path, monkeypatch):
    from paperforge_worker.config import WorkerSettings
    from paperforge_worker.pipelines import semantic_review as sr

    m = material(10)

    async def judge(*args, **kw):
        data = json.loads(kw["user_prompt"].split("\n", 1)[1])
        return verdict(data["claims"])

    spy = AsyncMock(side_effect=judge)
    monkeypatch.setattr(sr, "_judge", spy)
    ctx = SimpleNamespace(
        project_id="p", checkpoint={}, settings=WorkerSettings(), raise_if_stopped=AsyncMock()
    )

    async def emit(name, payload, checkpoint=None):
        ctx.checkpoint.update(checkpoint or {})

    ctx.emit = emit
    args = dict(
        section_key="s",
        question="Q",
        prose="P",
        evidence=[],
        runner=None,
        review_input=m,
        trace_context=ctx,
    )
    assert (await review_section(**args)).acceptable
    assert spy.await_count == 2
    assert (await review_section(**args)).acceptable
    assert spy.await_count == 2
    changed = copy.deepcopy(m)
    changed["evidence"][9]["text"] += " changed"
    assert (await review_section(**{**args, "review_input": changed})).acceptable
    assert spy.await_count == 3


def test_summary_requires_verified_body_binding_and_invalidates_on_change():
    m = material(1)
    prepared = summary_material(m, m["claims"], "body-v1")
    p = verdict(prepared["claims"])
    assert validate_claim_checks(p, prepared) == "missing_or_invalid_body_claim_binding"
    p["claim_checks"][0]["body_claim_ids"] = ["c0"]
    assert validate_claim_checks(p, prepared) is None
    assert prepared != summary_material(m, [], "body-v2")


def test_sentence_patch_preserves_unrelated_text_and_all_bindings():
    body = {
        "blocks": [
            {
                "type": "paragraph",
                "runs": [
                    {"t": "text", "v": "Keep this. Wrong direction."},
                    {"t": "cite", "keys": ["a"], "evidence_ids": ["e"]},
                ],
            }
        ]
    }
    assert replace_claim(
        body, {"claim_id": "s:0:0:1", "text": "Wrong direction."}, "Correct direction."
    )
    assert body["blocks"][0]["runs"] == [
        {"t": "text", "v": "Keep this. Correct direction."},
        {"t": "cite", "keys": ["a"], "evidence_ids": ["e"]},
    ]


def test_real_number_wrong_region_and_wrong_metric_are_not_verified():
    from paperforge_worker.pipelines.evidence import _parse_locator
    from paperforge_worker.pipelines.experiment_extraction import LocatorIndex

    source = "[[PAGE=1]] Learning rate is 0.01. [[PAGE=2]] Accuracy is 0.9."
    for value, metric, marker, quote in [
        (0.9, "accuracy", "[[PAGE=1]]", "Accuracy is 0.9."),
        (0.01, "accuracy", "[[PAGE=1]]", "Learning rate is 0.01."),
    ]:
        proof = validate_fields(
            {
                "value": value,
                "metric_name": metric,
                "source_marker": marker,
                "verbatim_span": quote,
            },
            fulltext=source,
            locators=LocatorIndex(),
            parse_locator=_parse_locator,
        )
        assert proof["fields"]["value"]["status"] == "unknown"


def test_table_cell_must_match_actual_header_and_model_row():
    from paperforge_worker.pipelines.evidence import _parse_locator
    from paperforge_worker.pipelines.experiment_extraction import LocatorIndex

    quote = "| Model | Accuracy | F1 |\n| A | 90 | 80 |\n| B | 85 | 70 |"
    item = {
        "metric_name": "accuracy",
        "model_family": "A",
        "value": 90,
        "verbatim_span": quote,
        "source_marker": "[[TABLE=1]]",
        "field_sources": {"value": {"row": 1, "column": 1}},
    }

    def check(x):
        return validate_fields(
            x,
            fulltext="[[TABLE=1]] " + quote,
            locators=LocatorIndex(),
            parse_locator=_parse_locator,
        )["fields"]["value"]["status"]

    assert check(item) == "verified"
    assert check({**item, "model_family": "B"}) == "unknown"
    assert check({**item, "metric_name": "f1"}) == "unknown"


def test_batches_have_only_their_own_evidence():
    pieces = batches(material(20))
    assert [len(p["claims"]) for p in pieces] == [8, 8, 4]
    assert {e["evidence_id"] for e in pieces[0]["evidence"]} == {f"e{i}" for i in range(8)}


async def test_partial_batch_resume_reuses_completed_claims(tmp_path, monkeypatch):
    from paperforge_worker.config import WorkerSettings
    from paperforge_worker.pipelines import semantic_review as sr

    m = material(2)
    partial = verdict(m["claims"][:1])
    spy = AsyncMock(side_effect=[partial, None, verdict(m["claims"][1:])])
    monkeypatch.setattr(sr, "_judge", spy)
    ctx = SimpleNamespace(
        project_id="p", checkpoint={}, settings=WorkerSettings(), raise_if_stopped=AsyncMock()
    )

    async def emit(name, payload, checkpoint=None):
        ctx.checkpoint.update(checkpoint or {})

    ctx.emit = emit
    args = dict(
        section_key="s",
        question="Q",
        prose="P",
        evidence=[],
        runner=None,
        review_input=m,
        trace_context=ctx,
    )
    first = await review_section(**args)
    assert [c["status"] for c in first.claim_checks] == ["supported", "unassessed"]
    second = await review_section(**args)
    assert second.acceptable
    retried = json.loads(spy.call_args.kwargs["user_prompt"].split("\n", 1)[1])
    assert [c["claim_id"] for c in retried["claims"]] == ["c1"]


def test_comparison_arithmetic_does_not_infer_significance_or_unknown_direction():
    from paperforge_worker.pipelines.field_provenance import (
        REQUIRED_COMPARISON,
        derived_comparisons,
    )

    proof = {
        "version": "experiment-v4-provenance",
        "fields": {f: {"status": "verified"} for f in REQUIRED_COMPARISON},
    }
    units = [
        {
            "evidence_id": k,
            "measurements": [
                {
                    "provenance_json": proof,
                    "comparability_key": "fixed",
                    "metric_name": "accuracy",
                    "dataset": "D",
                    "split": "test",
                    "unit": "%",
                    "value": v,
                    "model_family": k,
                }
            ],
        }
        for k, v in [("A", 90.1), ("B", 85.2)]
    ]
    comparison = derived_comparisons(units)[0]
    assert comparison["difference_left_minus_right"] == "4.9"
    assert comparison["better_model"] == "A"
    assert comparison["significance"] == "not_assessed"
    for unit in units:
        unit["measurements"][0]["metric_name"] = "unknown_metric"
    assert derived_comparisons(units)[0]["better_model"] is None
    units[1]["measurements"][0]["split"] = "validation"
    assert derived_comparisons(units) == []


def test_unrelated_sentence_does_not_qualify_result_dataset():
    from paperforge_worker.pipelines.field_provenance import validate_fields

    quote = "Dataset X was used for training. Accuracy 90 percent was measured on dataset Y."
    item = {
        "metric_name": "accuracy",
        "value": 90,
        "dataset": "X",
        "source_marker": "PAGE=1",
        "verbatim_span": quote,
    }
    result = validate_fields(
        item,
        fulltext="[[PAGE=1]]" + quote,
        locators=SimpleNamespace(regions=[]),
        parse_locator=lambda m: {"page": int(m.split("=")[1])},
    )
    assert result["fields"]["dataset"]["status"] == "unknown"


async def test_edit_to_one_batch_reuses_other_batch(monkeypatch):
    from paperforge_worker.pipelines import semantic_review as sr

    m = material(12)
    state = {}

    async def emit(_name, _payload, checkpoint=None):
        state.update(checkpoint or {})

    from paperforge_worker.config import WorkerSettings

    ctx = SimpleNamespace(
        project_id="p",
        settings=WorkerSettings(),
        checkpoint=state,
        emit=emit,
        raise_if_stopped=AsyncMock(),
    )

    async def judge(*args, **kwargs):
        batch = json.loads(kwargs["user_prompt"].split("\n", 1)[1])
        return verdict(batch["claims"])

    call = AsyncMock(side_effect=judge)
    monkeypatch.setattr(sr, "_judge", call)

    async def review():
        return await review_section(
            section_key="s",
            question="Q",
            prose="P",
            evidence=[],
            runner=None,
            review_input=m,
            trace_context=ctx,
        )

    assert (await review()).acceptable
    assert call.await_count == 2
    m["body_hash"] = "new-body"
    m["claims"][0]["text"] = "Corrected finding"
    assert (await review()).acceptable
    assert call.await_count == 3
