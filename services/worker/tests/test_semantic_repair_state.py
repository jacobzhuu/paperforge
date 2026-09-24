"""Fault injection at durable action boundaries, including real PostgreSQL resume."""

from __future__ import annotations

import json

import pytest
from db import create_job, get_job, list_job_events, resume_checkpoint
from paperforge_worker.context import JobStopped
from paperforge_worker.orchestration.semantic_repair import (
    CHECKPOINT_KEY,
    RepairState,
    execute_actions,
    plan_repairs,
)
from test_job_control import _context, _seed_with_job
from test_semantic_convergence import _inputs, _verdict


def retrieval_plan() -> RepairState:
    state = RepairState(scope="project:document")
    plan_repairs(
        state,
        [_verdict("s1", support="thin", answers_question="no", diagnosis="evidence_gap")],
        _inputs("s1"),
        lambda *_: "fix",
    )
    return state


async def no_stop():
    pass


async def test_resume_skips_completed_actions_and_inherits_budget(session_factory):
    project, job_id, _ = await _seed_with_job(session_factory)
    context = _context(project, job_id, session_factory)
    state = retrieval_plan()
    calls = []

    async def save(event, payload):
        await context.emit(event, payload, checkpoint={CHECKPOINT_KEY: state.payload()})

    async def execute(action):
        calls.append(action.kind)
        return {"count": 1}

    async def pause():
        if calls == ["deepen", "retrieve"]:
            raise JobStopped("pause")

    with pytest.raises(JobStopped):
        await execute_actions(state, execute=execute, save=save, stop_check=pause)
    async with context.session() as session:
        original = await get_job(session, job_id)
        resumed = await create_job(
            session, project_id=project, kind="write", checkpoint=resume_checkpoint(original)
        )
        seeded = resumed.checkpoint_json
        events = await list_job_events(session, job_id)
    assert [event.event_type for event in events].count("semantic_repair.action_completed") == 2
    context = _context(project, resumed.id, session_factory, checkpoint=seeded)
    state = RepairState.load(seeded[CHECKPOINT_KEY], scope="project:document")
    assert state.rounds_used == 1
    await execute_actions(state, execute=execute, save=save, stop_check=no_stop)
    assert calls == ["deepen", "retrieve", "matrix", "rewrite"]
    assert state.rounds_used == 1
    assert all(a.status == "completed" for a in state.actions)
    async with context.session() as session:
        resumed_events = await list_job_events(session, resumed.id)
        saved_job = await get_job(session, resumed.id)
    assert [event.event_type for event in resumed_events].count(
        "semantic_repair.action_completed"
    ) == 2
    assert saved_job.checkpoint_json[CHECKPOINT_KEY]["rounds_used"] == 1


async def test_crash_after_external_effect_never_blindly_replays_or_runs_dependents():
    state = retrieval_plan()
    persisted = {}
    calls = []

    async def save(event, payload):
        persisted.update(json.loads(json.dumps(state.payload())))

    async def crash(action):
        calls.append(action.kind)
        raise RuntimeError("worker died after external request")

    with pytest.raises(RuntimeError):
        await execute_actions(state, execute=crash, save=save, stop_check=no_stop)
    state = RepairState.load(persisted, scope=state.scope)
    assert not await execute_actions(state, execute=crash, save=save, stop_check=no_stop)
    assert calls == ["deepen"]
    assert state.rounds_used == 1
    assert all(a.status == "interrupted" for a in state.actions)
    state = RepairState.load(persisted, scope=state.scope)
    await execute_actions(state, execute=crash, save=save, stop_check=no_stop)
    assert calls == ["deepen"]


def test_state_is_scoped_and_old_checkpoints_start_empty():
    state = retrieval_plan()
    assert RepairState.load({}, scope=state.scope).rounds_used == 0
    assert RepairState.load(state.payload(), scope="other:document").rounds_used == 0
    assert RepairState.load(state.payload(), scope="project:new-document").rounds_used == 0


def test_new_rewrite_instruction_can_replan_but_identical_instruction_cannot():
    state = RepairState(scope="p:d")
    verdict = _verdict("s1", calibration="overclaimed", diagnosis="writing_gap")
    assert plan_repairs(state, [verdict], _inputs("s1"), lambda *_: "sentence A") == {
        "s1": "rewrite"
    }
    state = RepairState.load(json.loads(json.dumps(state.payload())), scope=state.scope)
    assert plan_repairs(state, [verdict], _inputs("s1"), lambda *_: "sentence A") == {}
    assert plan_repairs(state, [verdict], _inputs("s1"), lambda *_: "sentence B") == {
        "s1": "rewrite"
    }
