from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from paperforge_worker import worker


class _Context:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.warnings: list[tuple[str, str, dict[str, Any]]] = []

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

    assert worker._blocker_instances(degenerated) < worker._blocker_instances(previous)
    assert worker._repair_candidate_improves(previous, degenerated) is False


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
