"""Phase 4 综合的准入规则。

模型在这里只多出一段叙述，不多出任何权力。每条规则都是硬过滤：不合规就丢，
不做"宽容修补"——修补出来的条目看上去有引用、其实没有出处，比丢掉危险得多。
"""

from __future__ import annotations

from paperforge_worker.pipelines.synthesis_llm import (
    CORE_DIMENSIONS,
    MAX_ENTRIES_PER_KIND,
    build_synthesis,
    bundle_fingerprint,
    render_bundle,
    should_synthesize,
)

WORK_A = "11111111-1111-1111-1111-111111111111"
WORK_B = "22222222-2222-2222-2222-222222222222"
E1 = "aaaaaaaa-0000-0000-0000-000000000001"
E2 = "aaaaaaaa-0000-0000-0000-000000000002"
E3 = "aaaaaaaa-0000-0000-0000-000000000003"
KEY = "task=bgc|dataset=mibig|split=random"


def _unit(
    evidence_id: str,
    *,
    work_id: str = WORK_A,
    grade: str = "B_located_prose",
    text: str = "The method reaches AUROC 0.912 on the held-out split.",
    measurements: list[dict] | None = None,
):
    return {
        "evidence_id": evidence_id,
        "work_id": work_id,
        "cite_key": f"cite{evidence_id[-1]}",
        "grade": grade,
        "kind": "experimental_fact",
        "text": text,
        "page": 4,
        "section_path": "Results",
        "paragraph_index": 2,
        "object_ref": None,
        "stance": "supports",
        "condition_note": None,
        "confidence": 0.8,
        "measurements": measurements or [],
    }


def _bundle(*, evidence=None, clusters=None, answer_status="answered"):
    return {
        "question_id": "99999999-9999-9999-9999-999999999999",
        "question": "Does the method transfer across datasets?",
        "order_index": 0,
        "comparison_dimensions": ["dataset"],
        "expected_evidence_kinds": ["experimental_fact"],
        "answer_status": answer_status,
        "stance_summary": "consistent",
        "evidence": evidence if evidence is not None else [_unit(E1), _unit(E2, work_id=WORK_B)],
        "comparison_clusters": clusters
        if clusters is not None
        else [
            {
                "comparability_key": KEY,
                "classification": "conflicting",
                "evidence_ids": [E1, E2],
                "cite_keys": ["cite1", "cite2"],
                "conditions": [],
            }
        ],
        "not_comparable_groups": [],
        "evidence_gap": None,
    }


def _build(payload, *, bundle=None, dimensions=frozenset(CORE_DIMENSIONS)):
    return build_synthesis(
        payload=payload,
        bundle=bundle or _bundle(),
        allowed_dimensions=dimensions,
        model="test-model",
    )


# --- 规则 1：引用必须落在本 bundle 内 ------------------------------------------


def test_rule1_drops_an_evidence_id_from_another_question():
    """跨问题污染是最隐蔽的一种：条目看着有绑定，绑的却是别处的证据。"""
    result = _build(
        {
            "agreement": [
                {"statement": "Both studies report gains.", "evidence_ids": [E1, "not-in-bundle"]}
            ]
        }
    )

    assert [entry.evidence_ids for entry in result.agreement] == [(E1,)]


def test_rule1_drops_the_whole_entry_when_no_id_survives():
    result = _build(
        {"agreement": [{"statement": "Everything agrees.", "evidence_ids": ["ghost-id"]}]}
    )

    assert result.agreement == ()
    assert [item["reason"] for item in result.rejected] == ["no_bundle_evidence"]


def test_rule1_an_entirely_foreign_response_yields_an_empty_synthesis():
    """对抗用例：不能因为"至少留下点什么"就放行一条无出处的综合。"""
    result = _build(
        {
            "claim": "The approach generalises.",
            "agreement": [{"statement": "A agrees with B.", "evidence_ids": [E3]}],
            "conflict": [
                {"statement": "A contradicts B.", "evidence_ids": [E3], "comparability_key": KEY}
            ],
        }
    )

    assert result.agreement == ()
    assert result.conflict == ()
    assert result.claim == "The approach generalises."  # 无数字的断言本身不受规则 1 约束


