"""R5 只该拦「拿不可比的数字说事」，不该拦一切定性比较。

生产实测（2026-08-26）：27 条被 R5 删掉的句子里，17 条只引 1 篇文献（研究内比较），
26 条的证据单元没有任何量测；全量 102 条 comparison 类论断 ``supported`` 为 0 条。
被删的甚至包括三条综述自己在说「结果不可直接比较」的句子——R5 删掉了它要强制的
那句话本身。

根因：``classify_claim`` 把 ``numeric`` 排在 ``comparison`` 之前，所以 R5 只会
作用在**不含数字**的句子上；而它的判据是「共享量测 comparability_key」，一个
量测概念。判据与适用对象错位，于是永远只能返回 False。
"""

from __future__ import annotations

import pytest
from paperforge_worker.comparability import (
    comparison_admissible,
    comparison_scope,
    measured_comparability,
)
from paperforge_worker.pipelines.writing import enforce_sentence_evidence_rules


def _unit(work: str, *, keys: list[str] | None = None, **extra) -> dict:
    return {
        "evidence_id": f"e-{work}",
        "work_id": work,
        "cite_key": work,
        "grade": "B_located_prose",
        "section_path": "Results",
        "paragraph_index": 1,
        "measurements": [{"comparability_key": k} for k in (keys or [])],
        **extra,
    }


# --- 比较范围 ---------------------------------------------------------------


def test_comparison_scope_separates_within_study_from_cross_study():
    assert comparison_scope([]) == "no_evidence"
    assert comparison_scope([_unit("A"), _unit("A")]) == "within_study"
    assert comparison_scope([_unit("A"), _unit("B")]) == "cross_study"


def test_measured_comparability_says_nothing_when_there_is_nothing_measured():
    """两边都没有量测时返回 None——「无话可说」，不是「不可比」。"""
    assert measured_comparability([_unit("A"), _unit("B")]) is None
    # 只有一边有量测，同样凑不成一次比较
    assert measured_comparability([_unit("A", keys=["acc@ml1m"]), _unit("B")]) is None


def test_measured_comparability_is_the_real_r5_test():
    shared = [_unit("A", keys=["acc@ml1m"]), _unit("B", keys=["acc@ml1m", "ndcg"])]
    disjoint = [_unit("A", keys=["acc@ml1m"]), _unit("B", keys=["acc@cifar10"])]
    assert measured_comparability(shared) is True
    assert measured_comparability(disjoint) is False


# --- 准入判定 ---------------------------------------------------------------


def test_a_within_study_comparison_is_admissible():
    """「GSPAttack 优于所有基线方法」引的是提出它的那篇论文自己的实验。

    这是综述里最常见的句式，由该研究自身的对照实验支持。此前 ``< 2 篇文献``
    直接返回 False，把「不是跨研究比较」当成了「跨研究比较且不可比」。
    """
    assert comparison_admissible([_unit("A")]) is True
    assert comparison_admissible([_unit("A"), _unit("A", keys=["acc"])]) is True


def test_a_qualitative_cross_study_comparison_is_admissible():
    """比较的是方法路线而不是数值时，量测可比性判据让位。

    后面还有证据等级（R4）、定位（R6）与质检词面支持度继续把关。
    """
    assert comparison_admissible([_unit("A"), _unit("B")]) is True


def test_an_incomparable_measured_cross_study_comparison_is_still_refused():
    """R5 真正的战场逐字不变：两边都有量测，但没有共享的可比键。"""
    units = [_unit("A", keys=["acc@ml1m"]), _unit("B", keys=["acc@cifar10"])]
    assert comparison_admissible(units) is False


def test_a_comparable_measured_cross_study_comparison_is_admissible():
    units = [_unit("A", keys=["acc@ml1m"]), _unit("B", keys=["acc@ml1m"])]
    assert comparison_admissible(units) is True


def test_a_comparison_with_no_evidence_at_all_is_not_admissible():
    assert comparison_admissible([]) is False


def test_units_may_be_objects_as_well_as_dicts():
    from types import SimpleNamespace

    objs = [
        SimpleNamespace(work_id="A", measurements=[SimpleNamespace(comparability_key="acc")]),
        SimpleNamespace(work_id="B", measurements=[SimpleNamespace(comparability_key="acc")]),
    ]
    assert comparison_admissible(objs) is True


