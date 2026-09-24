"""定位判定：一个谓词，四个消费方，以及不能被放宽的那部分。

背景（生产实测，2026-08-22，10,132 条证据单元）：管线自己标成
``C_fulltext_unlocated`` 的只有 26 条，而写作阶段的 R6 把另外 **5,256** 条已被
``_fulltext_grade`` 定级为「已定位」的单元当成未定位，把引用它们的数字句整句删掉。
两处对 ``located`` 的定义不一致，是这一族缺陷的根。
"""

from __future__ import annotations

import pytest
from paperforge_worker.locators import (
    evidence_locator,
    is_located,
    locator_display,
    locator_of,
)
from paperforge_worker.pipelines.evidence import _fulltext_grade
from paperforge_worker.pipelines.writing import enforce_sentence_evidence_rules


def _unit(**fields):
    base = {
        "evidence_id": "e-1",
        "cite_key": "k1",
        "grade": "B_located_prose",
        "page": None,
        "object_ref": None,
        "section_path": None,
        "paragraph_index": None,
    }
    return {**base, **fields}


# --- 机器判定与人可读表示同源 ------------------------------------------------


@pytest.mark.parametrize(
    ("fields", "kind", "display"),
    [
        ({"object_ref": "table:3"}, "object", "table:3"),
        ({"page": 6}, "page", "p.6"),
        ({"page": 6, "object_ref": "table:3"}, "object", "p.6, table:3"),
        ({"section_path": "Results", "paragraph_index": 4}, "section", "§Results ¶4"),
        ({"section_path": "Results"}, "section", "§Results"),
        (
            {"page": 6, "section_path": "Results", "paragraph_index": 4},
            "page",
            "p.6, §Results ¶4",
        ),
    ],
)
def test_a_located_unit_always_has_something_a_reader_can_follow(fields, kind, display):
    locator = locator_of(_unit(**fields))
    assert locator is not None
    assert (locator.kind, locator.display) == (kind, display)


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"page": None, "object_ref": "", "section_path": "  "},
        # 布尔是 int 的子类，但 True 不是页码。
        {"page": True},
        # 段落序号从不单独成立：生产库里 10,132 条单元中带段落却没有章节的是 0 条，
        # 而一个没有章节的段落序号也没法让人翻到原文。
        {"paragraph_index": 4},
    ],
)
def test_an_unlocatable_unit_has_no_locator_and_no_display(fields):
    unit = _unit(**fields)
    assert locator_of(unit) is None
    assert is_located(unit) is False
    assert locator_display(unit) is None


def test_the_predicate_and_the_display_can_never_disagree():
    """「判定说可定位、却给不出定位串」这个组合必须不可能出现。"""
    from itertools import product

    for page, obj, section, para in product(
        [None, 6], [None, "table:3"], [None, "Results"], [None, 4]
    ):
        unit = _unit(page=page, object_ref=obj, section_path=section, paragraph_index=para)
        assert is_located(unit) is (locator_display(unit) is not None)


def test_a_unit_object_is_read_the_same_way_as_a_dict():
    """抽取阶段传的是 EvidenceCandidate 对象，写作阶段传的是 bundle 里的 dict。"""
    from types import SimpleNamespace

    fields = {"page": None, "object_ref": None, "section_path": "Methods", "paragraph_index": 2}
    assert locator_of(SimpleNamespace(**fields)) == locator_of(dict(fields))


# --- 定级：B/C 的分界与 R6 是同一条线 ----------------------------------------


def test_prose_located_by_section_grades_as_located():
    assert _fulltext_grade(None, "Results", 4, None, "prose_only") == "B_located_prose"
    assert _fulltext_grade(None, "Results", None, None, "prose_only") == "B_located_prose"


def test_nothing_to_go_on_still_grades_as_unlocated():
    assert _fulltext_grade(None, None, None, None, "prose_only") == "C_fulltext_unlocated"
    # 段落序号单独出现不足以定位，定级与 R6 在这一点上也必须一致。
    assert _fulltext_grade(None, None, 4, None, "prose_only") == "C_fulltext_unlocated"


def test_grading_and_r6_agree_on_every_field_combination():
    """两处口径一旦再次分叉，这条就红。"""
    from itertools import product

    for page, obj, section, para in product(
        [None, 6], [None, "table:3"], [None, "Results"], [None, 4]
    ):
        graded_located = (
            _fulltext_grade(page, section, para, obj, "prose_only") == "B_located_prose"
        )
        unit = _unit(page=page, object_ref=obj, section_path=section, paragraph_index=para)
        assert graded_located is is_located(unit), (page, obj, section, para)


# --- A 级与 object_ref 的既有约束不得放宽 --------------------------------------