# --- 规则 2：冲突必须落在同一可比簇内且跨两项研究 --------------------------------


def test_rule2_accepts_a_conflict_inside_one_cluster_across_two_works():
    result = _build(
        {
            "conflict": [
                {
                    "statement": "The two studies disagree on the direction of the effect.",
                    "evidence_ids": [E1, E2],
                    "comparability_key": KEY,
                }
            ]
        }
    )

    assert len(result.conflict) == 1
    assert result.conflict[0].comparability_key == KEY


def test_rule2_rejects_a_conflict_spanning_different_comparability_keys():
    """对抗用例：不可比的两项研究之间不存在"冲突"这回事。"""
    bundle = _bundle(
        evidence=[_unit(E1), _unit(E2, work_id=WORK_B)],
        clusters=[
            {"comparability_key": "key-a", "classification": "consistent", "evidence_ids": [E1]},
            {"comparability_key": "key-b", "classification": "consistent", "evidence_ids": [E2]},
        ],
    )
    result = _build(
        {
            "conflict": [
                {
                    "statement": "These results contradict each other.",
                    "evidence_ids": [E1, E2],
                    "comparability_key": "key-a",
                }
            ]
        },
        bundle=bundle,
    )

    assert result.conflict == ()
    assert [item["reason"] for item in result.rejected] == ["evidence_outside_cluster"]


def test_rule2_rejects_a_conflict_with_a_comparability_key_that_is_not_a_cluster():
    result = _build(
        {
            "conflict": [
                {
                    "statement": "They disagree.",
                    "evidence_ids": [E1, E2],
                    "comparability_key": "invented-key",
                }
            ]
        }
    )

    assert result.conflict == ()
    assert [item["reason"] for item in result.rejected] == ["unknown_comparability_key"]


def test_rule2_rejects_a_conflict_within_a_single_work():
    """同一篇论文里的两条证据不是"研究之间的冲突"。"""
    bundle = _bundle(
        evidence=[_unit(E1), _unit(E2)],  # 两条都属于 WORK_A
        clusters=[
            {"comparability_key": KEY, "classification": "mixed", "evidence_ids": [E1, E2]},
        ],
    )
    result = _build(
        {
            "conflict": [
                {
                    "statement": "The paper contradicts itself.",
                    "evidence_ids": [E1, E2],
                    "comparability_key": KEY,
                }
            ]
        },
        bundle=bundle,
    )

    assert result.conflict == ()
    assert [item["reason"] for item in result.rejected] == ["single_work_conflict"]


# --- 规则 3：条件维度必须来自本体 ------------------------------------------------


def test_rule3_accepts_a_dimension_from_the_task_ontology():
    result = _build(
        {
            "conditional": [
                {
                    "dimension": "pretraining_corpus",
                    "statement": "The gain only holds for domain-pretrained backbones.",
                    "evidence_ids": [E1],
                }
            ]
        },
        dimensions=frozenset(CORE_DIMENSIONS) | {"pretraining_corpus"},
    )

    assert [entry.dimension for entry in result.conditional] == ["pretraining_corpus"]


def test_rule3_rejects_a_freely_invented_dimension():
    result = _build(
        {
            "conditional": [
                {
                    "dimension": "vibes",
                    "statement": "It depends on the setting.",
                    "evidence_ids": [E1],
                }
            ]
        }
    )

    assert result.conditional == ()
    assert [item["reason"] for item in result.rejected] == ["unknown_dimension"]


# --- 规则 4：只有摘要级证据的条目降级成 gap ---------------------------------------


