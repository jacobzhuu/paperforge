"""`visual_input_hash` 回答的是「这条建议是不是已经提过了」。

因此它必须对语义敏感、对措辞不敏感——否则每轮 visual_plan 都会把同一张插图
重新提一遍，或者反过来漏掉一条真正的新建议。
"""

from __future__ import annotations

from db.repositories.visuals import visual_input_hash

_SPEC = {
    "kind": "ai_image",
    "prompt": "root system anchoring a slope",
    "size": "1536x1024",
    "quality": "medium",
    "semantics": {"subject": "root system anchoring a slope", "elements": []},
}


def test_refined_prompt_does_not_change_the_hash() -> None:
    """润色结果每次用词都不同；把它算进哈希等于关掉去重。"""
    baseline = visual_input_hash(_SPEC)
    refined = visual_input_hash({**_SPEC, "refined_prompt": "A wide conceptual illustration …"})
    other_wording = visual_input_hash({**_SPEC, "refined_prompt": "A cinematic cross-section …"})
    assert baseline == refined == other_wording


def test_semantic_changes_still_change_the_hash() -> None:
    changed = visual_input_hash(
        {**_SPEC, "semantics": {"subject": "a completely different subject", "elements": []}}
    )
    assert changed != visual_input_hash(_SPEC)


def test_unset_optional_fields_keep_historical_hashes_intact() -> None:
    """新增可选字段在未填写时，哈希必须与老版本完全一致。"""
    assert visual_input_hash({**_SPEC, "refined_prompt": None, "style": ""}) == visual_input_hash(
        _SPEC
    )
