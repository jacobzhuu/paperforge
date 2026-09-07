from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from paperforge_worker import worker


@pytest.fixture(autouse=True)
def _no_anchor_lookups(monkeypatch):
    """这一组测试断言的是收敛**控制流**，不是锚点计数。

    真实的 `_unresolved_anchor_counts` 要连库、并把 report_id 当 UUID 解析，而这里的
    报告是 SimpleNamespace、id 是 "worse-1" 之类的字符串。故意**不**放宽那边的解析：
    一个解析不了的 report_id 在生产里是真的出问题了，不该被静默吞掉。
    """

    async def _none(_context, _report):
        return {}

    monkeypatch.setattr(worker, "_unresolved_anchor_counts", _none)


class _Context:
    def __init__(self, *, quality_repair_rounds: int = 2) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.warnings: list[tuple[str, str, dict[str, Any]]] = []
        # 轮数现在来自部署配置（QUALITY_REPAIR_ROUNDS）；默认值就是原先写死的 2，
        # 所以这些用例断言的行为不变。
        self.settings = SimpleNamespace(quality_repair_rounds=quality_repair_rounds)

    async def emit(self, event: str, payload: dict[str, Any], **_kwargs: Any) -> None:
        self.events.append((event, payload))

    def warn(self, stage: str, reason: str, detail: dict[str, Any]) -> None:
        self.warnings.append((stage, reason, detail))


def _report(blocker_count: int, *, ready: bool = False, report_id: str = "report"):
    return SimpleNamespace(
        report_id=report_id,
        readiness_status="preflight_ready" if ready else "needs_revision",
        blockers=[
            {"code": "original_claim_source_missing", "count": 1} for _ in range(blocker_count)
        ],
    )


async def test_quality_convergence_stops_as_soon_as_the_gate_passes(monkeypatch) -> None:
    context = _Context()
    repairs: list[set[str]] = []
    restored: list[dict[str, Any]] = []

    async def failing_sections(_context, _report):
        return {"s4"}

    async def snapshot(_context):
        return {"s4": "best"}

    async def repair(_context, *, section_keys, **_kwargs):
        repairs.append(section_keys)

    async def restore(_context, value):
        restored.append(value)

    async def quality(_context, **_kwargs):
        return _report(0, ready=True, report_id="passed")

    monkeypatch.setattr(worker, "_quality_failing_sections", failing_sections)
    monkeypatch.setattr(worker, "snapshot_document_sections", snapshot)
    monkeypatch.setattr(worker, "repair_document_sections", repair)
    monkeypatch.setattr(worker, "restore_document_sections", restore)
    monkeypatch.setattr(worker, "_quality", quality)

    final, history = await worker._converge_scholarly_quality(
        context,
        initial_report=_report(2),
        language="en",
        paper_type="original",
        review_style="narrative",
    )

    assert final.readiness_status == "preflight_ready"
    assert len(repairs) == 1
    assert restored == []
    assert history == [
        {
            "attempt": 1,
            "before": 2,
            "after": 0,
            "accepted": True,
            "rolled_back": [],
            "regressed_kept": [],
            "sections": ["s4"],
            "report_id": "passed",
            "readiness_status": "preflight_ready",
        }
    ]


async def test_quality_convergence_rolls_back_worse_drafts_and_is_bounded(monkeypatch) -> None:
    """两轮上限、每轮不改善就回滚，且回滚不再重跑一次全文评估。

    每轮只评估**候选稿**一次。回滚之后的那份结论已经算过了（就是 `current`），
    重新落库即可——此前这里会再调一次 `_quality`，即每个被拒的轮次多花一次
    完整的全文 LLM 评估，最坏一轮任务多出两次。
    """
    context = _Context()
    repairs = 0
    restores = 0
    republished: list[str] = []
    quality_results = iter(
        [
            _report(2, report_id="worse-1"),
            _report(3, report_id="worse-2"),
        ]
    )

    async def failing_sections(_context, _report):
        return {"s2"}

    async def snapshot(_context):
        return {"s2": "best"}

    async def repair(_context, **_kwargs):
        nonlocal repairs
        repairs += 1

    async def restore(_context, _snapshot):
        nonlocal restores
        restores += 1

    async def quality(_context, **_kwargs):
        return next(quality_results)

    async def republish(_context, report, **_kwargs):
        republished.append(report.report_id)
        return report

    monkeypatch.setattr(worker, "_quality_failing_sections", failing_sections)
    monkeypatch.setattr(worker, "snapshot_document_sections", snapshot)
    monkeypatch.setattr(worker, "repair_document_sections", repair)
    monkeypatch.setattr(worker, "restore_document_sections", restore)
    monkeypatch.setattr(worker, "_quality", quality)
    monkeypatch.setattr(worker, "_republish_after_rollback", republish)

    final, history = await worker._converge_scholarly_quality(
        context,
        initial_report=_report(1, report_id="initial"),
        language="en",
        paper_type="original",
        review_style="narrative",
    )

    assert repairs == 2
    assert restores == 2
    # 关键的成本断言：迭代器只备了两份候选评估，跑完还没被取空就说明
    # 每轮只评估了候选稿一次。取空会 StopIteration，测试直接报错。
    assert next(quality_results, "exhausted") == "exhausted"
    assert republished == ["initial", "initial"]
    assert len(history) == 2
    assert all(item["accepted"] is False for item in history)
    # 两轮都被拒，最终交回去的必须是那份从没被改动过的最佳稿。
    assert final.report_id == "initial"
    assert worker._blocker_instances(final) == 1


