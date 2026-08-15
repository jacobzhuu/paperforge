"""语义评审 → 分流修复 → 重评这条闭环本身。

关注点不是模型判得准不准（那在 test_semantic_review 里用纯函数锁住），而是编排：
不合格的章节有没有被送到**对症**的那条修复路上、同一条路走不通会不会升级、
轮数有没有上限、以及评审器不可用时会不会把稿子误判成不合格。
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from paperforge_worker import worker
from paperforge_worker.config import WorkerSettings
from paperforge_worker.context import JobContext
from paperforge_worker.pipelines.semantic_review import SectionVerdict
from scholar_gateway import InMemoryHttpCache


def _context() -> JobContext:
    return JobContext(
        project_id=uuid.uuid4(),
        job_id=None,
        settings=WorkerSettings(llm_default_provider="noop"),
        session_factory=None,
        http_client=httpx.Client(),
        scholar_cache=InMemoryHttpCache(),
    )


def _inputs(*section_keys: str) -> list[dict[str, Any]]:
    return [
        {
            "section_key": key,
            "question_id": f"q-{key}",
            "question": f"{key} 的子问题？",
            "prose": "正文",
            "evidence": [{"evidence_id": "e-1", "work_id": "w-1", "text": "证据"}],
        }
        for key in section_keys
    ]


def _verdict(section_key: str, **overrides: Any) -> SectionVerdict:
    base = {
        "section_key": section_key,
        "answers_question": "full",
        "support": "sufficient",
        "calibration": "matched",
        "synthesis_mode": "synthesized",
        "diagnosis": "none",
        "rationale": "",
    }
    return SectionVerdict(**{**base, **overrides})


@pytest.fixture
def harness(monkeypatch):
    """把三条修复路都换成记录器，只观察编排决策。"""
    calls: dict[str, list[Any]] = {"retrieve": [], "resynthesize": [], "rewrite": [], "qmatrix": []}

    async def _retrieve(context, **kwargs):
        calls["retrieve"].append(kwargs)
        return {}

    async def _qmatrix(context, **kwargs):
        calls["qmatrix"].append(kwargs)

    async def _resynth(context):
        calls["resynthesize"].append(True)

    async def _rewrite(context, *, section_keys, language, paper_type):
        calls["rewrite"].append(sorted(section_keys))

    monkeypatch.setattr(worker, "_supplement_review_evidence", _retrieve)
    monkeypatch.setattr(worker, "_rebuild_question_matrix", _qmatrix)
    monkeypatch.setattr(worker, "_resynthesize_questions", _resynth)
    monkeypatch.setattr(worker, "repair_document_sections", _rewrite)
    return calls


async def test_a_clean_manuscript_triggers_no_repair(monkeypatch, harness) -> None:
    monkeypatch.setattr(worker, "_section_review_inputs", lambda ctx: _ok(_inputs("s1", "s2")))

    async def _review(**kwargs):
        return _verdict(kwargs["section_key"])

    monkeypatch.setattr(worker, "review_section", _review)
    payload = await worker._converge_section_semantics(
        _context(), language="zh", paper_type="review"
    )

    assert payload["unresolved"] == []
    assert harness == {"retrieve": [], "resynthesize": [], "rewrite": [], "qmatrix": []}
    assert len(payload["rounds"]) == 1


async def test_listing_is_repaired_by_regenerating_synthesis_not_by_rewriting_blindly(
    monkeypatch, harness
) -> None:
    """证据够、只是在罗列——该重跑综合。直接重写正文只会再罗列一遍。"""
    monkeypatch.setattr(worker, "_section_review_inputs", lambda ctx: _ok(_inputs("s2")))
    seen: list[int] = []

    async def _review(**kwargs):
        seen.append(1)
        # 第一轮不合格，重跑综合之后合格。
        if len(seen) == 1:
            return _verdict("s2", synthesis_mode="listed", diagnosis="synthesis_gap")
        return _verdict("s2")

    monkeypatch.setattr(worker, "review_section", _review)
    payload = await worker._converge_section_semantics(
        _context(), language="zh", paper_type="review"
    )

    assert harness["resynthesize"] == [True]
    assert harness["retrieve"] == []
    # 重跑综合之后正文必须跟着重写，否则新的综合永远进不了稿子——实测里 s5 就是
    # 这样「综合重跑了、正文一个字没变、下一轮判定当然也没变」。
    assert harness["rewrite"] == [["s2"]]
    assert payload["rounds"][0]["routes"] == {"s2": "resynthesize"}
    assert payload["unresolved"] == []


async def test_missing_evidence_retrieves_and_then_rewrites_the_section(
    monkeypatch, harness
) -> None:
    """补检索之后必须重写，否则新拿到的证据永远进不了正文。"""
    monkeypatch.setattr(worker, "_section_review_inputs", lambda ctx: _ok(_inputs("s5")))

    async def _review(**kwargs):
        return _verdict(
            "s5",
            answers_question="partial",
            support="thin",
            unanswered_aspects=("低丰度检测",),
            gap_declared=False,
            diagnosis="evidence_gap",
        )

    monkeypatch.setattr(worker, "review_section", _review)
    payload = await worker._converge_section_semantics(
        _context(), language="zh", paper_type="review"
    )

    assert len(harness["retrieve"]) == 1, "补检索只做一次，第二轮要换路而不是再检索一遍"
    assert harness["qmatrix"], "补检索之后要重建问题—证据矩阵"
    # 第一轮：补检索 + 重写（把新证据写进去）；第二轮：升级到重写。
    assert harness["rewrite"] == [["s5"], ["s5"]]
    # 一直不合格：轮数必须有上限，并且第二轮换一条路。
    assert len(payload["rounds"]) == worker.MAX_SEMANTIC_ROUNDS
    assert payload["rounds"][0]["routes"]["s5"] == "retrieve"
    assert payload["rounds"][1]["routes"]["s5"] != "retrieve"
    assert payload["unresolved"] == ["s5"]


async def test_the_last_repair_is_verified_before_reporting_the_result(
    monkeypatch, harness
) -> None:
    """最后一轮修完要再评一次。

    否则 ``unresolved`` 报的是**修之前**的判定：一个刚刚被修好的章节会被永远记成
    未解决，验收判据也就不再是判据。这里让最后那次评审返回合格，断言它进了结果。
    """
    monkeypatch.setattr(worker, "_section_review_inputs", lambda ctx: _ok(_inputs("s5")))
    seen: list[int] = []

    async def _review(**kwargs):
        seen.append(1)
        # 两轮都判不合格；只有收尾那次复评合格。
        if len(seen) <= worker.MAX_SEMANTIC_ROUNDS:
            return _verdict(
                "s5",
                answers_question="partial",
                support="thin",
                unanswered_aspects=("田间验证",),
                gap_declared=False,
            )
        return _verdict("s5")

    monkeypatch.setattr(worker, "review_section", _review)
    payload = await worker._converge_section_semantics(
        _context(), language="zh", paper_type="review"
    )

    assert len(seen) == worker.MAX_SEMANTIC_ROUNDS + 1, "收尾复评必须真的跑"
    assert payload["unresolved"] == []


async def test_overclaiming_goes_straight_to_rewriting(monkeypatch, harness) -> None:
    monkeypatch.setattr(worker, "_section_review_inputs", lambda ctx: _ok(_inputs("s6")))
    seen: list[int] = []

    async def _review(**kwargs):
        seen.append(1)
        if len(seen) == 1:
            return _verdict(
                "s6",
                calibration="overclaimed",
                unsupported_claims=("独脚金内酯改善磷吸收",),
                diagnosis="writing_gap",
            )
        return _verdict("s6")

    monkeypatch.setattr(worker, "review_section", _review)
    payload = await worker._converge_section_semantics(
        _context(), language="zh", paper_type="review"
    )

    assert harness["rewrite"] == [["s6"]]
    assert harness["retrieve"] == []
    assert payload["unresolved"] == []


async def test_a_reviewer_outage_never_marks_the_manuscript_unacceptable(
    monkeypatch, harness
) -> None:
    """评审器拿不到判定时保持既有交付判断——「没判」不是「不合格」。"""
    monkeypatch.setattr(worker, "_section_review_inputs", lambda ctx: _ok(_inputs("s1", "s2")))

    async def _review(**kwargs):
        return None

    monkeypatch.setattr(worker, "review_section", _review)
    payload = await worker._converge_section_semantics(
        _context(), language="zh", paper_type="review"
    )

    assert payload["unresolved"] == []
    assert harness == {"retrieve": [], "resynthesize": [], "rewrite": [], "qmatrix": []}


def _ok(value):
    """`_section_review_inputs` 是协程：每次调用都要给一个**新的**可等待对象。"""

    async def _coro(*_args, **_kwargs):
        return value

    return _coro()
