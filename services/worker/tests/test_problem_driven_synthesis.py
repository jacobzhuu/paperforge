from types import SimpleNamespace
from uuid import uuid4

from paperforge_worker.pipelines.qmatrix import (
    MAX_COMPARABILITY_BRIDGE,
    _deterministic_links,
    _diverse_ranked_candidates,
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


# --- 可比性桥接 -------------------------------------------------------------
#
# 生产实测：某项目 618 条证据里 11 条带可比测量、68 条被链接到子问题，两者只重叠
# 1 条（随机期望 1.2）。检索是词面的，结果表那一段几乎全是数字和模型名，中文子问题
# 的词一个都对不上。可比簇因此只能靠巧合形成。


def _measurement(key: str, *, metric: str = "NDCG@10"):
    return SimpleNamespace(
        comparability_key=key, metric_name=metric, dataset="Beauty", split="test"
    )


def _ranked(*units):
    """按名次给一串单元打分，模拟检索输出。"""
    return [(unit, 1.0 - index * 0.01) for index, unit in enumerate(units)]


def test_a_comparable_sibling_is_pulled_into_the_candidate_pool():
    """稀缺的是名额：结果表排在前 24 之外，桥接把比较的两条臂都补回来。"""
    anchor = _unit(text="anchor")
    filler = [_unit(text=f"filler {index}") for index in range(30)]
    table = _unit(text="Table 4 | 0.4471")

    selected = _diverse_ranked_candidates(
        _ranked(anchor, *filler, table),
        measurements={anchor.id: [_measurement("k1")], table.id: [_measurement("k1")]},
    )

    assert any(item[0] is table for item in selected)
    assert any(item[0] is anchor for item in selected)


def test_without_measurements_the_same_table_stays_out():
    """反向对照：桥接不是放大名额，没有共享键就照旧落选。"""
    anchor = _unit(text="anchor")
    filler = [_unit(text=f"filler {index}") for index in range(30)]
    table = _unit(text="Table 4 | 0.4471")

    selected = _diverse_ranked_candidates(_ranked(anchor, *filler, table))

    assert not any(item[0] is table for item in selected)


def test_a_different_comparability_key_does_not_bridge():
    anchor = _unit(text="anchor")
    filler = [_unit(text=f"filler {index}") for index in range(30)]
    other = _unit(text="Table 4 | 0.4471")

    selected = _diverse_ranked_candidates(
        _ranked(anchor, *filler, other),
        measurements={anchor.id: [_measurement("k1")], other.id: [_measurement("k2")]},
    )

    assert not any(item[0] is other for item in selected)


def test_a_salted_key_can_never_bridge():
    """安全性的来源：维度缺一个，键就按证据单元 id 加盐，天生唯一。

    不可比的结果结构上就配不上对，因此桥接不需要额外的白名单。
    """
    anchor = _unit(text="anchor")
    filler = [_unit(text=f"filler {index}") for index in range(30)]
    table = _unit(text="Table 4 | 0.4471")

    selected = _diverse_ranked_candidates(
        _ranked(anchor, *filler, table),
        measurements={
            anchor.id: [_measurement(f"unknown:{anchor.id}")],
            table.id: [_measurement(f"unknown:{table.id}")],
        },
    )

    assert not any(item[0] is table for item in selected)


def test_an_abstract_only_sibling_is_not_bridged():
    """C/D 级不参与跨研究比较，桥接也不该把它们带进来。"""
    anchor = _unit(text="anchor")
    filler = [_unit(text=f"filler {index}") for index in range(30)]
    weak = _unit(text="Table 4 | 0.4471", grade="D_abstract_only")

    selected = _diverse_ranked_candidates(
        _ranked(anchor, *filler, weak),
        measurements={anchor.id: [_measurement("k1")], weak.id: [_measurement("k1")]},
    )

    assert not any(item[0] is weak for item in selected)


def test_a_cluster_inside_one_paper_does_not_bridge():
    """跨研究比较要跨论文；同一篇里的两条结果不构成可比簇。"""
    work = uuid4()
    filler = [_unit(text=f"filler {index}") for index in range(30)]
    a = _unit(text="Table 4 | 0.4471")
    b = _unit(text="Table 7 | 0.5120")
    a.work_id = b.work_id = work

    selected = _diverse_ranked_candidates(
        _ranked(*filler, a, b),
        measurements={a.id: [_measurement("k1")], b.id: [_measurement("k1")]},
    )

    assert not any(item[0] is a or item[0] is b for item in selected)


def test_a_cross_paper_cluster_bridges_even_with_no_selected_anchor():
    """两条臂都通过了词面判定、只是被名额挤掉——补的正是这一对。"""
    filler = [_unit(text=f"filler {index}") for index in range(30)]
    a = _unit(text="Table 4 | 0.4471")
    b = _unit(text="Table 7 | 0.5120")

    selected = _diverse_ranked_candidates(
        _ranked(*filler, a, b),
        measurements={a.id: [_measurement("k1")], b.id: [_measurement("k1")]},
    )

    assert any(item[0] is a for item in selected)
    assert any(item[0] is b for item in selected)


def test_the_bridge_is_bounded_and_respects_the_per_work_cap():
    anchor = _unit(text="anchor")
    filler = [_unit(text=f"filler {index}") for index in range(30)]
    siblings = [_unit(text=f"Table {index}") for index in range(20)]
    measurements = {anchor.id: [_measurement("k1")]}
    for unit in siblings:
        measurements[unit.id] = [_measurement("k1")]

    selected = _diverse_ranked_candidates(
        _ranked(anchor, *filler, *siblings),
        measurements=measurements,
    )

    bridged = [item for item in selected if any(item[0] is unit for unit in siblings)]
    assert len(bridged) <= MAX_COMPARABILITY_BRIDGE
    assert len({id(item[0].work_id) for item in bridged}) == len(bridged)
