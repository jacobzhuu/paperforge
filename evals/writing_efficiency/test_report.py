from copy import deepcopy

import pytest

from evals.writing_efficiency.report import PROTOCOL, analyze
from evals.writing_efficiency.review import valid_review


def fixture():
    rows = [
        {
            "condition": c,
            "repeat": i,
            "fixture_hash": "same",
            "model_config_hash": "same",
            "cache_mode": "cold",
            "protocol": PROTOCOL,
            "writing_seconds": t,
            "tokens": 100,
            "unpriced_calls": 0,
            "hard_violations": ["existing"],
            "complete": True,
        }
        for c, t in [("baseline", 100), ("optimized", 80)]
        for i in range(3)
    ]
    reviews = {
        str(i): {"source": "ai_blind_review", "assessed": True, "passed": True} for i in range(3)
    }
    return rows, reviews


def test_explicit_ai_gate_does_not_claim_human_review():
    rows, reviews = fixture()
    result = analyze(rows, reviews)
    assert result["release_eligible"] and result["human_review_complete"] is False
    assert not analyze(rows, {})["release_eligible"]
    rows[-1]["hard_violations"].append("new")
    assert not analyze(rows, reviews)["release_eligible"]


def test_unpriced_incomplete_or_mismatched_never_passes():
    rows, reviews = fixture()
    for field, value in [("unpriced_calls", 1), ("complete", False), ("tokens", None)]:
        edited = deepcopy(rows)
        edited[-1][field] = value
        assert not analyze(edited, reviews)["release_eligible"]
    rows[-1]["model_config_hash"] = "different"
    with pytest.raises(ValueError):
        analyze(rows, reviews)


def test_ai_review_rejects_unsafe_unknown_and_invalid_scores():
    score = {
        "organization": 4,
        "evidence": 4,
        "consistency": 4,
        "unsafe_skip": False,
        "rationale": "checked",
    }
    value = {"assessed": True, "first": score, "second": dict(score)}
    assert valid_review(value)
    value["second"]["organization"] = float("nan")
    assert not valid_review(value)


@pytest.mark.asyncio
async def test_blind_review_reverses_order_and_excludes_execution_metadata():
    import json
    from types import SimpleNamespace

    from evals.writing_efficiency.review import compare

    seen = []

    class Runner:
        async def agenerate_json(self, *args, **kwargs):
            seen.append(json.loads(kwargs["user_prompt"]))
            score = {
                "organization": 4,
                "evidence": 4,
                "consistency": 4,
                "unsafe_skip": False,
                "rationale": "all sections and bindings checked",
            }
            return SimpleNamespace(
                ok=True, value={"assessed": True, "first": score, "second": score}
            )

    def row(prose):
        return {
            "condition": "SECRET",
            "job_id": "SECRET",
            "review_inputs": [
                {
                    "review_input": {
                        "section_key": key,
                        "question": "question",
                        "prose": prose,
                        "scope": {"job": "SECRET"},
                        "input_hash": "SECRET",
                        "claims": [],
                        "evidence": [],
                        "assets": [],
                        "binding_issues": [],
                    }
                }
                for key in ("body", "conclusion")
            ],
        }

    result = await compare(Runner(), row("Text one"), row("Text two"))
    assert result["passed"] and result["section_count"] == 2
    assert seen[0]["first"] == seen[1]["second"]
    assert seen[0]["second"] == seen[1]["first"]
    assert "SECRET" not in json.dumps(seen)


@pytest.mark.asyncio
async def test_missing_sections_do_not_obtain_ai_approval():
    from evals.writing_efficiency.review import compare

    class Runner:
        async def agenerate_json(self, *args, **kwargs):
            raise AssertionError("missing input must not call model")

    result = await compare(Runner(), {"review_inputs": []}, {"review_inputs": []})
    assert result["assessed"] is False and result["reason"] == "section_coverage"


def test_evidence_batches_cover_every_section_without_truncating_or_repeating_shared_units():
    from evals.writing_efficiency.review import groups, packets

    items = [
        {
            "review_input": {
                "section_key": str(i),
                "question": "Q",
                "prose": "complete prose",
                "claims": [],
                "evidence": [{"evidence_id": str(i), "work_id": "w", "text": str(i) * 60000}],
            }
        }
        for i in range(4)
    ]
    inputs, evidence = packets({"review_inputs": items}, {"review_inputs": items})
    batches = groups(inputs, evidence)
    assert len(batches) > 2
    assert len(batches[0][1][0]) == 4  # complete manuscript in global coherence check
    assert [s["section_key"] for b in batches[1:] for s in b[1][0]] == [str(i) for i in range(4)]
    assert {k: v for b in batches[1:] for k, v in b[2].items()} == evidence
    assert all(len(v["text"]) == 60000 for v in evidence.values())
