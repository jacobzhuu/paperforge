"""全篇级缺陷：逐节检查看不见，合起来才成立。

两条都来自真实稿子的观察：
- v4 的结论段落一处限定措辞都没有，而正文每千字有 2.6 处——正文老实写「证据尚不
  统一」，收尾却讲得斩钉截铁。读者只会记住结论。
- 同一段论述在两个小节里各写一遍，两节单独读都成立。
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from paperforge_worker.pipelines.quality import (
    RECOVERABLE_BLOCKER_CODES,
    conclusion_overreach,
    cross_section_repetition,
)

SHARED = (
    "根系分泌物中的黄酮类化合物能够选择性富集根际有益细菌，并通过改变土壤化学性质影响群落装配过程。"
)


def _row(section_key: str, *sentences: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        section_key=section_key,
        title=section_key,
        status="generated",
        cite_keys_json=[],
        body_ir_json={
            "blocks": [
                {
                    "type": "paragraph",
                    "runs": [{"t": "text", "v": " ".join(sentences)}],
                }
            ]
        },
    )


def test_the_same_argument_written_twice_is_caught() -> None:
    rows = [_row("s2", SHARED), _row("s4", SHARED)]
    findings = cross_section_repetition(rows)
    assert len(findings) == 1
    assert findings[0]["sections"] == ["s2", "s4"]
    assert findings[0]["overlap"] >= 0.75


def test_frame_sections_are_allowed_to_restate_the_body() -> None:
    """摘要、引言、结论、证据台账复述全篇是它们的职责，不该被判重复。"""
    rows = [_row("s2", SHARED), _row("abstract", SHARED), _row("conclusion", SHARED)]
    assert cross_section_repetition(rows) == []


def test_two_sections_on_different_topics_are_not_flagged() -> None:
    rows = [
        _row("s2", SHARED),
        _row(
            "s5",
            "液相色谱-质谱联用结合非靶向代谢组学可以系统捕捉互作过程中的化学变化，"
            "但代谢物注释仍受限于谱库覆盖度。",
        ),
    ]
    assert cross_section_repetition(rows) == []


def test_a_conclusion_that_drops_every_qualifier_is_flagged() -> None:
    # 长度取真实量级：判据本身有下限（正文 600 字 / 结论 120 字），
    # 免得把一段还没写完的桩稿判成越界。
    body = _row(
        "s2",
        "现有证据提示该机制可能依赖具体基因型，田间条件下的结果尚不一致，仍有待验证。" * 20,
    )
    conclusion = _row(
        "conclusion",
        "天然产物信号决定了根际群落的组装方向，并重塑了微生物组结构，"
        "该机制已成为作物改良的可靠路径。" * 4,
    )
    finding = conclusion_overreach([body, conclusion])
    assert finding is not None
    assert finding["conclusion_hedges"] == 0
    assert finding["body_hedges_per_1k"] >= 1.0


def test_a_conclusion_that_keeps_the_qualifiers_passes() -> None:
    body = _row("s2", "现有证据提示该机制可能依赖具体基因型，尚需田间验证。" * 20)
    conclusion = _row(
        "conclusion",
        "天然产物信号在塑造根际群落中可能发挥关键作用，但田间证据尚不一致，"
        "其普适性有待进一步验证。" * 4,
    )
    assert conclusion_overreach([body, conclusion]) is None


def test_absolute_language_in_the_conclusion_is_flagged_on_its_own() -> None:
    """即使正文也不限定，「证明了/毫无疑问」这类措辞本身就超出了综述能给的结论。"""
    body = _row("s2", "该化合物在两项独立研究中富集了特定菌群。" * 40)
    conclusion = _row("conclusion", "本综述证明了天然产物信号毫无疑问地决定了根际群落结构。" * 5)
    finding = conclusion_overreach([body, conclusion])
    assert finding is not None
    assert "证明了" in finding["absolutes"]


def test_both_codes_are_repairable_rather_than_terminal() -> None:
    """检测到就该驱动重写——只报不修正是上一轮修掉的毛病。"""
    assert "cross_section_repetition" in RECOVERABLE_BLOCKER_CODES
    assert "conclusion_overreach" in RECOVERABLE_BLOCKER_CODES
