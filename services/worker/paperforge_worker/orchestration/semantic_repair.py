"""Persistable semantic repair decisions, independent of queue and model providers.

The planner is a domain policy, not an additional LLM. Actions have durable boundaries;
external calls are not transactional with checkpoint writes (no exactly-once claim).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from paperforge_worker.pipelines.semantic_review import SectionVerdict, repair_route

CHECKPOINT_KEY = "semantic_repair"
ActionKind = Literal["deepen", "retrieve", "matrix", "resynthesize", "rewrite"]
ActionStatus = Literal["pending", "started", "completed", "interrupted", "failed"]


@dataclass
class RepairAction:
    kind: ActionKind
    sections: list[str]
    questions: list[str] = field(default_factory=list)
    status: ActionStatus = "pending"
    result: dict[str, Any] = field(default_factory=dict)


@dataclass
class RepairState:
    scope: str
    version: int = 1
    rounds_used: int = 0
    attempted: dict[str, list[str]] = field(default_factory=dict)
    note_hashes: dict[str, str] = field(default_factory=dict)
    input_hashes: dict[str, str] = field(default_factory=dict)
    notes: dict[str, str] = field(default_factory=dict)
    verdicts: dict[str, dict[str, Any]] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)
    actions: list[RepairAction] = field(default_factory=list)
    stop_reason: str | None = None
    unassessed: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, raw: Any, *, scope: str) -> RepairState:
        if not isinstance(raw, dict) or raw.get("version") != 1 or raw.get("scope") != scope:
            return cls(scope=scope)
        values = {key: raw[key] for key in cls.__dataclass_fields__ if key in raw}
        values["actions"] = [RepairAction(**item) for item in raw.get("actions", [])]
        return cls(**values)

    def payload(self) -> dict[str, Any]:
        return asdict(self)

    def result(self) -> dict[str, Any]:
        return {
            "rounds": self.history,
            "verdicts": list(self.verdicts.values()),
            "unresolved": [
                key for key, item in self.verdicts.items() if not item.get("acceptable")
            ],
            "unassessed": self.unassessed,
            "rounds_used": self.rounds_used,
            "stop_reason": self.stop_reason,
        }


def plan_repairs(
    state: RepairState,
    verdicts: list[SectionVerdict],
    inputs: list[dict[str, Any]],
    note_for: Callable[[SectionVerdict, dict[str, Any]], str],
) -> dict[str, str]:
    """Reserve one round, select routes, and preserve the existing dependency order."""
    shelf = {item["section_key"]: item for item in inputs}
    routes: dict[str, str] = {}
    for verdict in verdicts:
        key = verdict.section_key
        item = shelf.get(key, {})
        tried = set(state.attempted.get(key, []))
        input_hash = hashlib.sha256(
            json.dumps(
                {"question": item.get("question"), "evidence": item.get("evidence")},
                sort_keys=True,
                default=str,
            ).encode()
        ).hexdigest()
        if key in state.input_hashes and state.input_hashes[key] != input_hash:
            # New evidence can justify another route, but never resets rounds/call budget.
            tried.clear()
        state.input_hashes[key] = input_hash
        note = note_for(verdict, item)
        fingerprint = hashlib.sha256(note.encode()).hexdigest()
        if note and fingerprint != state.note_hashes.get(key):
            tried.discard("rewrite")
        state.note_hashes[key] = fingerprint
        state.notes[key] = note
        route = repair_route(
            verdict,
            attempted=frozenset(tried),
            pool_size=int(item.get("pool_size", 0)),
            unused_evidence=int(item.get("unused_evidence", 0)),
        )
        if route != "none":
            tried.add(route)
            routes[key] = route
        state.attempted[key] = sorted(tried)
    state.rounds_used += 1
    state.history.append(
        {
            "attempt": state.rounds_used,
            "failing": [item.section_key for item in verdicts],
            "diagnoses": {item.section_key: item.diagnosis for item in verdicts},
            "routes": routes,
            "reasons": {
                key: {
                    "diagnosis": item.diagnosis,
                    "pool_size": shelf.get(key, {}).get("pool_size", 0),
                    "unused_evidence": shelf.get(key, {}).get("unused_evidence", 0),
                }
                for item in verdicts
                if (key := item.section_key) in routes
            },
        }
    )
    state.actions = []
    retrieval = sorted(key for key, route in routes.items() if route == "retrieve")
    questions = sorted(
        {
            str(shelf[key]["question_id"])
            for key in retrieval
            if shelf.get(key, {}).get("question_id") is not None
        }
    )
    if retrieval:
        state.actions.extend(
            RepairAction(kind, retrieval, questions) for kind in ("deepen", "retrieve", "matrix")
        )
    synthesis = sorted(key for key, route in routes.items() if route == "resynthesize")
    if synthesis:
        state.actions.append(RepairAction("resynthesize", synthesis))
    if routes:
        state.actions.append(RepairAction("rewrite", sorted(routes)))
    return routes


async def execute_actions(
    state: RepairState,
    *,
    execute: Callable[[RepairAction], Awaitable[dict[str, Any]]],
    save: Callable[[str, dict[str, Any]], Awaitable[None]],
    stop_check: Callable[[], Awaitable[None]],
) -> bool:
    """Resume completed boundaries; an ambiguous in-flight action requires reevaluation."""
    uncertain = [action for action in state.actions if action.status == "started"]
    if uncertain:
        for action in state.actions:
            if action.status in {"pending", "started"}:
                action.status = "interrupted"
        # Do not execute downstream actions based on an uncertain prerequisite.
        state.history[-1]["actions"] = [asdict(action) for action in state.actions]
        await save("semantic_repair.interrupted", {"actions": [a.kind for a in uncertain]})
        return False
    for action in state.actions:
        if action.status != "pending":
            continue
        await stop_check()
        action.status = "started"
        await save("semantic_repair.action_started", {"action": action.kind})
        # Stop/cancel/crash leaves started durable: recovery evaluates persisted artifacts.
        action.result = await execute(action)
        action.status = "completed"
        state.history[-1]["actions"] = [asdict(item) for item in state.actions]
        await save(
            "semantic_repair.action_completed",
            {
                "action": action.kind,
                "result": action.result,
            },
        )
    return True
