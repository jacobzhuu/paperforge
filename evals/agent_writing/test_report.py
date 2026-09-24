from copy import deepcopy

import pytest

from evals.agent_writing.report import CONDITIONS, analyze
from evals.agent_writing.run import evaluation_url


def runs():
    return [
        {
            "condition": condition,
            "repeat": repeat,
            "fixture_hash": "fixed",
            "model_config_hash": "fixed",
            "cache_mode": "cold",
            "complete": True,
            "writing_seconds": 70 if condition == "dag_parallel" else 100,
            "tokens": 1000,
            "hard_violations": [],
            "expert_verified": True,
            "expert_scores": {"organization": 4, "evidence": 4, "consistency": 4},
        }
        for condition in CONDITIONS
        for repeat in range(3)
    ]


def test_missing_gold_and_unknown_usage_never_pass_release_gate():
    values = runs()
    assert analyze(values)["release_eligible"]
    values[0]["expert_verified"] = False
    assert not analyze(values)["release_eligible"]
    values = runs()
    values[-1]["tokens"] = None
    assert not analyze(values)["release_eligible"]


def test_unpaired_or_degraded_runs_do_not_pass():
    values = runs()
    values[-1]["fixture_hash"] = "different"
    with pytest.raises(ValueError):
        analyze(values)
    values = runs()
    values[-1]["expert_scores"]["consistency"] = 3
    assert not analyze(values)["release_eligible"]
    values = runs()
    values[-1]["hard_violations"] = ["unsupported_claim"]
    assert not analyze(values)["release_eligible"]
    with pytest.raises(ValueError):
        analyze(values + [deepcopy(values[0])])


def test_live_runner_rejects_default_or_production_database(monkeypatch):
    monkeypatch.delenv("PAPERFORGE_EVAL_DATABASE_URL", raising=False)
    with pytest.raises(ValueError):
        evaluation_url()
    monkeypatch.setenv("PAPERFORGE_EVAL_DATABASE_URL", "postgresql+asyncpg://localhost/paperforge")
    with pytest.raises(ValueError):
        evaluation_url()
