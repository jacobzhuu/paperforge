from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from llm_runtime.config import ModelPrice
from paperforge_worker.config import WorkerSettings
from paperforge_worker.pipelines.citation_decisions import (
    citation_pairs,
    decision_request,
    semantic_sources,
)

from evals.jev_shadow.freeze import digest, save
from evals.jev_shadow.report import audit_event_chain, audit_http, threshold_metrics, wilson_upper
from evals.jev_shadow.run import Budget, BudgetStop, adjudicate, load_labels, lock_labels


def test_production_price_configuration_decodes_json():
    config = WorkerSettings(
        _env_file=None, typesafe_model_prices='{"jev-1.13.0":{"input":0.042,"output":0}}'
    )
    assert config.typesafe_decision_config()["model_prices"]["jev-1.13.0"] == ModelPrice(0.042, 0)
    broken = WorkerSettings(_env_file=None, typesafe_model_prices="invalid")
    assert broken.typesafe_decision_config()["model_prices"] == {}


def test_source_precedence_and_exact_pair_truncation():
    entries = [
        (SimpleNamespace(bibtex_key="a"), SimpleNamespace(id="w", abstract="fallback")),
        (SimpleNamespace(bibtex_key="b"), SimpleNamespace(id="x", abstract="abstract")),
    ]
    sources = semantic_sources(entries, [{"work_id": "w", "text": "e" * 500}])
    pairs = citation_pairs([{"cite_key": "a", "context_snippet": "c" * 350}], sources)
    assert sources["b"] == "abstract"
    assert len(pairs[0]["context"]) == 300 and len(pairs[0]["evidence"]) == 400
    state, questions = decision_request(pairs * 2)
    assert "pairs[1].context" in questions["item_1"]["instructions"]
    assert len(state["pairs"]) == 2


def test_empty_and_small_selected_sets_cannot_qualify():
    assert wilson_upper(0, 5) > 0.05
    assert not threshold_metrics([], 0.9)["eligible"]
    rows = [
        {"grade": 4, "score": 4, "confidence": 1.0, "ambiguous": False, "baseline_score": 1.0}
        for _ in range(5)
    ]
    assert not threshold_metrics(rows, 0.9)["eligible"]


def test_partial_is_not_weak_and_ambiguous_is_not_scored_as_gold():
    rows = [
        {"grade": 2, "score": 2, "confidence": 0.9, "ambiguous": False, "baseline_score": 0.5},
        {"grade": 0, "score": 4, "confidence": 1, "ambiguous": True, "baseline_score": 0},
    ]
    m = threshold_metrics(rows, 0.5)
    assert m["accuracy"] == 1
    assert m["accepted_certain"] == 1
    assert m["ambiguous_acceptance_rate"] == 1
    assert not m["eligible"]


def test_budget_persists_reservations_and_never_records_credentials(tmp_path):
    budget = Budget(tmp_path, max_calls=1)
    body = {"model": "test", "max_tokens": 10}
    budget.reserve(body, ModelPrice(0.4, 1.4), "llm")
    with pytest.raises(BudgetStop):
        Budget(tmp_path, max_calls=1).reserve(body, ModelPrice(0.4, 1.4), "llm")
    record = json.loads(next(tmp_path.glob("*.reservation.json")).read_text())
    assert record["reserved_usd"] > 0
    with pytest.raises(BudgetStop):
        budget.reserve(body, None, "llm")


def test_blind_adjudication_and_lock_are_immutable(tmp_path):
    pair = {"sample_id": "p00-00", "pair_hash": "hash"}
    groups = [{"pairs": [pair]}]
    corpus_hash = digest(groups)
    save(tmp_path / "corpus.json", {"groups": groups, "corpus_hash": corpus_hash})
    save(
        tmp_path / "labels.draft.json",
        {
            "corpus_hash": corpus_hash,
            "items": [{**pair, "grade": 2, "ambiguous": True, "reason": "uncertain"}],
        },
    )
    review = tmp_path / "review.json"
    save(review, {"method": "blind", "items": []})
    with pytest.raises(ValueError, match="uncertain"):
        adjudicate(tmp_path, review)
    corrected = tmp_path / "corrected.json"
    save(
        corrected,
        {
            "method": "blind",
            "items": [
                {"sample_id": "p00-00", "grade": 1, "ambiguous": False, "reason": "not supported"},
            ],
        },
    )
    assert adjudicate(tmp_path, corrected)["codex_reviewed"] == 1
    lock_labels(tmp_path, tmp_path / "labels.reviewed.json")
    label = load_labels(tmp_path)["items"][0]
    assert label["grade"] == 1 and label["draft_label"]["grade"] == 2
    with pytest.raises(ValueError, match="precede"):
        adjudicate(tmp_path, corrected)
    with pytest.raises(FileExistsError):
        lock_labels(tmp_path, tmp_path / "labels.reviewed.json")


def test_raw_zero_confidence_survives_parser_and_event_audit(tmp_path):
    pairs = [{"context": "context", "evidence": "excerpt"}]
    state, questions = decision_request(pairs)
    answer = {
        "type": "score",
        "score": 2.0,
        "confidence": 0.0,
        "probabilities": {str(i): 0.2 for i in range(5)},
        "legend": {str(i): v for i, v in enumerate(questions["item_0"]["criteria"])},
    }
    save(
        tmp_path / "http/0000.reservation.json",
        {
            "kind": "jev",
            "reserved_usd": 0.001,
            "request": {"state": state, "questions": questions},
        },
    )
    save(
        tmp_path / "http/0000.response.json",
        {
            "status": 200,
            "body": json.dumps({"answers": {"item_0": answer}}),
        },
    )
    save(
        tmp_path / "dev.group-00.json",
        {
            "events": [{"payload": {"items": [{"index": 0, **answer}]}}],
        },
    )
    assert audit_http(tmp_path)["raw_parsed_mismatches"] == 0
    corpus = {"groups": [{"split": "dev", "pairs": pairs}]}
    audit = audit_event_chain(tmp_path, corpus, "dev")
    assert audit["compared_answers"] == 1 and audit["mismatches"] == 0
