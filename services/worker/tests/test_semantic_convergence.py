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


def _inputs(*section_keys: str, pool_size: int = 1, unused: int = 0) -> list[dict[str, Any]]:
    return [
        {
            "section_key": key,
            "question_id": f"q-{key}",
            "question": f"{key} 的子问题？",
            "prose": "正文",
            "evidence": [{"evidence_id": "e-1", "work_id": "w-1", "text": "证据"}],
            "pool_size": pool_size,
            "unused_evidence": unused,
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
    calls: dict[str, list[Any]] = {
        "retrieve": [],
        "resynthesize": [],
        "rewrite": [],
        "qmatrix": [],
        "notes": [],
        "deepen": [],
    }

    async def _retrieve(context, **kwargs):
        calls["retrieve"].append(kwargs)
        return {}

    async def _deepen(context, **kwargs):
        calls["deepen"].append(kwargs)
        return {"added": 0}

    async def _qmatrix(context, **kwargs):
        calls["qmatrix"].append(kwargs)

    async def _resynth(context):
        calls["resynthesize"].append(True)

    async def _rewrite(context, *, section_keys, language, paper_type, notes=None):
        calls["rewrite"].append(sorted(section_keys))
        calls["notes"].append(notes or {})

    monkeypatch.setattr(worker, "_supplement_review_evidence", _retrieve)
    monkeypatch.setattr(worker, "_deepen_question_evidence", _deepen)
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
    assert harness == {
        "retrieve": [],
        "resynthesize": [],
        "rewrite": [],
        "qmatrix": [],
        "notes": [],
        "deepen": [],
    }
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


async def test_evidence_already_on_the_shelf_is_not_repaired_by_retrieving_more(
    monkeypatch, harness
) -> None:
    """判定说「证据薄」，但这一节的证据池里还压着 18 条一次没引用的——补检索是白花钱。

    实测原型：s5 有 24 条链接证据、正文只用了 6 条，两轮里第一轮被送去补检索，
    结果判定一字未变。证据不在手上和证据没用上是两回事。
    """
    monkeypatch.setattr(
        worker,
        "_section_review_inputs",
        lambda ctx: _ok(_inputs("s5", pool_size=24, unused=18)),
    )

    async def _review(**kwargs):
        return _verdict(
            "s5",
            answers_question="partial",
            support="thin",
            synthesis_mode="listed",
            unanswered_aspects=("低丰度检测",),
            gap_declared=False,
            diagnosis="evidence_gap",
        )

    monkeypatch.setattr(worker, "review_section", _review)
    payload = await worker._converge_section_semantics(
        _context(), language="zh", paper_type="review"
    )

    assert harness["retrieve"] == [], "证据池里还有没用上的，不该再去检索"
    assert payload["rounds"][0]["routes"]["s5"] == "resynthesize"
    note = harness["notes"][0]["s5"]
    assert "SYNTHESIS" in note, "罗列的病因要说给写作器听"
    # 但**不许**把「还有 N 条没用上，请用起来」写进指令。实测那 18 条里是生长素生理、
    # 玉米年产量、图题和测序建库流程，对这个子问题不可用——催着用等于让它灌水。
    assert "没用" not in note and "用起来" not in note


async def test_a_rewrite_repeats_when_it_has_new_sentences_to_fix(monkeypatch, harness) -> None:
    """重写指令随判定走：这一轮点名的句子换了，就还值得再写一次。

    实测 s2：第一轮点名「微生物 VOCs 使植物进入防御准备状态」，重写之后那句的主语
    确实改成了「有益微生物」，但同段里「VOCs 激活 ISR/SAR」还留着，第二轮点的是新的
    那句。按「一条路只走一次」，s2 第二轮拿不到任何修复——不是修不动，是没人再修。
    """
    monkeypatch.setattr(worker, "_section_review_inputs", lambda ctx: _ok(_inputs("s2")))
    seen: list[int] = []

    async def _review(**kwargs):
        seen.append(1)
        return _verdict(
            "s2",
            synthesis_mode="mixed",
            unsupported_claims=(f"第 {len(seen)} 轮点名的句子",),
        )

    monkeypatch.setattr(worker, "review_section", _review)
    payload = await worker._converge_section_semantics(
        _context(), language="zh", paper_type="review"
    )

    assert harness["rewrite"] == [["s2"], ["s2"]], "点名的句子变了就该再写一次"
    assert harness["notes"][0]["s2"] != harness["notes"][1]["s2"], "两轮指令必须不同"
    assert payload["unresolved"] == ["s2"]


async def test_a_rewrite_is_not_repeated_when_the_verdict_is_unchanged(
    monkeypatch, harness
) -> None:
    """反向保护：判定一字未变就别再花一次钱重写，换下一条路或停。"""
    monkeypatch.setattr(worker, "_section_review_inputs", lambda ctx: _ok(_inputs("s6")))

    async def _review(**kwargs):
        return _verdict("s6", synthesis_mode="mixed", unsupported_claims=("同一句永远不变的话",))

    monkeypatch.setattr(worker, "review_section", _review)
    payload = await worker._converge_section_semantics(
        _context(), language="zh", paper_type="review"
    )

    assert harness["rewrite"] == [["s6"]], "判定没变，不该重复重写"
    assert payload["unresolved"] == ["s6"]


async def test_a_small_evidence_pool_still_routes_to_retrieval(monkeypatch, harness) -> None:
    """反向保护：池子本来就小、又确实答不上，那就是真缺证据，闸不能误伤。"""
    monkeypatch.setattr(
        worker,
        "_section_review_inputs",
        lambda ctx: _ok(_inputs("s7", pool_size=3, unused=3)),
    )

    async def _review(**kwargs):
        return _verdict(
            "s7",
            answers_question="partial",
            support="thin",
            unanswered_aspects=("田间验证",),
            gap_declared=False,
            diagnosis="evidence_gap",
        )

    monkeypatch.setattr(worker, "review_section", _review)
    payload = await worker._converge_section_semantics(
        _context(), language="zh", paper_type="review"
    )

    assert payload["rounds"][0]["routes"]["s7"] == "retrieve"
    assert len(harness["retrieve"]) == 1


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
    assert harness == {
        "retrieve": [],
        "resynthesize": [],
        "rewrite": [],
        "qmatrix": [],
        "notes": [],
        "deepen": [],
    }


def _ok(value):
    """`_section_review_inputs` 是协程：每次调用都要给一个**新的**可等待对象。"""

    async def _coro(*_args, **_kwargs):
        return value

    return _coro()


async def test_thin_evidence_first_uses_what_the_library_already_holds(
    monkeypatch, harness
) -> None:
    """「证据薄」的第一反应不该是再买一批文献。

    实测（项目 ff6b9983 首轮全流程）：库里抽出 848 条证据单元，每个子问题只看排名
    前 24 条，5 个问题合计 120 条进分类器、最终挂上 26 条——97% 的证据从没被任何
    问题看过。这种情况下补检索买回来的新文献照样挤不进那 24 个位置。所以先加挂
    （候选池排除已挂单元，让第二梯队有一次被看见的机会），再谈补检索。
    """
    monkeypatch.setattr(worker, "_section_review_inputs", lambda ctx: _ok(_inputs("s5")))

    async def _review(**kwargs):
        return _verdict(
            "s5",
            answers_question="partial",
            support="thin",
            unanswered_aspects=("递送效率",),
            gap_declared=False,
            diagnosis="evidence_gap",
        )

    monkeypatch.setattr(worker, "review_section", _review)
    await worker._converge_section_semantics(_context(), language="zh", paper_type="review")

    assert len(harness["deepen"]) == 1
    assert harness["deepen"][0]["question_ids"] == {"q-s5"}


async def test_retrieval_is_told_which_questions_failed(monkeypatch, harness) -> None:
    """评审器刚刚判完哪一节缺证，那份判断不能在路上被丢掉。

    此前补检索不接收问题清单，改从 ``answer_status`` 重新推一遍缺口；实测 s4/s5 的
    问题都是 ``partial`` 而不是 ``insufficient_evidence``，于是缺口列表为空、一个检索
    请求都没发——事件流里只留下 ``routes: retrieve``，看起来按病因修了，实际只重写。
    """
    monkeypatch.setattr(worker, "_section_review_inputs", lambda ctx: _ok(_inputs("s4", "s5")))

    async def _review(**kwargs):
        return _verdict(
            kwargs["section_key"],
            answers_question="partial",
            support="thin",
            unanswered_aspects=("监管条款",),
            gap_declared=False,
            diagnosis="evidence_gap",
        )

    monkeypatch.setattr(worker, "review_section", _review)
    await worker._converge_section_semantics(_context(), language="zh", paper_type="review")

    assert harness["retrieve"][0]["question_ids"] == {"q-s4", "q-s5"}
