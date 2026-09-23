from evals.agent_upgrade.report import evaluate


def test_missing_human_labels_and_uncertain_predictions_are_not_passes():
    cases = [
        {"id": "1", "label": None},
        {"id": "2", "label": "supported", "reviewer": "reviewer", "rationale": "matches source"},
    ]
    result = evaluate(cases, [{"id": "1", "label": "supported"}, {"id": "2", "label": "uncertain"}])
    assert result["accuracy"] is None
    assert result["unassessed"] == 1
    assert result["human_review_complete"] is False
    assert result["release_eligible"] is False
