"""语义评审：判定的收口、验收判据、修复分流，以及「不制造冲突」这条铁律。

这些用例锁的是**判据本身**，不是模型：模型输出用桩注入，断言集中在
``build_section_verdict`` / ``build_comparability_groups`` / ``repair_route``
这三个纯函数上——它们决定了一份判定会把稿子送到哪条修复路上。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from llm_runtime import LLMConfig, LLMResponse, LLMRunner
from paperforge_worker.pipelines.semantic_review import (
    MODERATORS,
    SectionVerdict,
    build_comparability_groups,
    build_section_verdict,
    compare_findings,
    repair_route,
    review_section,
)

EVIDENCE = [
    {
        "evidence_id": "e-1",
        "work_id": "w-1",
        "cite_key": "a2025",
        "grade": "B_located_prose",
        "text": "施用菌株 A 后，田间小麦产量提高。",
    },
    {
        "evidence_id": "e-2",
        "work_id": "w-2",
        "cite_key": "b2025",
        "grade": "B_located_prose",
        "text": "同类菌株在田间试验中未观察到产量提高。",
    },
]


class _Provider:
    def __init__(self, payloads: list[Any]) -> None:
        self.payloads = list(payloads)
        self.prompts: list[str] = []

    def generate(self, request):
        self.prompts.append(request.user_prompt)
        payload = self.payloads.pop(0) if self.payloads else {}
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        return LLMResponse(text=text, model="stub", provider="stub")


def _runner(payloads: list[Any]) -> tuple[LLMRunner, _Provider]:
    provider = _Provider(payloads)
    return (
        LLMRunner(
            LLMConfig(provider="openai", base_url="http://stub", api_key="k"), provider=provider
        ),
        provider,
    )


def _verdict(**overrides: Any) -> SectionVerdict:
    base = {
        "section_key": "s1",
        "answers_question": "full",
        "support": "sufficient",
        "calibration": "matched",
        "synthesis_mode": "synthesized",
        "diagnosis": "none",
        "rationale": "",
    }
    return SectionVerdict(**{**base, **overrides})


# ---- 验收判据 ------------------------------------------------------------------


def test_a_section_that_answers_and_is_supported_passes() -> None:
    assert _verdict().acceptable


@pytest.mark.parametrize(
    "overrides",
    [
        {"answers_question": "no"},
        {"support": "unsupported"},
        {"calibration": "overclaimed"},
        {"synthesis_mode": "listed"},
        {"answers_question": "partial", "gap_declared": False},
    ],
)
def test_each_failure_mode_is_rejected(overrides: dict[str, Any]) -> None:
    assert not _verdict(**overrides).acceptable


def test_named_unsupported_claims_block_even_when_the_tone_is_calibrated() -> None:
    """整体语气不过强，不代表里面没有超证据的句子。

    实测 s2 卡在这个缝里：calibration=matched、mode=mixed、缺口也声明了，于是判为合格，
    可评审器同时点名三条超出证据的论断——其中一条把「微生物 VOCs 激活茉莉酸/乙烯/
    水杨酸通路」写成已知事实，而证据讲的是细胞壁多糖激发子诱导 ISR/SAR。
    """
    verdict = _verdict(
        answers_question="partial",
        gap_declared=True,
        synthesis_mode="mixed",
        unsupported_claims=("微生物VOCs通过激活茉莉酸/乙烯通路使植物进入防御准备状态",),
    )
    assert not verdict.acceptable
    assert repair_route(verdict) == "rewrite"


def test_a_declared_gap_is_an_acceptable_partial_answer() -> None:
    """证据真的不够时，明说缺口是合格行为——目标明确禁止为了凑长度灌水。"""
    assert _verdict(answers_question="partial", gap_declared=True).acceptable


# ---- 修复分流 ------------------------------------------------------------------


def test_missing_aspects_with_thin_evidence_go_to_retrieval_not_rewriting() -> None:
    """证据撑不住的缺口，重写正文只会把缺口写得更委婉。"""
    verdict = _verdict(
        answers_question="partial",
        support="thin",
        unanswered_aspects=("低丰度检测的挑战",),
        gap_declared=False,
    )
    assert repair_route(verdict) == "retrieve"


def test_listing_with_adequate_evidence_goes_to_synthesis() -> None:
    verdict = _verdict(synthesis_mode="listed")
    assert repair_route(verdict) == "resynthesize"


def test_claims_beyond_the_evidence_go_to_rewriting() -> None:
    verdict = _verdict(calibration="overclaimed", unsupported_claims=("独脚金内酯改善磷吸收",))
    assert repair_route(verdict) == "rewrite"


def test_a_route_already_tried_escalates_to_the_next_one() -> None:
    """同一条路走过而判定没变，就换下一条——不对着同一个病因重复付钱。"""
    verdict = _verdict(
        answers_question="partial",
        support="thin",
        unanswered_aspects=("田间验证",),
        gap_declared=False,
    )
    assert repair_route(verdict, attempted=frozenset({"retrieve"})) != "retrieve"
    assert (
        repair_route(verdict, attempted=frozenset({"retrieve", "resynthesize", "rewrite"}))
        == "none"
    )


def test_an_acceptable_section_is_never_repaired() -> None:
    assert repair_route(_verdict()) == "none"


def test_retrieval_is_skipped_when_the_section_never_used_what_it_already_has() -> None:
    """「证据薄」有两种：没检索到，和检索到了没写进去。后者补检索只会垒得更高。

    线上实测的分界（项目 6a6bbf18 第 6 版）：合格章节用掉证据池的 77–86%、
    只剩 2–3 条没用；不合格的三节只用掉 25–46%、各剩 13–18 条。
    """
    verdict = _verdict(
        answers_question="partial",
        support="thin",
        unanswered_aspects=("低丰度检测的挑战",),
        gap_declared=False,
    )
    assert repair_route(verdict, pool_size=24, unused_evidence=18) != "retrieve"
    # 池子小或者用得足的照旧走补检索——闸只挡「手上还有一堆没用」这一种。
    assert repair_route(verdict, pool_size=3, unused_evidence=3) == "retrieve"
    assert repair_route(verdict, pool_size=14, unused_evidence=2) == "retrieve"
    assert repair_route(verdict) == "retrieve"


# ---- 判定收口 ------------------------------------------------------------------


def test_unknown_enum_values_fall_back_to_the_lenient_end() -> None:
    """解析噪声不该被当成缺陷：评审器的作用是发现问题，不是制造问题。"""
    verdict = build_section_verdict(
        {"answers_question": "maybe", "support": "???", "synthesis_mode": ""},
        section_key="s1",
    )
    assert verdict.acceptable
    assert verdict.diagnosis == "none"


def test_a_failing_verdict_without_a_cause_still_gets_routed() -> None:
    verdict = build_section_verdict(
        {"answers_question": "no", "support": "unsupported", "diagnosis": "none"},
        section_key="s3",
    )
    assert not verdict.acceptable
    assert verdict.diagnosis == "evidence_gap"


async def test_a_failed_review_call_returns_no_verdict_rather_than_a_failure() -> None:
    """评审器不可用时保持既有行为，绝不把「没判」当成「不合格」。"""
    runner, _provider = _runner(["not json"])
    assert (
        await review_section(
            section_key="s1", question="Q?", prose="正文", evidence=EVIDENCE, runner=runner
        )
        is None
    )


# ---- 分歧：既要抓得到，也不许编 ---------------------------------------------------


async def test_a_genuine_contradiction_across_two_works_is_reported() -> None:
    """故障注入：两篇文献对同一结论给出相反结果，必须判为 contradictory。

    没有这条，「几乎全是 complementary」的真实结果就无法与「这个评审器只会说
    complementary」区分开。
    """
    runner, _provider = _runner(
        [
            {
                "groups": [
                    {
                        "claim_topic": "菌株施用对田间产量的影响",
                        "relation": "contradictory",
                        "members": [
                            {"evidence_id": "e-1", "polarity": "supports"},
                            {"evidence_id": "e-2", "polarity": "opposes"},
                        ],
                        "rationale": "同一终点下结论相反",
                    }
                ]
            }
        ]
    )
    groups = await compare_findings(question="菌株能否提高产量？", evidence=EVIDENCE, runner=runner)
    assert groups is not None
    assert [g.relation for g in groups] == ["contradictory"]
    assert groups[0].work_ids == {"w-1", "w-2"}


def test_a_contradiction_inside_one_paper_is_not_a_disagreement() -> None:
    """同一篇文献内部的措辞差异不是学界分歧——降级为 complementary。"""
    same_work = [{**EVIDENCE[0]}, {**EVIDENCE[1], "work_id": "w-1"}]
    groups = build_comparability_groups(
        {
            "groups": [
                {
                    "claim_topic": "产量",
                    "relation": "contradictory",
                    "members": [
                        {"evidence_id": "e-1", "polarity": "supports"},
                        {"evidence_id": "e-2", "polarity": "opposes"},
                    ],
                }
            ]
        },
        evidence=same_work,
    )
    assert [g.relation for g in groups] == ["complementary"]


def test_same_polarity_is_not_a_contradiction() -> None:
    groups = build_comparability_groups(
        {
            "groups": [
                {
                    "claim_topic": "产量",
                    "relation": "contradictory",
                    "members": [
                        {"evidence_id": "e-1", "polarity": "supports"},
                        {"evidence_id": "e-2", "polarity": "supports"},
                    ],
                }
            ]
        },
        evidence=EVIDENCE,
    )
    assert [g.relation for g in groups] == ["complementary"]


def test_moderated_must_name_a_moderator_from_the_closed_set() -> None:
    """「大概是条件不同吧」不是可核验的判断，降级为 incomparable。"""
    vague = build_comparability_groups(
        {
            "groups": [
                {
                    "claim_topic": "产量",
                    "relation": "moderated",
                    "moderator": "大概是别的原因",
                    "members": [
                        {"evidence_id": "e-1", "polarity": "supports"},
                        {"evidence_id": "e-2", "polarity": "opposes"},
                    ],
                }
            ]
        },
        evidence=EVIDENCE,
    )
    assert [g.relation for g in vague] == ["incomparable"]

    named = build_comparability_groups(
        {
            "groups": [
                {
                    "claim_topic": "产量",
                    "relation": "moderated",
                    "moderator": "population",
                    "members": [
                        {"evidence_id": "e-1", "polarity": "supports"},
                        {"evidence_id": "e-2", "polarity": "opposes"},
                    ],
                }
            ]
        },
        evidence=EVIDENCE,
    )
    assert [g.relation for g in named] == ["moderated"]
    assert named[0].moderator in MODERATORS


def test_evidence_ids_outside_the_bundle_are_dropped() -> None:
    """出处必须守住：模型引一个不存在的 ID，那一条不能进结果。"""
    groups = build_comparability_groups(
        {
            "groups": [
                {
                    "claim_topic": "产量",
                    "relation": "complementary",
                    "members": [
                        {"evidence_id": "e-1", "polarity": "supports"},
                        {"evidence_id": "ghost", "polarity": "supports"},
                    ],
                }
            ]
        },
        evidence=EVIDENCE,
    )
    # 只剩一条成员的组不成其为「关系」，整组丢弃。
    assert groups == []


def test_member_provenance_is_carried_through() -> None:
    groups = build_comparability_groups(
        {
            "groups": [
                {
                    "claim_topic": "产量",
                    "relation": "complementary",
                    "members": [
                        {"evidence_id": "e-1", "polarity": "supports", "condition": "田间"},
                        {"evidence_id": "e-2", "polarity": "mixed"},
                    ],
                }
            ]
        },
        evidence=EVIDENCE,
    )
    member = groups[0].members[0]
    assert member["work_id"] == "w-1"
    assert member["cite_key"] == "a2025"
    assert member["condition"] == "田间"
