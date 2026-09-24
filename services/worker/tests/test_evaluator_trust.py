"""Trust boundary regressions derived from the anonymous manuscript audit."""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from paperforge_worker.pipelines.review_contract import checked_payload, validate_claim_checks
from paperforge_worker.pipelines.semantic_review import build_section_verdict

from evals.evaluator_trust.manuscript_review import (
    build_manuscript_input,
    validate_manuscript_result,
)


def case():
    material = {
        "claims": [
            {"claim_id": "s5:4:0:4", "resolved_evidence_ids": ["table1"], "source_refs": []}
        ],
        "binding_issues": [],
    }
    payload = {
        "answers_question": "full",
        "support": "sufficient",
        "calibration": "matched",
        "synthesis_mode": "synthesized",
        "diagnosis": "none",
        "gap_declared": True,
        "rationale": "The teacher/student pairing is incorrect.",
        "unsupported_claims": [],
        "claim_checks": [
            {
                "claim_id": "s5:4:0:4",
                "status": "contradicted",
                "evidence_ids": ["table1"],
                "reason": "ResNet32x4 is a teacher, not a student.",
            }
        ],
    }
    return material, payload


def test_claim_error_cannot_hide_behind_overall_matched_or_empty_unsupported_list():
    material, payload = case()
    assert validate_claim_checks(payload, material) is None
    result = build_section_verdict(checked_payload(payload, material), section_key="s5")
    assert not result.acceptable
    assert result.claim_checks[0]["status"] == "contradicted"
    assert "ResNet32x4" in result.unsupported_claims[0]


@pytest.mark.parametrize(
    "mutation,reason",
    [
        (lambda p: p.update(claim_checks=[]), "incomplete_claim_coverage"),
        (
            lambda p: p["claim_checks"][0].update(evidence_ids=["other_claim"]),
            "foreign_claim_evidence",
        ),
        (
            lambda p: p["claim_checks"][0].update(claim_id="foreign"),
            "invalid_or_duplicate_claim_id",
        ),
        (lambda p: p["claim_checks"][0].update(status="uncertain"), "uncertain_claim"),
        (lambda p: p.update(support=[]), "invalid_verdict_fields"),
        (lambda p: p["claim_checks"][0].update(status=[]), "invalid_claim_status"),
        (lambda p: p.update(gap_declared="false"), "invalid_gap_declared"),
    ],
)
def test_partial_or_malformed_output_is_unassessed(mutation, reason):
    material, payload = case()
    mutation(payload)
    assert validate_claim_checks(payload, material) == reason


def test_extra_unanchored_allegation_is_not_silently_dropped():
    material, payload = case()
    payload["unsupported_claims"] = ["Needs third-party replication"]
    with pytest.raises(ValueError, match="unanchored"):
        checked_payload(payload, material)


@pytest.mark.parametrize("status", ["supported", "inference", "gap", "nonfactual"])
def test_grounded_author_report_inference_or_gap_is_not_an_unsupported_claim(status):
    material, payload = case()
    payload["claim_checks"][0]["status"] = status
    assert validate_claim_checks(payload, material) is None
    assert build_section_verdict(checked_payload(payload, material), section_key="s").acceptable


def test_deterministic_binding_problem_cannot_be_overridden_by_model():
    material, payload = case()
    payload["claim_checks"][0]["status"] = "supported"
    material["binding_issues"] = [{"claim_id": "s5:4:0:4", "reason": "unknown_citation"}]
    assert not build_section_verdict(checked_payload(payload, material), section_key="s").acceptable


def manuscript():
    rows = [
        SimpleNamespace(
            section_key=k,
            title=k,
            body_ir_json={
                "blocks": [
                    {
                        "type": "paragraph",
                        "runs": [
                            {"t": "text", "v": t},
                            {"t": "cite", "keys": ["paper"], "evidence_ids": ["e"]},
                        ],
                    }
                ]
            },
        )
        for k, t in [
            ("s5", "CFD 90.64 is below CLIP 97.24."),
            ("conclusion", "CFD exceeds zero-shot CLIP."),
            ("abstract", "A conditional review."),
            ("synthesis", "Compare experimental settings."),
            ("appendix", "The source table contains a missing cell."),
        ]
    ]
    return build_manuscript_input(
        rows,
        {"e": {"id": "e", "text": "CFD 90.64; CLIP 97.24"}, "unrelated": {"id": "unrelated"}},
        snapshot="frozen",
    )


