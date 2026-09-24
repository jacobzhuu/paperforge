"""Real LangGraph interruption and replay against the domain action ledger."""

import copy
import uuid
from types import SimpleNamespace

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from paperforge_worker.context import JobStopped
from paperforge_worker.orchestration.semantic_graph import run_graph, setup_graph_store
from paperforge_worker.orchestration.semantic_repair import CHECKPOINT_KEY, RepairState
from test_semantic_convergence import _inputs, _verdict


class Context:
    def __init__(self):
        self.checkpoint = {}
        self.project_id = uuid.uuid4()
        self.job_id = uuid.uuid4()
        self.events = []

    async def emit(self, event, payload, *, checkpoint=None, **_):
        self.events.append(event)
        self.checkpoint.update(copy.deepcopy(checkpoint or {}))

    async def raise_if_stopped(self):
        pass


def hooks(context, state, calls, *, passes=False, crash=False):
    async def review(items, state):
        return [
            _verdict("s1")
            if passes
            else _verdict("s1", support="thin", answers_question="no", diagnosis="evidence_gap")
        ]

    async def inputs():
        return _inputs("s1")

    async def execute(action):
        calls.append(action.kind)
        if crash:
            raise RuntimeError("lost provider response")
        return {"count": 1}

    async def save(event, payload):
        await context.emit(event, payload, checkpoint={CHECKPOINT_KEY: state.payload()})

    async def snapshot():
        return "document", "body-hash"

    return dict(
        context=context,
        state=state,
        review=review,
        inputs=inputs,
        execute=execute,
        save=save,
        snapshot=snapshot,
        note_for=lambda *_: "repair",
        max_rounds=1,
    )


async def test_human_interrupt_resume_does_not_reset_budget_or_repeat_actions():
    context, saver, calls = Context(), InMemorySaver(), []
    state = RepairState(scope="test")
    with pytest.raises(JobStopped, match="repair_needs_input"):
        await run_graph(**hooks(context, state, calls), checkpointer=saver)
    assert state.rounds_used == 1 and calls == ["deepen", "retrieve", "matrix", "rewrite"]
    context.checkpoint["repair_response"] = {
        "interrupt_id": context.checkpoint["repair_interrupt"]["id"],
        "choice": "continue",
    }
    restored = RepairState.load(context.checkpoint[CHECKPOINT_KEY], scope="test")
    result = await run_graph(**hooks(context, restored, calls, passes=True), checkpointer=saver)
    assert result["stop_reason"] == "acceptable"
    assert result["rounds_used"] == 1
    assert len(calls) == 4
    assert context.checkpoint["repair_interrupt"] is None


async def test_uncertain_action_reconciled_without_replay():
    context, saver, calls = Context(), InMemorySaver(), []
    state = RepairState(scope="test")
    with pytest.raises(RuntimeError, match="lost provider"):
        await run_graph(**hooks(context, state, calls, crash=True), checkpointer=saver)
    restored = RepairState.load(context.checkpoint[CHECKPOINT_KEY], scope="test")
    result = await run_graph(**hooks(context, restored, calls, passes=True), checkpointer=saver)
    assert calls == ["deepen"]
    assert result["rounds_used"] == 1
    assert "semantic_repair.interrupted" in context.events


async def test_postgres_checkpointer_survives_new_connection(pg_database_url):
    await setup_graph_store(pg_database_url)
    context, calls = Context(), []
    context.settings = SimpleNamespace(database_url=pg_database_url)
    state = RepairState(scope="test-pg")
    with pytest.raises(JobStopped):
        await run_graph(**hooks(context, state, calls))
    context.checkpoint["repair_response"] = {
        "interrupt_id": context.checkpoint["repair_interrupt"]["id"],
        "choice": "finish",
    }
    restored = RepairState.load(context.checkpoint[CHECKPOINT_KEY], scope="test-pg")
    result = await run_graph(**hooks(context, restored, calls))
    assert result["stop_reason"] == "user_finished"
    assert result["unresolved"] == ["s1"]
    assert len(calls) == 4
