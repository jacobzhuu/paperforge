"""Shadow writes cannot influence delivery; snapshot/repeat/budget isolation contracts."""

import json
import uuid
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from db import (
    WRITE_DOCUMENT_KEY,
    create_document,
    create_job,
    create_project,
    create_user,
    document_snapshot_hash,
    upsert_section,
)
from db.models.paper import EvaluatorShadowRun, GenerationJob, PaperSection, QualityReportRecord
from llm_runtime.runner import JsonResult, LLMCallRecord
from paperforge_worker import evaluator_shadow as shadow
from paperforge_worker.config import WorkerSettings
from paperforge_worker.pipelines.review_inputs import build_review_input
from sqlalchemy import select

from evals.evaluator_shadow.report import summarize


def material():
    return build_review_input(
        section_key="s1",
        question="What is known?",
        prose="Evidence is missing.",
        body={
            "blocks": [{"type": "paragraph", "runs": [{"t": "text", "v": "Evidence is missing."}]}]
        },
        evidence=[],
        whitelist={},
    )


async def seed(factory):
    async with factory() as session:
        user = await create_user(
            session, email=f"{uuid.uuid4()}@test.invalid", password_hash="!test"
        )
        project = await create_project(
            session, title="Shadow", paper_type="review", owner_id=user.id
        )
        doc = await create_document(session, project_id=project.id, outline_id=None)
        section = await upsert_section(
            session,
            document_id=doc.id,
            section_key="s1",
            title="Evidence",
            order_no=1,
            body_ir={
                "blocks": [
                    {"type": "paragraph", "runs": [{"t": "text", "v": "Evidence is missing."}]}
                ]
            },
            cite_keys=[],
        )
        job = await create_job(
            session,
            project_id=project.id,
            kind="write",
            checkpoint={WRITE_DOCUMENT_KEY: str(doc.id)},
        )
        job.status = "succeeded"
        report = QualityReportRecord(
            project_id=project.id,
            document_id=doc.id,
            document_version=1,
            paper_snapshot_hash=document_snapshot_hash([section]),
            quality_profile="scholarly",
            review_style="narrative",
            readiness_status="needs_revision",
            stale=False,
            blockers_json=[{"code": "existing_hard_violation"}],
        )
        session.add(report)
        await session.commit()
        return job.id, doc.id, section.id, report.id


async def foreground_state(factory, ids):
    async with factory() as session:
        rows = [
            await session.get(cls, key)
            for cls, key in zip(
                [GenerationJob, PaperSection, QualityReportRecord],
                [ids[0], ids[2], ids[3]],
                strict=True,
            )
        ]
        return [{c.name: getattr(row, c.name) for c in row.__table__.columns} for row in rows]


class Runner:
    enabled = True

    def __init__(self):
        self.calls = 0

    async def agenerate_json(self, *args, **kwargs):
        self.calls += 1
        data = json.loads(kwargs["user_prompt"].split("\n", 1)[1])
        return JsonResult(
            value={
                "answers_question": "partial",
                "support": "thin",
                "calibration": "matched",
                "synthesis_mode": "mixed",
                "diagnosis": "evidence_gap",
                "gap_declared": True,
                "unsupported_claims": [],
                "rationale": "An honest gap.",
                "claim_checks": [
                    {
                        "claim_id": c["claim_id"],
                        "status": "gap",
                        "evidence_ids": [],
                        "reason": "Missing evidence stated explicitly.",
                    }
                    for c in data["claims"]
                ],
            },
            raw_text="raw response",
        )