async def test_rollback_falls_back_to_a_real_reassessment_when_restore_is_incomplete(
    monkeypatch,
) -> None:
    """快照哈希对不上就不能走捷径——那说明回滚没有完全复原正文。

    捷径的前提是「正文逐字回到了 current 评估时的状态」。前提不成立时把
    current 重新落库，等于给一份不是它描述的稿子挂上它的结论。
    """
    context = _Context()

    async def failing_sections(_context, _report):
        return {"s2"}

    async def snapshot(_context):
        return {"s2": "best"}

    async def repair(_context, **_kwargs):
        return None

    async def restore(_context, _snapshot):
        return None

    quality_results = iter(
        [
            _report(5, report_id="worse-1"),
            _report(2, report_id="reassessed-1"),
            _report(5, report_id="worse-2"),
            _report(2, report_id="reassessed-2"),
        ]
    )
    refusals = 0

    async def quality(_context, **_kwargs):
        return next(quality_results)

    async def republish_refuses(_context, _report, **_kwargs):
        nonlocal refusals
        refusals += 1
        return None

    monkeypatch.setattr(worker, "_quality_failing_sections", failing_sections)
    monkeypatch.setattr(worker, "snapshot_document_sections", snapshot)
    monkeypatch.setattr(worker, "repair_document_sections", repair)
    monkeypatch.setattr(worker, "restore_document_sections", restore)
    monkeypatch.setattr(worker, "_quality", quality)
    monkeypatch.setattr(worker, "_republish_after_rollback", republish_refuses)

    final, history = await worker._converge_scholarly_quality(
        context,
        initial_report=_report(2, report_id="initial"),
        language="en",
        paper_type="original",
        review_style="narrative",
    )

    assert refusals == 2
    assert final.report_id == "reassessed-2"
    assert all(item["accepted"] is False for item in history)


def test_repair_candidate_cannot_improve_by_deleting_the_review_core() -> None:
    previous = SimpleNamespace(
        readiness_status="needs_revision",
        blockers=[{"code": "core_claim_fulltext_missing", "count": 5}],
    )
    degenerated = SimpleNamespace(
        readiness_status="needs_revision",
        blockers=[
            {"code": "supported_core_claims_missing", "count": 1},
            {"code": "review_source_diversity_low", "count": 1},
        ],
    )

    # 计数确实变小了——正是这一点让「数目标函数」这条路单独不够用：把综述核心删掉
    # 也能让计数变小。退化码的判定必须独立于计数，所以它拆成了自己的谓词。
    assert worker._blocker_instances(degenerated) < worker._blocker_instances(previous)
    assert worker._introduces_degeneration(previous, degenerated) is True


