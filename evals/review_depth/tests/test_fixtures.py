import json
from pathlib import Path

from evals.review_depth.validate_fixtures import validate_g1

FIXTURES = Path(__file__).parents[1] / "fixtures"


def test_g1_reserves_twenty_slots_without_pretending_they_are_gold() -> None:
    rows = json.loads((FIXTURES / "g1_annotation_slots.json").read_text())
    assert len(rows) == 20
    assert validate_g1(rows, allow_incomplete=True) == []
    assert len(validate_g1(rows)) == 20


def test_g3_has_three_conditions_for_each_of_five_topics() -> None:
    rows = json.loads((FIXTURES / "g3_ablation_manifest.json").read_text())
    by_topic: dict[str, set[str]] = {}
    for row in rows:
        by_topic.setdefault(row["topic_id"], set()).add(row["condition"])
    assert len(by_topic) == 5
    assert all(
        conditions == {"legacy", "fulltext_only", "problem_driven"}
        for conditions in by_topic.values()
    )
