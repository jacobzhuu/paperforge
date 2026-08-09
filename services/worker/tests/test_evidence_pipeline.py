"""Regression coverage for the evidence-stage failure isolation."""

from __future__ import annotations

import uuid

from paperforge_worker.pipelines.evidence import (
    EvidenceOutcome,
    _json_safe,
    _merge_evidence_outcome,
    _structured_payload,
)


def test_structured_payload_is_json_safe_with_uuid_evidence_ids() -> None:
    evidence_id = uuid.uuid4()
    payload = _json_safe({"main_results": [{"evidence_unit_id": evidence_id}]})
    assert payload["main_results"][0]["evidence_unit_id"] == str(evidence_id)


def test_failed_work_does_not_discard_completed_work_counts() -> None:
    total = EvidenceOutcome(selected_works=2, works_failed=1)
    _merge_evidence_outcome(
        total,
        EvidenceOutcome(works=1, units=3, created=2, reused=1, measurements=1),
    )
    payload = total.to_payload()
    assert payload["works_ok"] == 1
    assert payload["works_failed"] == 1
    assert payload["processing_coverage"] == 0.5
    assert payload["coverage"] == 0.0


def test_experiment_v2_payload_uses_generic_training_dimensions() -> None:
    from db.repositories.tasks import TaskSpec

    tasks = [
        TaskSpec(
            slug="bgc.identification",
            domain="bgc",
            labels={"en": "BGC identification"},
            datasets=("MIBiG",),
            inclusion_cues=("biosynthetic gene cluster", "bgc", "MIBiG"),
        )
    ]
    payload = _structured_payload(
        fulltext=(
            "A BGC identification Transformer used MIBiG 3.1 with a random split. "
            "We trained with AdamW, learning rate 0.001, batch size 32, and 20 epochs."
        ),
        structured_objects=[],
        datasets=[],
        metrics=[],
        records=[],
        tasks=tasks,
    )
    assert payload["schema"] == "experiment_v2"
    assert payload["task_id"] == "bgc.identification"
    assert payload["dataset_version"] == "3.1"
    assert payload["optimizer"] == "AdamW"
    assert payload["learning_rate"] == 0.001
    assert payload["batch_size"] == 32
    assert payload["epochs"] == 20
