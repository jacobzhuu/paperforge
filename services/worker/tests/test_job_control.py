"""任务的取消 / 暂停 / 断点续跑（真实 Postgres）。

一键生成实测 22 分钟起步，此前用户对它没有任何叫停手段：`cancelled` 状态在各层
枚举里躺着但全仓库无人写入，checkpoint 每阶段都写却从没有人读。这里锁住三件事：

1. 停止是**协作式**的——在阶段 / 章节边界生效，已产出的内容一件不丢，
   而且不能被 `_run_stage` 的兜底 except 当成「阶段降级」吞掉；
2. 暂停落 paused（不是 failed），取消落 cancelled，两者都发对应事件；
3. 续跑读得懂断点——已完成的阶段跳过，已写完的章节不重写。
"""

from __future__ import annotations

import uuid

import httpx
from db import (
    create_job,
    list_job_events,
    list_sections,
    request_job_stop,
    update_job,
)
from db.models.paper import GenerationJob
from paperforge_worker.config import WorkerSettings
from paperforge_worker.context import JobContext, JobStopped, job_context
from paperforge_worker.pipelines.document import (
    WRITE_DOCUMENT_KEY,
    write_document,
)
from paperforge_worker.worker import _run_stage
from scholar_gateway import InMemoryHttpCache
from test_document_persistence import _draft_writer, _latest_document, _seed


async def _seed_with_job(session_factory, *, kind: str = "write", checkpoint=None):
    project_id, cite_key = await _seed(session_factory)
    async with session_factory() as session:
        job = await create_job(session, project_id=project_id, kind=kind, checkpoint=checkpoint)
        await update_job(session, job, status="running")
        await session.commit()
        return project_id, job.id, cite_key


def _context(project_id, job_id, session_factory, *, checkpoint=None) -> JobContext:
    return JobContext(
        project_id=project_id,
        job_id=job_id,
        settings=WorkerSettings(llm_default_provider="noop"),
        session_factory=session_factory,
        http_client=httpx.Client(),
        scholar_cache=InMemoryHttpCache(),
        checkpoint=dict(checkpoint or {}),
    )


async def _stop(session_factory, job_id: uuid.UUID, mode: str) -> None:
    async with session_factory() as session:
        job = await session.get(GenerationJob, job_id)
        await request_job_stop(session, job, mode=mode)
        await session.commit()


async def _job(session_factory, job_id: uuid.UUID) -> GenerationJob:
    async with session_factory() as session:
        return await session.get(GenerationJob, job_id)


async def _event_types(session_factory, job_id: uuid.UUID) -> list[str]:
    async with session_factory() as session:
        return [event.event_type for event in await list_job_events(session, job_id)]


async def test_stage_boundary_cancel_stops_the_pipeline(session_factory) -> None:
    """阶段边界看到取消标记就停：后续阶段一个都不能再跑。"""
    project_id, job_id, _ = await _seed_with_job(session_factory, kind="full")
    await _stop(session_factory, job_id, "cancel")
    context = _context(project_id, job_id, session_factory)
    ran: list[str] = []

    async def _runner(stage: str):
        ran.append(stage)
        return {}

    try:
        for stage in ("outline", "write", "render"):
            await _run_stage(context, stage, lambda s=stage: _runner(s))
    except JobStopped as stop:
        assert stop.mode == "cancel"
    else:  # pragma: no cover - 没抛出说明停止检查根本没生效
        raise AssertionError("JobStopped was not raised at the stage boundary")

    assert ran == []


async def test_stop_is_not_swallowed_as_a_stage_degradation(session_factory) -> None:
    """停止信号不能被 `_run_stage` 的兜底 except 当成阶段失败。

    draft-first 下阶段异常一律降级并继续；停止信号如果走了那条路，
    用户点的取消就变成「这一阶段跳过，接着跑下一阶段」。
    """
    project_id, job_id, _ = await _seed_with_job(session_factory, kind="full")
    context = _context(project_id, job_id, session_factory)

    async def _stops():
        raise JobStopped("pause")

    try:
        await _run_stage(context, "write", _stops)
    except JobStopped:
        pass
    else:  # pragma: no cover
        raise AssertionError("JobStopped was swallowed by the degradation handler")

    # 降级路径没有被走过：既没有 warning，也没有 write.failed 事件。
    assert context.warnings == []
    assert "write.failed" not in await _event_types(session_factory, job_id)