def test_rule4_demotes_an_abstract_only_entry_into_gap_with_a_note():
    bundle = _bundle(evidence=[_unit(E1, grade="D_abstract_only", text="Improves performance.")])
    result = _build(
        {"agreement": [{"statement": "The method helps.", "evidence_ids": [E1]}]},
        bundle=bundle,
    )

    assert result.agreement == ()
    assert len(result.gap) == 1
    assert result.gap[0].demoted_from == "agreement"
    assert "abstract-only" in (result.gap[0].note or "")
    assert result.gap[0].evidence_ids == (E1,)


def test_rule4_keeps_an_entry_that_mixes_abstract_and_fulltext_evidence():
    bundle = _bundle(
        evidence=[_unit(E1, grade="D_abstract_only", text="Improves performance."), _unit(E2)]
    )
    result = _build(
        {"agreement": [{"statement": "Both report gains.", "evidence_ids": [E1, E2]}]},
        bundle=bundle,
    )

    assert len(result.agreement) == 1
    assert result.gap == ()


# --- 规则 5：数字必须在被引证据里找得到出处 ---------------------------------------


def test_rule5_rejects_a_statement_with_a_fabricated_percentage():
    result = _build(
        {
            "agreement": [
                {"statement": "The method improves AUROC by 7.4%.", "evidence_ids": [E1]}
            ]
        }
    )

    assert result.agreement == ()
    assert [item["reason"] for item in result.rejected] == ["unsourced_number"]


def test_rule5_nulls_a_claim_containing_a_fabricated_percentage():
    """对抗用例：claim 不带 evidence_ids，出处池是整个 bundle——仍然不许编。"""
    result = _build({"claim": "Across studies the method gains 12.5 points."})

    assert result.claim is None
    assert [item["kind"] for item in result.rejected] == ["claim"]


def test_rule5_accepts_a_number_quoted_from_the_evidence_text():
    result = _build(
        {"agreement": [{"statement": "AUROC reaches 0.912.", "evidence_ids": [E1]}]}
    )

    assert len(result.agreement) == 1


def test_rule5_accepts_a_number_that_only_exists_in_a_measurement_row():
    """表格里的数值同样是合法出处；否则结构化抽取的结果永远写不进正文。"""
    bundle = _bundle(
        evidence=[
            _unit(
                E1,
                text="Table 3 reports the main comparison.",
                measurements=[
                    {
                        "metric_name": "ndcg@10",
                        "value": 0.4471,
                        "unit": None,
                        "dataset": "beauty",
                        "task": "seq_rec",
                        "sample_size": None,
                        "split": "leave-one-out",
                        "comparability_key": KEY,
                    }
                ],
            )
        ]
    )
    result = _build(
        {"agreement": [{"statement": "NDCG@10 is 0.4471.", "evidence_ids": [E1]}]},
        bundle=bundle,
    )

    assert len(result.agreement) == 1


def test_rule5_does_not_reject_a_year_or_a_table_number():
    """NUMLINT 的非实验数值豁免必须继承过来，否则正常句子会被大面积误杀。"""
    result = _build(
        {
            "agreement": [
                {"statement": "Table 2 summarises the 2020 benchmark.", "evidence_ids": [E1]}
            ]
        }
    )

    assert len(result.agreement) == 1


# --- 结构性行为 ------------------------------------------------------------------


def test_a_gap_entry_needs_no_evidence_ids():
    result = _build({"gap": [{"statement": "No study evaluates the cross-domain setting."}]})

    assert [entry.statement for entry in result.gap] == [
        "No study evaluates the cross-domain setting."
    ]


def test_an_all_rejected_response_is_empty_and_carries_its_reasons():
    result = _build(
        {"agreement": [{"statement": "Gains of 99%.", "evidence_ids": [E1]}], "gap": []}
    )

    assert result.is_empty
    assert result.rejected


def test_a_malformed_response_is_handled_as_empty():
    result = _build({"agreement": "not a list", "conflict": None, "claim": 42})

    assert result.is_empty


