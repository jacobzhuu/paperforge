from types import SimpleNamespace
from uuid import uuid4

from paperforge_worker.pipelines.qmatrix import (
    _deterministic_links,
    _link_set_score,
    _rank_candidates,
    _terms,
)
from paperforge_worker.pipelines.synthesis import _synthesize_bundle


def _unit(*, text: str, grade: str = "B_located_prose", kind: str = "experimental_fact"):
    return SimpleNamespace(
        id=uuid4(),
        work_id=uuid4(),
        text=text,
        section_path="Results",
        grade=grade,
        kind=kind,
        page=3,
        paragraph_index=1,
        object_ref=None,
    )


def test_qmatrix_candidate_retrieval_prefers_relevant_located_evidence():
    relevant = _unit(text="Results on MovieLens show the poisoning attack reduces HR@20.")
    abstract = _unit(
        text="A generic recommendation background statement.",
        grade="D_abstract_only",
        kind="review_restatement",
    )
    ranked = _rank_candidates(
        "How does poisoning affect recommendation metrics on MovieLens?",
        [abstract, relevant],
        expected_kinds={"experimental_fact"},
    )
    assert ranked[0][0] is relevant
    assert ranked[0][1] > ranked[-1][1]


def test_qmatrix_fallback_never_promotes_abstract_hit_to_support():
    abstract = _unit(
        text="The abstract reports an improvement on a nearby task.",
        grade="D_abstract_only",
    )
    assert _deterministic_links([(abstract, 0.95)], measurements={}) == []


def test_qmatrix_does_not_admit_an_unrelated_numeric_fact_by_kind_alone():
    unrelated = _unit(text="An RNA secondary-structure model reaches AUROC 0.99.")
    assert not _rank_candidates(
        "生物合成基因簇识别的性能优势是什么？",
        [unrelated],
        expected_kinds={"experimental_fact"},
    )


def test_qmatrix_chinese_terms_use_bigrams_not_one_whole_clause():
    terms = _terms("生物合成基因簇识别")
    assert "生物" in terms
    assert "识别" in terms
    assert "生物合成基因簇识别" not in terms


def test_qmatrix_rejects_evidence_assigned_to_a_different_task():
    unit = _unit(text="BGC classification reaches F1 0.9.")
    unit.task_id = "bgc.classification"
    assert not _rank_candidates(
        "How accurate is BGC identification?",
        [unit],
        expected_kinds={"experimental_fact"},
        task_id="bgc.identification",
    )


def test_qmatrix_monotonic_score_prefers_multi_work_fulltext_evidence() -> None:
    first = _unit(text="Located result A")
    second = _unit(text="Located result B")
    abstract = _unit(text="Abstract only", grade="D_abstract_only")
    evidence = {unit.id: unit for unit in (first, second, abstract)}
    assert _link_set_score([first.id, second.id], evidence) > _link_set_score(
        [abstract.id], evidence
    )
    assert _link_set_score([first.id, second.id], evidence) > _link_set_score([], evidence)


def test_synthesis_detects_conflict_only_within_same_comparability_key():
    first = _unit(text="F1 improves to 91.3.")
    second = _unit(text="F1 decreases to 87.0.")
    first.work_id = uuid4()
    second.work_id = uuid4()
    links = [
        (SimpleNamespace(stance="supports", condition_note=None, confidence=0.9), first),
        (SimpleNamespace(stance="contradicts", condition_note=None, confidence=0.8), second),
    ]
    measurement = lambda unit_id, key, value: SimpleNamespace(  # noqa: E731
        evidence_unit_id=unit_id,
        metric_name="F1",
        value=value,
        unit="%",
        dataset="TestSet",
        task="classification",
        sample_size=100,
        split="test",
        comparability_key=key,
    )
    context = {
        first.work_id: {"cite_key": "a", "title": "A", "year": 2024},
        second.work_id: {"cite_key": "b", "title": "B", "year": 2025},
    }
    question = SimpleNamespace(
        id=uuid4(),
        text="Does the method improve F1?",
        order_index=1,
        comparison_dimensions_json=["dataset", "metric"],
        expected_evidence_kinds_json=["experimental_fact"],
    )
    bundle = _synthesize_bundle(
        question,
        linked=links,
        measurements={
            first.id: [measurement(first.id, "same", 91.3)],
            second.id: [measurement(second.id, "same", 87.0)],
        },
        work_context=context,
    )
    assert bundle["answer_status"] == "contested"
    assert bundle["stance_summary"] == "conflicting"
    assert bundle["comparison_clusters"][0]["classification"] == "conflicting"

    incomparable = _synthesize_bundle(
        question,
        linked=links,
        measurements={
            first.id: [measurement(first.id, "dataset-a", 91.3)],
            second.id: [measurement(second.id, "dataset-b", 87.0)],
        },
        work_context=context,
    )
    assert incomparable["answer_status"] == "partial"
    assert incomparable["comparison_clusters"] == []
    assert len(incomparable["not_comparable_groups"]) == 2