async def test_pause_mid_write_keeps_written_sections_and_lands_paused(
    session_factory,
    monkeypatch,
) -> None:
    """写到一半暂停：已写完的章节留在库里，任务落 paused 而不是 failed。"""
    project_id, job_id, cite_key = await _seed_with_job(session_factory)
    # 开关轮询默认有 2 秒节流（生产上每节要跑几分钟，节流是为了别每节都打库）。
    # 测试里几节写完不到 2 秒，不关掉就永远读的是入口那次的缓存值。
    monkeypatch.setattr("paperforge_worker.context._STOP_POLL_TTL_SECONDS", 0.0)

    base = _draft_writer(cite_key)

    async def _pausing(**kwargs):
        draft = await base(**kwargs)
        # 用户在第二节写作期间点下暂停。这一节会写完并落库，之后才停。
        if str(kwargs["section"].get("key")) == "s2":
            await _stop(session_factory, job_id, "pause")
        return draft

    monkeypatch.setattr("paperforge_worker.pipelines.document.write_section", _pausing)

    async with job_context(
        project_id=project_id,
        job_id=job_id,
        settings=WorkerSettings(llm_default_provider="noop"),
        session_factory=session_factory,
    ) as context:
        await write_document(context, language="en", title="T", coherence=False)
        raise AssertionError("write_document 没有在暂停点停下")

    job = await _job(session_factory, job_id)
    assert job.status == "paused", "暂停被当成了失败"
    assert "job.paused" in await _event_types(session_factory, job_id)

    document = await _latest_document(session_factory, project_id)
    assert document is not None
    async with session_factory() as session:
        rows = await list_sections(session, document.id)
    # 停在 s2 之后：s1/s2 已落库，s3 与 abstract 还没写。暂停的意义就是这些不能白写。
    assert [row.section_key for row in rows] == ["s1", "s2"]
    assert all(row.body_ir_json for row in rows)
    # 断点也留下了：续跑要靠它找回同一份 document。
    assert job.checkpoint_json.get(WRITE_DOCUMENT_KEY) == str(document.id)


async def test_cancelled_job_is_not_rewritten_to_failed(session_factory) -> None:
    """已落终态的任务不该被中断收尾改写。

    `_write_interrupted` 会把非终态任务标成 failed。取消的任务如果不被挡住，
    用户看到的就是「我点了取消，它显示失败了」——像是出了错，而不是我停的。
    """
    project_id, job_id, _ = await _seed_with_job(session_factory)
    async with session_factory() as session:
        job = await session.get(GenerationJob, job_id)
        await update_job(session, job, status="cancelled")
        await session.commit()

    try:
        async with job_context(
            project_id=project_id,
            job_id=job_id,
            settings=WorkerSettings(llm_default_provider="noop"),
            session_factory=session_factory,
        ):
            raise RuntimeError("boom")
    except RuntimeError:
        pass

    assert (await _job(session_factory, job_id)).status == "cancelled"


async def test_resume_skips_already_completed_stages(session_factory) -> None:
    """续跑：checkpoint 里已完成的阶段不重跑，只发一条 `{stage}.skipped`。"""
    project_id, job_id, _ = await _seed_with_job(
        session_factory,
        kind="full",
        checkpoint={"outline": {"section_count": 4}},
    )
    context = _context(
        project_id, job_id, session_factory, checkpoint={"outline": {"section_count": 4}}
    )
    ran: list[str] = []

    async def _runner():
        ran.append("outline")
        return {}

    result = await _run_stage(context, "outline", _runner)

    assert ran == [], "已完成的阶段被重跑了"
    assert result is None
    assert "outline.skipped" in await _event_types(session_factory, job_id)
    # 下游要靠 checkpoint 补标量，payload 必须原样带回来。
    assert context.stage_payload("outline") == {"section_count": 4}


async def test_failed_stage_is_not_treated_as_completed(session_factory) -> None:
    """上一轮降级的阶段必须重跑：`{stage}_failed` 不是「做完了」。"""
    checkpoint = {"search": {"persisted_entry_count": 0}, "search_failed": True}
    project_id, job_id, _ = await _seed_with_job(
        session_factory, kind="full", checkpoint=checkpoint
    )
    context = _context(project_id, job_id, session_factory, checkpoint=checkpoint)
    ran: list[str] = []

    async def _runner():
        ran.append("search")
        return {}

    await _run_stage(context, "search", _runner)
    assert ran == ["search"]


async def test_resumed_write_skips_sections_already_persisted(
    session_factory,
    monkeypatch,
) -> None:
    """续跑 write：上一轮写好的章节不再调用 LLM 重写。

    这是暂停功能的成败所在——在第 6 节暂停、继续时从第 1 节重写，
    等于暂停从未发生，那十几分钟的调用要再付一遍。
    """
    project_id, job_id, cite_key = await _seed_with_job(session_factory)
    monkeypatch.setattr(
        "paperforge_worker.pipelines.document.write_section", _draft_writer(cite_key)
    )

    # 第一轮：完整写一遍。
    first = _context(project_id, job_id, session_factory)
    await write_document(first, language="en", title="T", coherence=False)
    document = await _latest_document(session_factory, project_id)
    assert document is not None
    async with session_factory() as session:
        written = {row.section_key for row in await list_sections(session, document.id)}
    assert written

    # 第二轮：带着上一轮的 document 断点续跑，写手一次都不该被调用。
    calls: list[str] = []
    base = _draft_writer(cite_key)

    async def _counting(**kwargs):
        calls.append(str(kwargs["section"].get("key")))
        return await base(**kwargs)

    monkeypatch.setattr("paperforge_worker.pipelines.document.write_section", _counting)

    resumed_ctx = _context(
        project_id,
        job_id,
        session_factory,
        checkpoint={WRITE_DOCUMENT_KEY: str(document.id)},
    )
    outcome = await write_document(resumed_ctx, language="en", title="T", coherence=False)

    assert calls == [], f"续跑重写了已完成的章节: {calls}"
    # 复用的是同一份 document，而不是又建了一版。
    assert outcome.document_id == str(document.id)