async def test_background_repeats_freeze_input_and_never_mutate_online_records(
    session_factory, monkeypatch
):
    ids = await seed(session_factory)
    settings = WorkerSettings()
    ctx = {"settings": settings, "session_factory": session_factory, "job_id": str(ids[0])}
    before = await foreground_state(session_factory, ids)
    await shadow.after_job_end(ctx)
    await shadow.after_job_end(ctx)
    async with session_factory() as session:
        runs = list((await session.scalars(select(EvaluatorShadowRun))).all())
        assert len(runs) == 1
        frozen = deepcopy(runs[0].input_json)
        assert frozen["baseline_comparable"]
    runner = Runner()
    monkeypatch.setattr(shadow.ShadowContext, "llm_runner", lambda self: runner)
    await shadow.shadow_tick(ctx)
    await shadow.shadow_tick(ctx)
    assert runner.calls == 3, "completed repeats must not execute twice"
    assert await foreground_state(session_factory, ids) == before
    async with session_factory() as session:
        row = await session.get(EvaluatorShadowRun, runs[0].id)
        assert row.status == "completed"
        assert row.input_json == frozen
        assert len({r["input_hash"] for r in row.results_json}) == 1
        assert {r["repeat"] for r in row.results_json} == {1, 2, 3}
        assert all(r["status"] == "assessed" for r in row.results_json)
    assert settings.writer_execution_mode == "legacy"


async def test_disabled_failed_capture_and_cohort_cap_are_not_delivery_failures(
    session_factory, monkeypatch
):
    ids = await seed(session_factory)
    ctx = {
        "settings": WorkerSettings(evaluator_shadow_document_limit=0),
        "session_factory": session_factory,
        "job_id": str(ids[0]),
    }
    before = await foreground_state(session_factory, ids)
    await shadow.after_job_end(ctx)
    async with session_factory() as session:
        assert not list((await session.scalars(select(EvaluatorShadowRun))).all())

    async def broken(*args):
        raise OSError("shadow storage down")

    monkeypatch.setattr(shadow, "capture", broken)
    await shadow.after_job_end(ctx)
    ctx["settings"].evaluator_shadow_enabled = False
    await shadow.after_job_end(ctx)
    assert await foreground_state(session_factory, ids) == before


async def test_foreground_priority_and_interruption_do_not_replay_paid_work(
    session_factory, monkeypatch
):
    ids = await seed(session_factory)
    ctx = {"settings": WorkerSettings(), "session_factory": session_factory, "job_id": str(ids[0])}
    await shadow.after_job_end(ctx)
    runner = Runner()
    monkeypatch.setattr(shadow.ShadowContext, "llm_runner", lambda self: runner)
    async with session_factory() as session:
        job = await session.get(GenerationJob, ids[0])
        job.status = "running"
        await session.commit()
    await shadow.shadow_tick(ctx)
    assert runner.calls == 0
    async with session_factory() as session:
        job = await session.get(GenerationJob, ids[0])
        job.status = "succeeded"
        run = await session.scalar(select(EvaluatorShadowRun))
        run.status = "running"
        run.updated_at = datetime.now(UTC) - timedelta(hours=1)
        await session.commit()
    await shadow.shadow_tick(ctx)
    await shadow.shadow_tick(ctx)
    assert runner.calls == 0
    async with session_factory() as session:
        run = await session.scalar(select(EvaluatorShadowRun))
        assert run.status == "interrupted"


async def test_shadow_trace_and_costs_use_artifacts_only(tmp_path):
    import httpx
    from storage import make_object_store

    ctx = shadow.ShadowContext(
        project_id=uuid.uuid4(),
        job_id=None,
        settings=WorkerSettings(storage_fs_root=str(tmp_path)),
        session_factory=None,
        http_client=httpx.Client(),
        scholar_cache=None,
    )
    ctx.store, ctx.prefix, ctx.events = make_object_store(ctx.settings), "shadow/test", []
    try:
        await ctx.emit("raw", {"error": "invalid_json"}, checkpoint={"agent_budget": {"calls": 1}})
        await ctx._save_call(LLMCallRecord(role="section_reviewer", model="mock", provider="noop"))
        assert ctx.checkpoint["agent_budget"]["calls"] == 1
        assert len(list(tmp_path.rglob("*.json"))) == 2
    finally:
        ctx.http_client.close()


def test_deterministic_error_survives_llm_clearance():
    m = material()
    m["binding_issues"] = [{"claim_id": "s1:0:0:0", "reason": "unknown_citation"}]
    verdict = SimpleNamespace(acceptable=True, to_payload=lambda: {"acceptable": True})
    row = shadow.result_row(
        m,
        1,
        verdict,
        [
            {
                "event": "review.result",
                "payload": {"status": "assessed", "raw_judgment": {"claim_checks": []}},
            }
        ],
    )
    assert row["shadow_has_violation"]
    assert row["deterministic"][0]["reason"] == "unknown_citation"


