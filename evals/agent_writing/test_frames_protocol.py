from copy import deepcopy

import pytest
from paperforge_worker.pipelines.review_inputs import REVIEW_INPUT_VERSION

from evals.agent_writing.frames_protocol import CONDITIONS, CONFIGURATIONS, PROTOCOL, analyze
from evals.agent_writing.test_report import runs


def values():
    result = runs()
    mapping = dict(zip(("legacy", "dag_serial", "dag_parallel"), CONDITIONS, strict=True))
    for row in result:
        row["condition"] = mapping[row["condition"]]
        mode, width = CONFIGURATIONS[row["condition"]]
        row.update(
            protocol=PROTOCOL,
            writer_execution_mode=mode,
            frame_concurrency=width,
            body_concurrency=2,
            evaluator_version=REVIEW_INPUT_VERSION,
        )
    return result


def test_original_gates_and_new_conditions():
    rows = values()
    assert analyze(rows)["release_eligible"]
    for row in rows:
        if row["condition"] == CONDITIONS[2]:
            row["writing_seconds"] = 87
    assert not analyze(rows)["release_eligible"]
    assert analyze(rows)["speed_gate_comparison"] == "B_to_C"
    for row in rows:
        if row["condition"] == CONDITIONS[0]:
            row["writing_seconds"] = 200
    assert not analyze(rows)["release_eligible"], "A/C speed cannot rescue failed B/C gate"


def test_versions_configs_and_human_reviews_are_required():
    for field, bad in (
        ("evaluator_version", "v1"),
        ("frame_concurrency", 2),
        ("protocol", "old"),
        ("body_concurrency", 1),
    ):
        rows = values()
        rows[0][field] = bad
        with pytest.raises(ValueError):
            analyze(rows)
    rows = deepcopy(values())
    rows[0]["expert_verified"] = False
    assert not analyze(rows)["release_eligible"]
