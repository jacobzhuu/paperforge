from evals.review_depth.analysis import summarize_runs, wilcoxon_signed_rank


def test_wilcoxon_detects_consistent_positive_pairs() -> None:
    result = wilcoxon_signed_rank([(1, 2), (2, 4), (3, 6), (4, 8), (5, 10)])
    assert result["n"] == 5
    assert result["w_minus"] == 0
    assert result["p_value"] <= 0.0625


def test_run_summary_reports_quality_and_cost_by_condition() -> None:
    report = summarize_runs(
        [
            {
                "condition": "legacy",
                "metrics": {"4_core": 0.2},
                "input_tokens": 100,
                "output_tokens": 20,
                "wall_seconds": 5,
            },
            {
                "condition": "legacy",
                "metrics": {"4_core": 0.4},
                "input_tokens": 200,
                "output_tokens": 40,
                "wall_seconds": 7,
            },
        ]
    )
    assert report["legacy"]["metrics"]["4_core"] == 0.3
    assert report["legacy"]["input_tokens"] == 150
    assert report["legacy"]["wall_seconds"] == 6
