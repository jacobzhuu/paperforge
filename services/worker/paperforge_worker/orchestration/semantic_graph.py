"""Versioned LangGraph repair subflow over existing domain actions and execution fences.

Graph state carries only control data. Domain checkpoints are reconciled on every node;
uncertain external actions are assessed rather than replayed. ARQ still owns dispatch.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from paperforge_worker.orchestration.semantic_repair import (
    CHECKPOINT_KEY,
    RepairState,
    execute_actions,
    plan_repairs,
)

GRAPH_VERSION = "semantic-graph-v1"


class GraphState(TypedDict, total=False):
    route: str
    awaiting_id: str
    responded: bool
    review_only: bool


def build_graph(
    *,
    context,
    state: RepairState,
    review,
    inputs,
    execute,
    note_for,
    save,
    snapshot,
    checkpointer,
    max_rounds: int = 2,
):
    """Callbacks bind existing domain logic; graph nodes do not select new models/tools."""
    current_inputs: list[dict[str, Any]] = []
    failing: list[Any] = []

    async def assess(graph_state):
        nonlocal current_inputs, failing
        await context.raise_if_stopped()
        # Domain state is the authority when a node committed before its graph checkpoint.
        raw = context.checkpoint.get(CHECKPOINT_KEY)
        if raw and raw.get("scope") == state.scope:
            loaded = RepairState.load(raw, scope=state.scope)
            state.__dict__.update(loaded.__dict__)
        if any(a.status in {"pending", "started"} for a in state.actions):
            await execute_actions(
                state, execute=execute, save=save, stop_check=context.raise_if_stopped
            )
        current_inputs = await inputs()
        state.unassessed = []
        verdicts = await review(current_inputs, state)
        state.verdicts = {v.section_key: v.to_payload() for v in verdicts}
        failing = [v for v in verdicts if not v.acceptable]
        if not current_inputs:
            state.stop_reason = "no_sections"
        elif state.unassessed:
            state.stop_reason = "reviewer_unavailable"
        elif not failing:
            state.stop_reason = "acceptable"
        else:
            state.stop_reason = None
        await save("semantic_repair.assessed", {"unassessed": len(state.unassessed)})
        if state.stop_reason:
            return {"route": "finish"}
        if graph_state.get("review_only") or state.rounds_used >= max_rounds:
            state.stop_reason = "budget_exhausted"
            return {"route": "finish" if graph_state.get("responded") else "human"}
        return {"route": "plan"}

    async def plan(graph_state):
        # On a graph-node retry, rebuild transient inputs; do not reserve another round.
        if any(a.status in {"pending", "started"} for a in state.actions):
            return {"route": "execute"}
        if not current_inputs:
            return {"route": "assess"}
        routes = plan_repairs(state, failing, current_inputs, note_for)
        await save("semantic_repair.planned", state.history[-1])
        if not routes:
            state.stop_reason = "no_untried_route"
            return {"route": "finish" if graph_state.get("responded") else "human"}
        return {"route": "execute"}

    async def act(_):
        await execute_actions(
            state, execute=execute, save=save, stop_check=context.raise_if_stopped
        )
        return {"route": "assess"}

    async def prepare_input(_):
        document_id, body_hash = await snapshot()
        identifier = str(uuid.uuid4())
        request = {
            "id": identifier,
            "version": GRAPH_VERSION,
            "document_id": document_id,
            "snapshot_hash": body_hash,
            "reason": state.stop_reason or "evidence_insufficient",
            "sections": state.result()["unresolved"],
            "choices": ["continue", "finish"],
        }
        await context.emit(
            "semantic_repair.needs_input",
            {"reason": request["reason"]},
            checkpoint={"repair_interrupt": request, CHECKPOINT_KEY: state.payload()},
        )
        return {"awaiting_id": identifier}

    async def human(graph_state):
        # No side effects before interrupt: LangGraph re-enters this node on resume.
        response = interrupt({"id": graph_state["awaiting_id"]})
        if response.get("interrupt_id") != graph_state["awaiting_id"]:
            raise ValueError("stale repair response")
        if response.get("choice") not in {"continue", "finish"}:
            raise ValueError("invalid repair response")
        await context.emit(
            "semantic_repair.resumed",
            {"reason": response["choice"]},
            checkpoint={"repair_interrupt": None, "repair_response": None},
        )
        state.stop_reason = None if response["choice"] == "continue" else "user_finished"
        await save("semantic_repair.user_choice", {"reason": response["choice"]})
        return {
            "responded": True,
            "review_only": state.rounds_used >= max_rounds,
            "route": "assess" if response["choice"] == "continue" else "finish",
        }

    async def finish(_):
        await save("semantic_repair.finished", {"stop_reason": state.stop_reason})
        return {"route": "done"}

    graph = StateGraph(GraphState)
    for name, node in (
        ("assess", assess),
        ("plan", plan),
        ("execute", act),
        ("prepare_input", prepare_input),
        ("human", human),
        ("finish", finish),
    ):
        graph.add_node(name, node)
    graph.add_edge(START, "assess")
    graph.add_conditional_edges(
        "assess",
        lambda s: s["route"],
        {"finish": "finish", "plan": "plan", "human": "prepare_input"},
    )
    graph.add_conditional_edges(
        "plan",
        lambda s: s["route"],
        {"execute": "execute", "finish": "finish", "human": "prepare_input", "assess": "assess"},
    )
    graph.add_edge("execute", "assess")
    graph.add_edge("prepare_input", "human")
    graph.add_conditional_edges(
        "human", lambda s: s["route"], {"assess": "assess", "finish": "finish"}
    )
    graph.add_edge("finish", END)
    return graph.compile(checkpointer=checkpointer)


async def run_graph(
    *,
    context,
    state,
    review,
    inputs,
    execute,
    note_for,
    save,
    snapshot,
    checkpointer=None,
    max_rounds=2,
):
    from paperforge_worker.context import JobStopped

    thread_id = context.checkpoint.get("repair_graph_thread")
    if not thread_id:
        thread_id = hashlib.sha256(
            f"{context.project_id}:{context.job_id}:{state.scope}:{GRAPH_VERSION}".encode()
        ).hexdigest()
        await context.emit(
            "semantic_repair.engine",
            {"engine": "langgraph", "version": GRAPH_VERSION},
            checkpoint={"repair_graph_thread": thread_id, "repair_graph_scope": state.scope},
        )
    if context.checkpoint.get("repair_graph_scope") != state.scope:
        raise JobStopped("pause", reason="repair_graph_scope_changed")

    async def invoke(saver):
        graph = build_graph(
            context=context,
            state=state,
            review=review,
            inputs=inputs,
            execute=execute,
            note_for=note_for,
            save=save,
            snapshot=snapshot,
            checkpointer=saver,
            max_rounds=max_rounds,
        )
        config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 32}
        existing = await graph.aget_state(config)
        response = context.checkpoint.get("repair_response")
        # A paused graph is resumed with its exact pending node, not a fresh START invocation.
        argument = Command(resume=response) if response else (None if existing.values else {})
        if existing.values and not existing.next:
            return state.result()
        result = await graph.ainvoke(argument, config)
        if result.get("__interrupt__"):
            raise JobStopped("pause", reason="repair_needs_input")
        await context.emit(
            "section_review.converged",
            state.result(),
            stage="quality",
            checkpoint={CHECKPOINT_KEY: state.payload()},
        )
        return state.result()

    if checkpointer is not None:
        return await invoke(checkpointer)
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from psycopg import AsyncConnection
    from psycopg.rows import dict_row

    url = context.settings.database_url.replace("postgresql+asyncpg://", "postgresql://")
    async with await AsyncConnection.connect(
        url,
        autocommit=True,
        prepare_threshold=0,
        row_factory=dict_row,
        options="-csearch_path=paperforge_graph",
        application_name="paperforge-repair-graph",
    ) as connection:
        return await invoke(AsyncPostgresSaver(connection))


async def setup_graph_store(database_url: str) -> None:
    """Provision the isolated schema once under an advisory lock, safe across workers."""
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from psycopg import AsyncConnection
    from psycopg.rows import dict_row

    async with await AsyncConnection.connect(
        database_url.replace("postgresql+asyncpg://", "postgresql://"),
        autocommit=True,
        prepare_threshold=0,
        row_factory=dict_row,
    ) as connection:
        await connection.execute("SELECT pg_advisory_lock(732108551)")
        try:
            await connection.execute("CREATE SCHEMA IF NOT EXISTS paperforge_graph")
            await connection.execute("SET search_path TO paperforge_graph")
            await AsyncPostgresSaver(connection).setup()
        finally:
            await connection.execute("SELECT pg_advisory_unlock(732108551)")