def test_a_grade_still_requires_a_real_structured_cell():
    """N4 / M1-9：A 级只给真正的结构化单元格，散文提及永远不算。"""
    assert _fulltext_grade(6, "Results", 1, "table:3", "structured_cell") == "A_located_structured"
    # 同样的字段，强度只是「散文里提到了表 3」——仍然是 B。
    assert _fulltext_grade(6, "Results", 1, "table:3", "object_mention") == "B_located_prose"
    # 结构化强度但没有 object_ref，也不够 A。
    assert _fulltext_grade(6, "Results", 1, None, "structured_cell") == "B_located_prose"
    # 结构化强度 + object_ref，但既无页码也无章节：仍然不够 A。
    assert _fulltext_grade(None, None, 1, "table:3", "structured_cell") == "B_located_prose"


def test_object_and_page_anchored_units_stay_accepted():
    """放宽只针对 prose_only；原本就通过的两档一条都不能掉。"""
    for fields in (
        {"object_ref": "table:3"},
        {"page": 6},
        {"page": 6, "object_ref": "figure:1"},
    ):
        assert is_located(_unit(**fields)) is True


# --- R6：这一族缺陷的回归 ------------------------------------------------------


def _numeric_paragraph() -> list[dict]:
    return [
        {
            "sentences": [
                {
                    "text": "Injecting 0.5% of profiles raised the hit rate from 0.7% to 14.2%.",
                    "cite_keys": ["k1"],
                    "evidence_ids": ["e-1"],
                }
            ]
        }
    ]


def test_a_numeric_sentence_survives_on_section_located_evidence():
    """5,256 条误拒的回归。

    此前 R6 只认 page/object_ref，于是一条被定级为 ``B_located_prose``、
    提示词里明明标着 §Results 的证据，会让引用它的整句从正文里消失。
    """
    paragraphs = enforce_sentence_evidence_rules(
        _numeric_paragraph(),
        evidence_by_id={"e-1": _unit(section_path="Results", paragraph_index=4)},
        language="en",
    )

    assert paragraphs, "the paragraph must not be emptied"
    kept = paragraphs[0]["sentences"]
    assert len(kept) == 1
    assert "14.2%" in kept[0]["text"]
    assert kept[0]["evidence_ids"] == ["e-1"]
    assert not kept[0].get("downgraded_reason")


def test_a_numeric_sentence_is_still_dropped_when_nothing_locates_it():
    """放宽不是取消。真的无从查证时，数字句照旧不能留在正文里。"""
    paragraphs = enforce_sentence_evidence_rules(
        _numeric_paragraph(),
        evidence_by_id={"e-1": _unit(grade="B_located_prose")},
        language="en",
    )

    kept = [s for p in paragraphs for s in p.get("sentences") or []]
    assert kept == []
    downgraded = [s for p in paragraphs for s in p.get("downgraded_sentences") or []]
    assert [s["downgraded_reason"] for s in downgraded] == ["R6_locator_missing"]


def test_an_abstract_only_unit_still_cannot_carry_a_bare_numeric_claim():
    """D 级单元也带 ``section_path='abstract'``，放宽定位不能让它开始支撑数字。

    它走的是 R4：``_sentence_grade_ok`` 先一步判定「只有摘要级证据」，句子被改写成
    显式归因而不是当作已证实的数字留在正文里。这条路径与 ``located`` 无关，这里钉住
    它没有因为放宽而改变。
    """
    paragraphs = enforce_sentence_evidence_rules(
        _numeric_paragraph(),
        evidence_by_id={
            "e-1": _unit(
                grade="D_abstract_only", section_path="abstract", paragraph_index=1
            )
        },
        language="en",
    )

    kept = [s for p in paragraphs for s in p.get("sentences") or []]
    assert len(kept) == 1
    assert kept[0]["downgraded_reason"] == "R4_abstract_attribution"
    assert kept[0]["text"].startswith("The cited work reports in its abstract that")


def test_the_locator_reaches_the_prompt_the_model_reads():
    """提示词里的定位串与 R6 的判定同源——不能再出现「标着 §Results 却被删」。"""
    from paperforge_worker.pipelines.writing import _evidence_context_block

    unit = _unit(section_path="Results", paragraph_index=4, text="…", grade="B_located_prose")
    block = _evidence_context_block(
        section={"title": "t", "stance_summary": "supported"},
        evidence=[unit],
        language="en",
    )

    assert "§Results ¶4" in block
    assert is_located(unit) is True


def test_evidence_locator_rejects_an_unknown_field():
    with pytest.raises(TypeError):
        evidence_locator(page=1, chapter="Results")  # type: ignore[call-arg]