def test_improvement_counts_the_anchors_that_triggered_the_repair() -> None:
    """修好锚点必须算改善——否则修复轮是在为一个它动不了的计数付钱。

    生产实测（项目 aaa9a5b1，2026-09-06）：可计数的发现项恒为 1，而触发重写的是 56 条
    未定位锚点。只数发现项时 `1 < 1` 永远为假，两轮 29 次重写全部回滚，37 分钟白跑。
    """
    previous = SimpleNamespace(
        readiness_status="needs_revision",
        blockers=[{"code": "original_claim_source_missing", "count": 1}],
    )
    candidate = SimpleNamespace(
        readiness_status="needs_revision",
        blockers=[{"code": "original_claim_source_missing", "count": 1}],
    )

    # 发现项没变，只数它就看不见任何进展——这正是旧行为。
    assert worker._repair_candidate_improves(previous, candidate) is False
    # 锚点从 56 降到 36：这一轮确实修了东西，必须判为改善。
    assert (
        worker._repair_candidate_improves(
            previous, candidate, previous_unresolved=56, candidate_unresolved=36
        )
        is True
    )
    # 锚点变多则不是改善。
    assert (
        worker._repair_candidate_improves(
            previous, candidate, previous_unresolved=36, candidate_unresolved=56
        )
        is False
    )


async def test_only_the_worsened_sections_are_rolled_back(monkeypatch) -> None:
    """按节结算：改善和持平的留下，只有变差的退回。

    旧行为是全有全无——9 个章节里 8 个改好了、1 个没改好，整轮一起退回。
    """
    context = _Context()
    restored: list[dict[str, Any]] = []

    async def current_state(_context):
        return {"s1": "rewritten", "s2": "rewritten", "s3": "rewritten"}

    async def restore(_context, value):
        restored.append(value)

    monkeypatch.setattr(worker, "snapshot_document_sections", current_state)
    monkeypatch.setattr(worker, "restore_document_sections", restore)

    rolled = await worker._roll_back_worsened_sections(
        context,
        snapshot={"s1": "old-1", "s2": "old-2", "s3": "old-3"},
        before={"s1": 5, "s2": 5, "s3": 5},
        after={"s1": 2, "s2": 5, "s3": 9},   # 改善 / 持平 / 变差
    )

    assert rolled == ["s3"]
    # 传给 restore 的必须是**完整**快照：restore_document_sections 会删掉快照里没有的
    # 章节，过滤过的快照会把要保留的 s1/s2 一起删掉。
    assert restored == [{"s1": "rewritten", "s2": "rewritten", "s3": "old-3"}]


async def test_a_section_lost_by_the_rewrite_is_restored_not_dropped(monkeypatch) -> None:
    """重写把某一节整个弄丢时，它不在「现状」里，也就不会被判为变差——必须补回来。"""
    context = _Context()
    restored: list[dict[str, Any]] = []

    async def current_state(_context):
        return {"s1": "rewritten"}   # s2 没了

    async def restore(_context, value):
        restored.append(value)

    monkeypatch.setattr(worker, "snapshot_document_sections", current_state)
    monkeypatch.setattr(worker, "restore_document_sections", restore)

    await worker._roll_back_worsened_sections(
        context,
        snapshot={"s1": "old-1", "s2": "old-2"},
        before={"s1": 5},
        after={"s1": 9},
    )

    assert restored == [{"s1": "old-1", "s2": "old-2"}]


async def test_a_rejected_candidate_is_never_left_live_when_no_section_regressed(
    monkeypatch,
) -> None:
    """按节回滚是优化，不是新的安全边界。

    退步来自**报告级**阻断项时，没有任何单一章节的锚点数会变差，于是按节路径算出
    「一节都不用退」。若就此收手，一份已经判定为更差的稿子会留在库里——这正是引入
    按节回滚时差点放进去的回归。不变量是：accepted=False 就绝不能留在库里。
    """
    context = _Context(quality_repair_rounds=1)   # 只测一轮的结算，不测收敛
    restores: list[dict[str, Any]] = []

    async def failing_sections(_context, _report):
        return {"s1"}

    async def snapshot(_context):
        return {"s1": "old-1"}

    async def repair(_context, **_kwargs):
        return None

    async def restore(_context, value):
        restores.append(value)

    # 候选的阻断项**变多**（1 → 3），而锚点计数两边都空：归因不到任何章节。
    quality_results = iter([_report(3, report_id="worse")])

    async def quality(_context, **_kwargs):
        return next(quality_results)

    async def republish(_context, report, **_kwargs):
        return report

    monkeypatch.setattr(worker, "_quality_failing_sections", failing_sections)
    monkeypatch.setattr(worker, "snapshot_document_sections", snapshot)
    monkeypatch.setattr(worker, "repair_document_sections", repair)
    monkeypatch.setattr(worker, "restore_document_sections", restore)
    monkeypatch.setattr(worker, "_quality", quality)
    monkeypatch.setattr(worker, "_republish_after_rollback", republish)

    _final, history = await worker._converge_scholarly_quality(
        context,
        initial_report=_report(1, report_id="initial"),
        language="en",
        paper_type="original",
        review_style="narrative",
    )

    assert history[0]["accepted"] is False
    assert restores, "被拒的候选必须回滚，哪怕没有一个章节的锚点计数变差"
    assert history[0]["rolled_back"] == ["s1"]


