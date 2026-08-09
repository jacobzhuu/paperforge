from __future__ import annotations

from paperforge_worker.pipelines.readiness import evaluate_evidence_readiness


def _bundle(index: int, *, ready: bool) -> dict:
    evidence = (
        [
            {
                "evidence_id": f"e{index}-1",
                "work_id": f"w{index}-1",
                "grade": "B_located_prose",
            },
            {
                "evidence_id": f"e{index}-2",
                "work_id": f"w{index}-2",
                "grade": "A_located_structured",
            },
        ]
        if ready
        else []
    )
    return {
        "question_id": f"q{index}",
        "answer_status": "answered" if ready else "insufficient_evidence",
        "evidence": evidence,
    }


def test_narrative_coverage_shortfall_degrades_instead_of_blocking() -> None:
    """一个子问题缺证不该让整篇稿子一个字都产不出来（draft-first）。"""
    report = evaluate_evidence_readiness([_bundle(index, ready=index == 0) for index in range(5)])
    assert report.ready
    assert report.degraded
    assert report.readiness_status == "evidence_degraded"
    assert report.coverage == 0.2
    assert not report.blockers
    assert {item["code"] for item in report.degradations} == {"question_evidence_coverage_low"}
    assert {item["severity"] for item in report.degradations} == {"degradable"}


def test_systematic_coverage_shortfall_still_blocks() -> None:
    """严格模式是用户显式要的：覆盖率缺口在系统综述里就是选择偏差，不能降级。"""
    bundles = [_bundle(index, ready=index == 0) for index in range(5)]
    systematic = evaluate_evidence_readiness(bundles, review_style="systematic")
    submission = evaluate_evidence_readiness(bundles, quality_profile="submission")
    assert not systematic.ready
    assert not submission.ready
    assert "question_evidence_coverage_low" in {item["code"] for item in systematic.blockers}


def test_narrative_gate_allows_one_explicit_gap_but_systematic_does_not() -> None:
    bundles = [_bundle(index, ready=index < 4) for index in range(5)]
    narrative = evaluate_evidence_readiness(bundles)
    systematic = evaluate_evidence_readiness(bundles, review_style="systematic")
    assert narrative.ready
    assert not narrative.degraded
    assert narrative.coverage == 0.8
    assert not systematic.ready


def test_single_source_corpus_blocks_in_every_mode() -> None:
    """「至少两个独立来源」是防单源综述的唯一防线，不参与降级。"""
    bundle = _bundle(0, ready=True)
    bundle["evidence"] = [
        {"evidence_id": "e0-1", "work_id": "w0-1", "grade": "B_located_prose"},
        {"evidence_id": "e0-2", "work_id": "w0-1", "grade": "A_located_structured"},
    ]
    report = evaluate_evidence_readiness([bundle])
    assert not report.ready
    assert "evidence_source_diversity_low" in {item["code"] for item in report.blockers}


def test_unready_questions_are_reported_for_gap_sections() -> None:
    report = evaluate_evidence_readiness([_bundle(index, ready=index == 0) for index in range(3)])
    assert report.unready_question_ids == ["q1", "q2"]


def test_classifier_majority_rejection_does_not_erase_sufficient_retained_matrix() -> None:
    bundles = [_bundle(index, ready=True) for index in range(5)]
    report = evaluate_evidence_readiness(
        bundles,
        matrix_payload={
            "diagnostics": [
                {"no_link_reason": "classifier_rejected_all"},
                {"no_link_reason": "classifier_rejected_all"},
                {"no_link_reason": "classifier_rejected_all"},
                {"no_link_reason": None},
                {"no_link_reason": None},
            ]
        },
    )
    assert report.ready
    assert "evidence_classifier_rejected_majority" in {item["code"] for item in report.warnings}


def test_classifier_majority_rejection_never_blocks_on_its_own() -> None:
    """分类器整批否决是提示/桥接问题的信号，不是「语料回答不了问题」的证据。"""
    report = evaluate_evidence_readiness(
        [_bundle(index, ready=index < 2) for index in range(5)],
        matrix_payload={
            "diagnostics": [
                {"no_link_reason": "classifier_rejected_all"},
                {"no_link_reason": "classifier_rejected_all"},
                {"no_link_reason": "classifier_rejected_all"},
                {"no_link_reason": None},
                {"no_link_reason": None},
            ]
        },
    )
    assert report.ready
    assert "evidence_classifier_rejected_majority" in {item["code"] for item in report.degradations}


def test_all_partial_questions_degrade_narrative_writing() -> None:
    bundles = [_bundle(index, ready=True) for index in range(5)]
    for bundle in bundles:
        bundle["answer_status"] = "partial"
    report = evaluate_evidence_readiness(bundles)
    assert report.ready
    assert report.degraded
    assert report.coverage == 1.0
    assert "no_fully_synthesized_question" in {item["code"] for item in report.degradations}


def test_systematic_requires_strong_not_partial_answers() -> None:
    bundles = [_bundle(index, ready=True) for index in range(5)]
    bundles[-1]["answer_status"] = "partial"
    report = evaluate_evidence_readiness(bundles, review_style="systematic")
    assert not report.ready
    assert report.coverage == 0.8