def synthetic_run(status="assessed"):
    m = material()
    results = [
        {
            "input_hash": m["input_hash"],
            "section_key": "s1",
            "repeat": r,
            "status": status,
            "source_ambiguity": False,
            "deterministic": [],
            "llm": {"claim_checks": [{"claim_id": m["claims"][0]["claim_id"], "status": "gap"}]},
        }
        for r in [1, 2, 3]
    ]
    return {
        "id": "run",
        "project_id": "p",
        "status": "completed",
        "input_json": {"document_id": "d", "snapshot": "frozen", "sections": [m], "repeats": 3},
        "results_json": results,
    }


def test_no_gold_no_data_and_repeated_failures_never_pass_trust_gate():
    assert not summarize([])["recommend_abc_review"]
    report = summarize([synthetic_run()])
    assert report["repeat_consistency"] == 1
    assert report["confusions"]["llm"]["fp_rate"] is None
    assert not report["recommend_abc_review"]
    report = summarize([synthetic_run("unassessed")])
    assert report["repeat_consistency"] == 0
    assert report["failure_rate"] == 1


def test_fp_fn_uses_independent_labels_and_worst_repeat_not_online_gate():
    run = synthetic_run()
    m = run["input_json"]["sections"][0]
    gold = {
        "input_hash": m["input_hash"],
        "claim_id": m["claims"][0]["claim_id"],
        "layer": "llm",
        "label": "violation",
        "reviewer": "independent-ai",
        "rationale": "Frozen source contradicts it.",
        "source_quote": "Evidence is missing.",
    }
    report = summarize([run], [gold])
    assert report["confusions"]["llm"]["fn"] == 1, "three repeats are one gold claim"
    with pytest.raises(ValueError, match="duplicate"):
        summarize([run], [gold, gold])
    gold["source_quote"] = "fabricated quotation"
    with pytest.raises(ValueError, match="verified"):
        summarize([run], [gold])


async def test_provider_failure_is_retained_and_cannot_change_online_gate(
    session_factory, monkeypatch
):
    ids = await seed(session_factory)
    ctx = {"settings": WorkerSettings(), "session_factory": session_factory, "job_id": str(ids[0])}
    await shadow.after_job_end(ctx)
    before = await foreground_state(session_factory, ids)

    class BrokenRunner(Runner):
        async def agenerate_json(self, *args, **kwargs):
            self.calls += 1
            raise RuntimeError("provider failure")

    runner = BrokenRunner()
    monkeypatch.setattr(shadow.ShadowContext, "llm_runner", lambda self: runner)
    with pytest.raises(RuntimeError):
        await shadow.shadow_tick(ctx)
    await shadow.shadow_tick(ctx)
    assert runner.calls == 1
    assert await foreground_state(session_factory, ids) == before
    async with session_factory() as session:
        run = await session.scalar(select(EvaluatorShadowRun))
        assert run.status == "failed"
        assert run.results_json[0]["status"] == "unassessed"
        assert run.results_json[0]["reason"] == "RuntimeError"


async def test_foreground_arrival_pauses_and_resumes_only_unfinished_repeats(
    session_factory, monkeypatch
):
    ids = await seed(session_factory)
    ctx = {"settings": WorkerSettings(), "session_factory": session_factory, "job_id": str(ids[0])}
    await shadow.after_job_end(ctx)
    runner = Runner()
    monkeypatch.setattr(shadow.ShadowContext, "llm_runner", lambda self: runner)
    checks = 0

    async def busy(_factory):
        nonlocal checks
        checks += 1
        return checks == 3  # first tick + first call allowed; second call yields

    monkeypatch.setattr(shadow, "foreground_busy", busy)
    await shadow.shadow_tick(ctx)
    assert runner.calls == 1
    async with session_factory() as session:
        run = await session.scalar(select(EvaluatorShadowRun))
        assert run.status == "pending"
        section = await session.get(PaperSection, ids[2])
        section.body_ir_json = {"blocks": []}
        await session.commit()
    await shadow.shadow_tick(ctx)
    assert runner.calls == 3
    async with session_factory() as session:
        run = await session.scalar(select(EvaluatorShadowRun))
        assert run.status == "completed"
        assert len(run.input_json["sections"][0]["claims"]) == 1
        assert len(run.results_json) == 3