def test_skips_a_question_whose_evidence_is_already_a_gap():
    """证据不足的子问题已经被标成缺口；让模型去"综合"零条证据只会诱导补全。"""
    assert not should_synthesize(_bundle(answer_status="insufficient_evidence"))
    assert not should_synthesize(_bundle(evidence=[_unit(E1)]))
    assert not should_synthesize(
        _bundle(evidence=[_unit(E1), _unit(E2, grade="D_abstract_only")])
    )
    assert should_synthesize(_bundle())


def test_the_fingerprint_ignores_the_attached_synthesis_but_tracks_the_evidence():
    bundle = _bundle()
    baseline = bundle_fingerprint(bundle)

    bundle["synthesis"] = {"claim": "anything"}
    assert bundle_fingerprint(bundle) == baseline

    bundle["evidence"][0]["text"] = "A different result."
    assert bundle_fingerprint(bundle) != baseline


def test_the_rendered_bundle_marks_clusters_and_never_leaks_a_reference_entry():
    rendered = render_bundle(_bundle())

    assert f"[COMPARABLE CLUSTER comparability_key={KEY}]" in rendered
    assert f"EVIDENCE_ID={E1}" in rendered
    assert "work_id=" in rendered


def test_an_oversized_response_is_bounded_on_both_ends():
    """降级成 gap 的条目不计入 kept；上限必须落在读入条数上，否则超长响应仍会走完。"""
    result = _build(
        {
            "agreement": [
                {"statement": f"Point {i}.", "evidence_ids": [E1]} for i in range(200)
            ],
            "gap": [{"statement": f"Gap {i}."} for i in range(200)],
        }
    )

    assert len(result.agreement) == MAX_ENTRIES_PER_KIND
    assert len(result.gap) == MAX_ENTRIES_PER_KIND


# --- 署名归属：提示词第 6 条不能只靠提示词 -------------------------------------


def test_an_author_year_attribution_is_stripped_from_a_statement():
    """影子评估实测：92 条通过校验的条目里仍有一条写了「Qin等人（2025）」。

    综合语句是给写作器的论证指引，里面的作者—年份既没有绑定也不在白名单里；
    删掉署名、保留内容，出处仍由 evidence_ids 承担。
    """
    result = _build(
        {
            "gap": [
                {"statement": "Qin等人（2025）在摘要中提及联邦序列推荐的防御策略，具体机制未描述。"}
            ]
        }
    )

    statement = result.gap[0].statement
    assert "Qin" not in statement
    assert "2025" not in statement
    assert "在摘要中提及联邦序列推荐的防御策略" in statement


def test_the_latin_form_is_stripped_too():
    result = _build(
        {
            "agreement": [
                {
                    "statement": "Smith et al. (2020) report the same trend.",
                    "evidence_ids": [E1],
                }
            ]
        }
    )

    assert result.agreement[0].statement == "report the same trend."


def test_a_year_without_a_citation_shape_survives():
    """"2020 年的基准" 不是署名；过度删除会把正常句子改坏。"""
    result = _build(
        {
            "agreement": [
                {"statement": "Both studies use the 2020 benchmark.", "evidence_ids": [E1]}
            ]
        }
    )

    assert result.agreement[0].statement == "Both studies use the 2020 benchmark."


def test_a_rejection_records_the_offending_value():
    """只记"维度不认识"没法行动；要能追到是哪个维度才知道该不该扩本体。"""
    result = _build(
        {
            "conditional": [
                {"dimension": "retriever", "statement": "It depends.", "evidence_ids": [E1]}
            ],
            "conflict": [
                {
                    "statement": "They disagree.",
                    "evidence_ids": [E1, E2],
                    "comparability_key": "made-up",
                }
            ],
        }
    )

    values = {item["reason"]: item["value"] for item in result.rejected}
    assert values["unknown_dimension"] == "retriever"
    assert values["unknown_comparability_key"] == "made-up"


def test_rejections_without_a_specific_value_leave_it_empty():
    result = _build(
        {"agreement": [{"statement": "Gains of 99%.", "evidence_ids": [E1]}]}
    )

    assert result.rejected[0]["value"] == ""
