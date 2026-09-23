"""Run with `uv run python -m evals.agent_repair.run`; no model or database calls.

Reports policy behavior, not scientific accuracy or real token/cost savings.
"""

from __future__ import annotations

import asyncio
import json

from paperforge_worker.orchestration.semantic_repair import (
    RepairState,
    execute_actions,
    plan_repairs,
)
from paperforge_worker.pipelines.semantic_review import SectionVerdict

CASES = [
    ("overclaim", {"calibration": "overclaimed"}, 1, 0, ["rewrite"]),
    ("listing", {"synthesis_mode": "listed"}, 1, 0, ["resynthesize", "rewrite"]),
    (
        "missing_evidence",
        {"answers_question": "no", "support": "thin"},
        1,
        0,
        ["deepen", "retrieve", "matrix", "rewrite"],
    ),
    ("unused_evidence", {"answers_question": "no", "support": "thin"}, 24, 18, ["rewrite"]),
]


async def evaluate() -> list[dict]:
    results = []
    for name, overrides, pool, unused, expected in CASES:
        state = RepairState(scope=name)
        verdict = SectionVerdict(
            **{
                "section_key": "s1",
                "answers_question": "full",
                "support": "sufficient",
                "calibration": "matched",
                "synthesis_mode": "synthesized",
                "diagnosis": "none",
                "rationale": "fixture",
                **overrides,
            }
        )
        plan_repairs(
            state,
            [verdict],
            [
                {
                    "section_key": "s1",
                    "question_id": "q1",
                    "pool_size": pool,
                    "unused_evidence": unused,
                }
            ],
            lambda *_: "repair fixture",
        )
        actions = []
        events = []

        async def execute(action, *, actions=actions):
            actions.append(action.kind)
            return {"fixture": True}

        async def save(event, payload, *, events=events):
            events.append(event)

        async def stop():
            pass

        await execute_actions(state, execute=execute, save=save, stop_check=stop)
        restored = RepairState.load(json.loads(json.dumps(state.payload())), scope=name)
        await execute_actions(restored, execute=execute, save=save, stop_check=stop)
        results.append(
            {
                "scenario": name,
                "actions": actions,
                "expected_actions": expected,
                "duplicate_actions_on_resume": len(actions) - len(expected),
                "rounds_used_after_resume": restored.rounds_used,
                "passed": actions == expected and restored.rounds_used == 1,
            }
        )
    return results


def main() -> None:
    results = asyncio.run(evaluate())
    print(json.dumps({"mode": "offline_policy_fixtures", "scenarios": results}, indent=2))
    if not all(item["passed"] for item in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