def test_unknown_gold_is_not_reported_as_measured_false_positive():
    run = synthetic_run("unassessed")
    m = run["input_json"]["sections"][0]
    label = {
        "input_hash": m["input_hash"],
        "claim_id": m["claims"][0]["claim_id"],
        "layer": "llm",
        "label": "clean",
        "reviewer": "reviewer",
        "rationale": "Honest gap.",
        "source_quote": "Evidence is missing.",
    }
    counts = summarize([run], [label])["confusions"]["llm"]
    assert counts["fp"] == 0 and counts["fp_rate"] is None
    assert counts["unknown_negative"] == 1
    assert counts["worst_case_fp_rate"] == 1


def test_placeholder_and_declared_gap_remain_separate_in_shadow():
    m = material()
    m["claims"][0]["text"] = "相关数值待补充，因此不做排序。"
    assert shadow.deterministic_checks(m) == []
    m["claims"][0]["text"] = "[待补证据]"
    assert shadow.deterministic_checks(m)[0]["reason"] == "placeholder"


def test_invalid_claim_check_shape_is_retained_without_breaking_shadow_report():
    m = material()
    row = shadow.result_row(
        m,
        1,
        None,
        [
            {
                "event": "review.result",
                "payload": {
                    "status": "unassessed",
                    "reason": "incomplete_claim_coverage",
                    "raw_judgment": {"claim_checks": None},
                },
            }
        ],
    )
    assert row["llm"]["claim_checks"] is None
    run = synthetic_run()
    run["results_json"] = [row]
    report = summarize([run])
    assert report["failure_rate"] == 1
    assert not report["recommend_abc_review"]


def test_trust_gate_requires_gold_and_never_automatically_launches_even_when_satisfied():
    from paperforge_worker.orchestration.writing_graph import fingerprint

    runs, gold = [], []
    for doc in range(10):
        run = synthetic_run()
        run.update(id=str(doc), project_id=str(doc % 3))
        run["input_json"].update(document_id=str(doc), snapshot=str(doc), sections=[])
        run["results_json"] = []
        for section in range(2):
            m = material()
            m["section_key"] = f"s{section}"
            m["claims"] = [
                {"claim_id": f"{section}:{n}", "text": f"Frozen claim {doc}/{section}/{n}"}
                for n in range(5)
            ]
            m["input_hash"] = fingerprint(m)
            run["input_json"]["sections"].append(m)
            llm_checks = [
                {
                    "claim_id": c["claim_id"],
                    "status": "contradicted" if (n + section) % 2 else "supported",
                }
                for n, c in enumerate(m["claims"])
            ]
            det_id = m["claims"][0]["claim_id"]
            for repeat in (1, 2, 3):
                run["results_json"].append(
                    {
                        "section_key": m["section_key"],
                        "repeat": repeat,
                        "input_hash": m["input_hash"],
                        "status": "assessed",
                        "source_ambiguity": False,
                        "llm": {"claim_checks": llm_checks},
                        "deterministic": [{"claim_id": det_id, "reason": "binding_error"}],
                    }
                )
            for c, check in zip(m["claims"], llm_checks, strict=True):
                label = {
                    "input_hash": m["input_hash"],
                    "claim_id": c["claim_id"],
                    "layer": "llm",
                    "label": "violation" if check["status"] == "contradicted" else "clean",
                    "reviewer": "fixture-only",
                    "rationale": "Synthetic oracle for gate test.",
                    "source_quote": c["text"],
                }
                gold.append(label)
                if c["claim_id"] == det_id:
                    gold.append({**label, "layer": "deterministic", "label": "violation"})
        runs.append(run)
    report = summarize(runs, gold)
    assert report["recommend_abc_review"], report["conditions"]
    assert not report["automatic_actions"]
    assert not summarize(runs)["recommend_abc_review"]
    runs[0]["results_json"][0]["deterministic"] = []
    report = summarize(runs, gold)
    assert report["confusions"]["deterministic"]["fn"] == 1
    assert not report["recommend_abc_review"]