def test_full_document_coverage_includes_frames_synthesis_and_appendix():
    material = manuscript()
    assert len(material["sections"]) == 5
    assert [e["id"] for e in material["evidence"]] == ["e"]
    assert validate_manuscript_result({"reviewed_sections": ["s5"], "findings": []}, material)


def test_offline_cross_section_conflict_requires_verifiable_locations():
    material = manuscript()
    result = {
        "reviewed_sections": [s["section_key"] for s in material["sections"]],
        "findings": [
            {
                "kind": "contradiction",
                "locations": [
                    {"section_key": "s5", "quote": "CFD 90.64 is below CLIP 97.24."},
                    {"section_key": "conclusion", "quote": "CFD exceeds zero-shot CLIP."},
                ],
                "evidence_ids": ["e"],
                "reason": "Reversed comparison direction.",
            }
        ],
    }
    assert validate_manuscript_result(result, material) is None
    wrong = deepcopy(result)
    wrong["findings"][0]["locations"][0]["quote"] = "A quote not in the manuscript"
    assert validate_manuscript_result(wrong, material) == "unverifiable_quote"


def test_table_roles_use_exact_exclusive_headers_not_network_name_heuristics():
    from paperforge_worker.pipelines.review_contract import table_role_conflicts

    material = {
        "evidence": [{"id": "t", "text": "teacher Net-A Net-B\n90 80\nstudent Net-C Net-D\n70 60"}],
        "claims": [
            {
                "claim_id": "c",
                "resolved_evidence_ids": ["t"],
                "text": "以 Net-A 为教师蒸馏 Net-B 学生。",
            }
        ],
    }
    conflicts = table_role_conflicts(material)
    assert len(conflicts) == 1
    assert "Net-B is teacher, not student" in conflicts[0]["reason"]
    material["claims"][0]["text"] = "Net-A 为教师，Net-C 为学生；Net-B 为教师，Net-D 为学生。"
    assert not table_role_conflicts(material)
    material["claims"][0]["text"] = "Net-Bigger 学生提高性能。"
    assert not table_role_conflicts(material)
    material["evidence"][0]["text"] = "teacher Net-A Net-B\nstudent Net-C"
    material["claims"][0]["text"] = "Net-B 学生"
    assert not table_role_conflicts(material), "ambiguous headers must not be guessed"


async def test_invalid_json_retries_once_and_records_raw_outputs():
    from llm_runtime.runner import JsonResult
    from paperforge_worker.pipelines.semantic_review import _judge

    events = []

    class Runner:
        enabled = True
        count = 0

        async def agenerate_json(self, *args, **kwargs):
            self.count += 1
            return JsonResult(value=None, raw_text="broken", error="invalid_json: ValueError")

    async def emit(event, payload):
        events.append((event, payload))

    runner = Runner()
    assert (
        await _judge(
            runner,
            system_prompt="s",
            user_prompt="u",
            metadata={},
            trace_context=SimpleNamespace(emit=emit),
            retry_invalid=True,
        )
        is None
    )
    assert runner.count == 2
    assert [e[1]["raw_text"] for e in events] == ["broken", "broken"]


def test_claim_regression_does_not_confuse_section_synthesis_with_fact_accuracy():
    from evals.evaluator_trust.run import claim_outcome

    material, payload = case()
    payload["claim_checks"][0]["status"] = "supported"
    payload["synthesis_mode"] = "listed"
    verdict = build_section_verdict(checked_payload(payload, material), section_key="s")
    assert not verdict.acceptable, "whole-section acceptance must remain unchanged"
    assert claim_outcome(verdict) == "accept"
    assert claim_outcome(None) == "unassessed"


def test_deterministic_override_retains_model_reason_without_claiming_clearance():
    material, payload = case()
    payload["rationale"] = "Overall this section is acceptable."
    result = checked_payload(payload, material)
    assert result["rationale"].startswith("逐条论断/绑定核查未通过")
    assert "Overall this section is acceptable." in result["rationale"]
    assert not build_section_verdict(result, section_key="s").acceptable