# --- 端到端：R5 在写作阶段的行为 -----------------------------------------------

#: 含「优于」触发 comparison；刻意不含数字——含数字会先被判成 numeric 走 R6。
WITHIN = "GSPAttack 优于所有基线方法，且对多种防御措施具有抵抗力。"
CROSS = "与修改现有节点特征或边相比，注入新节点通常更可行，因为攻击者无需控制真实用户账户。"


def _paragraph(text: str, evidence_ids: list[str]):
    return [{"sentences": [{"text": text, "cite_keys": ["k"], "evidence_ids": evidence_ids}]}]


def _survivors(paragraphs):
    return [s for p in paragraphs for s in p.get("sentences") or []]


def test_a_within_study_comparison_survives_r5():
    """17/27 误删的那一类。"""
    out = enforce_sentence_evidence_rules(
        _paragraph(WITHIN, ["e-A"]),
        evidence_by_id={"e-A": _unit("A")},
        language="zh",
    )
    kept = _survivors(out)
    assert len(kept) == 1
    assert kept[0]["text"] == WITHIN
    assert not kept[0].get("downgraded_reason")


def test_a_qualitative_cross_study_comparison_survives_r5():
    out = enforce_sentence_evidence_rules(
        _paragraph(CROSS, ["e-A", "e-B"]),
        evidence_by_id={"e-A": _unit("A"), "e-B": _unit("B")},
        language="zh",
    )
    assert len(_survivors(out)) == 1


def test_an_incomparable_measured_comparison_is_still_deleted():
    """放宽不是取消：R5 原本要拦的那类照旧删，并照旧留痕。"""
    out = enforce_sentence_evidence_rules(
        _paragraph(CROSS, ["e-A", "e-B"]),
        evidence_by_id={
            "e-A": _unit("A", keys=["acc@ml1m"]),
            "e-B": _unit("B", keys=["acc@cifar10"]),
        },
        language="zh",
    )
    assert _survivors(out) == []
    downgraded = [s for p in out for s in p.get("downgraded_sentences") or []]
    assert [s["downgraded_reason"] for s in downgraded] == ["R5_not_comparable"]
    # Step 2 的审计线索不受影响
    assert downgraded[0]["downgraded_text"] == CROSS


def test_the_review_may_say_that_results_are_not_comparable():
    """被删的句子里有三条正是综述在说明结果不可比——那正是 R5 要强制的立场。"""
    text = (
        "这些数据集在样本规模和特征维度上差异显著，"
        "因此不同数据集上的实验结果不能直接进行跨研究比较。"
    )
    out = enforce_sentence_evidence_rules(
        _paragraph(text, ["e-A"]),
        evidence_by_id={"e-A": _unit("A")},
        language="zh",
    )
    assert len(_survivors(out)) == 1


@pytest.mark.parametrize(
    "text",
    [
        "Shilling-Rank 通过训练过程中真实数据与假数据的内在属性差异进行区分。",
        "该模型在提升目标物品的推荐上优于基线统计和生成式托攻击。",
    ],
)
def test_method_descriptions_that_merely_contain_a_comparison_word_survive(text):
    """「差异」「优于」出现在方法描述里，并不使它成为跨研究比较。"""
    out = enforce_sentence_evidence_rules(
        _paragraph(text, ["e-A"]),
        evidence_by_id={"e-A": _unit("A")},
        language="zh",
    )
    assert len(_survivors(out)) == 1


# --- 其余闸门未被放宽 ---------------------------------------------------------


def test_a_within_study_comparison_on_abstract_evidence_is_still_downgraded():
    """R4 照旧：只有摘要级证据的比较句被改写成显式归因，不当成已证实。"""
    out = enforce_sentence_evidence_rules(
        _paragraph(WITHIN, ["e-A"]),
        evidence_by_id={"e-A": _unit("A", grade="D_abstract_only")},
        language="zh",
    )
    kept = _survivors(out)
    assert len(kept) == 1
    assert kept[0]["downgraded_reason"] == "R4_abstract_attribution"


def test_a_within_study_comparison_with_no_evidence_binding_is_still_removed():
    """没有证据绑定的论断照旧拿掉——放宽只作用于「有证据、但被误判不可比」。"""
    out = enforce_sentence_evidence_rules(
        _paragraph(WITHIN, []),
        evidence_by_id={},
        language="zh",
    )
    assert _survivors(out) == []