def test_manual_confirmation_only_skips_repair_when_the_source_is_located() -> None:
    base = {
        "source_kind": "fulltext",
        "source_page": None,
        "source_section": "Results",
        "source_paragraph": None,
        "grade_ok": True,
        "comparability_ok": True,
        "support_status": "insufficient_support",
        "manual_status": "confirmed",
    }
    assert worker._claim_anchor_requires_repair(SimpleNamespace(**base)) is False
    assert (
        worker._claim_anchor_requires_repair(SimpleNamespace(**{**base, "source_section": None}))
        is True
    )


class _SessionContext:
    """最小的 `context.session()` 替身：只需要能进出 async with。"""

    def __init__(self, session: Any) -> None:
        self._session = session

    async def __aenter__(self) -> Any:
        return self._session

    async def __aexit__(self, *_exc: Any) -> bool:
        return False


class _DbContext(_Context):
    def __init__(self) -> None:
        super().__init__()
        self.project_id = "project"

    def session(self) -> _SessionContext:
        return _SessionContext(object())


async def test_republish_after_rollback_reuses_the_existing_verdict(monkeypatch) -> None:
    """回滚后的正文与 current 对得上时，把 current 重新落库，不重新评估。

    落库不是可省的一步：create_quality_report 写候选报告时把 current 那一行置了
    stale，所以此刻库里 live 的是一份描述着已被回滚掉的稿子的结论。不重新发布，
    导出与界面就会照着它走。
    """
    import db

    published: list[dict[str, Any]] = []

    async def latest_document(_session, _project_id):
        return SimpleNamespace(id="doc", version=3)

    async def list_sections(_session, _document_id):
        return [SimpleNamespace(section_key="s2")]

    def snapshot_hash(_rows):
        return "hash-of-restored-body"

    async def publish(_context, report, **kwargs):
        published.append({"report_id": report.report_id, **kwargs})
        report.report_id = "republished"

    monkeypatch.setattr(db, "latest_document", latest_document)
    monkeypatch.setattr(db, "list_sections", list_sections)
    monkeypatch.setattr(db, "document_snapshot_hash", snapshot_hash)
    monkeypatch.setattr(worker, "_publish_quality_report", publish)

    report = _report(1, report_id="best-so-far")
    report.paper_snapshot_hash = "hash-of-restored-body"

    result = await worker._republish_after_rollback(
        _DbContext(),
        report,
        quality_profile="scholarly",
        review_style="narrative",
    )

    assert result is report
    # 新行的 id 必须回写，否则后续按 report_id 取论断锚点会落到那份 stale 的行上。
    assert result.report_id == "republished"
    assert published == [
        {
            "report_id": "best-so-far",
            "document_id": "doc",
            "document_version": 3,
            "quality_profile": "scholarly",
            "review_style": "narrative",
        }
    ]


async def test_republish_after_rollback_refuses_when_the_body_does_not_match(monkeypatch) -> None:
    """快照哈希对不上就什么都不写，交回 None 让调用方真正重新评估。"""
    import db

    published: list[Any] = []

    async def latest_document(_session, _project_id):
        return SimpleNamespace(id="doc", version=3)

    async def list_sections(_session, _document_id):
        return [SimpleNamespace(section_key="s2")]

    def snapshot_hash(_rows):
        return "some-other-body"

    async def publish(_context, report, **_kwargs):
        published.append(report)

    monkeypatch.setattr(db, "latest_document", latest_document)
    monkeypatch.setattr(db, "list_sections", list_sections)
    monkeypatch.setattr(db, "document_snapshot_hash", snapshot_hash)
    monkeypatch.setattr(worker, "_publish_quality_report", publish)

    report = _report(1, report_id="best-so-far")
    report.paper_snapshot_hash = "hash-of-restored-body"

    result = await worker._republish_after_rollback(
        _DbContext(),
        report,
        quality_profile="scholarly",
        review_style="narrative",
    )

    assert result is None
    assert published == []
    assert report.report_id == "best-so-far"
