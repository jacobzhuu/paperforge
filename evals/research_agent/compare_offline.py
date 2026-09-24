"""Reproducible, no-network comparison of fixed statistics and Pi orchestration.

Pi runs as the real pinned Node process, with a scripted model fixture. It never
contacts a model provider. Both arms call the same restricted computation tool.
"""

import asyncio
import json
import os
import time
from pathlib import Path

from paperforge_worker.orchestration.research_graph import compute_process

ROOT = Path(__file__).resolve().parents[2]
SPEC = {
    "columns": ["time_ms"], "group_by": "group",
    "units": {"time_ms": "ms"}, "confirmed": True,
}
CASES = {
    "grouped": (
        b"group,time_ms\nA,1\nA,3\nB,2\nB,4\n",
        SPEC,
    ),
    "missing": (
        b"group,time_ms\nA,1\nA,NA\nA,3\nB,2\nB,4\n",
        SPEC,
    ),
    "single": (
        b"group,time_ms\nA,1\nB,2\n",
        SPEC,
    ),
}
TOOL_ORDER = ("read_table_summary", "compute_descriptive", "read_results", "propose_patch")


async def pi_fixture(content, spec):
    process = await asyncio.create_subprocess_exec(
        "node", str(ROOT / "services/pi/agent.mjs"),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": "/nonexistent"},
    )
    calls = []
    computed = None

    async def send(value):
        process.stdin.write((json.dumps(value) + "\n").encode())
        await process.stdin.drain()

    await send({
        "type": "start", "model": "scripted-offline", "goal": "Describe the confirmed data",
    })
    try:
        for _ in range(12):
            line = await asyncio.wait_for(process.stdout.readline(), 15)
            if not line:
                raise RuntimeError((await process.stderr.read()).decode()[:300])
            request = json.loads(line)
            kind = request["type"]
            if kind == "settled":
                return computed, calls
            if kind == "error":
                raise RuntimeError(request["error"])
            if kind == "model":
                name = TOOL_ORDER[len(calls)] if len(calls) < len(TOOL_ORDER) else None
                result = {
                    "text": "done" if name is None else "",
                    "tool_calls": [] if name is None else [{
                        "id": f"call-{len(calls)}", "type": "function",
                        "function": {"name": name, "arguments": "{}"},
                    }],
                }
            elif kind == "tool":
                name = request["name"]
                if name != TOOL_ORDER[len(calls)]:
                    raise AssertionError(f"unexpected tool: {name}")
                calls.append(name)
                if name == "read_table_summary":
                    result = await compute_process(content, "synthetic.csv", spec, inspect=True)
                elif name == "compute_descriptive":
                    computed = await compute_process(content, "synthetic.csv", spec)
                    result = computed
                elif name == "read_results":
                    result = computed
                else:
                    result = {"status": "proposal_requested"}
            else:
                raise AssertionError(f"unexpected protocol: {kind}")
            await send({"id": request["id"], "result": result})
        raise RuntimeError("Pi did not settle")
    finally:
        process.terminate()
        await process.wait()


async def main():
    results = []
    for name, (content, spec) in CASES.items():
        started = time.perf_counter()
        deterministic = await compute_process(content, "synthetic.csv", spec)
        deterministic_seconds = time.perf_counter() - started
        started = time.perf_counter()
        pi, calls = await pi_fixture(content, spec)
        pi_seconds = time.perf_counter() - started
        correct = (
            pi["records"] == deterministic["records"]
            and pi["policy"] == deterministic["policy"]
        )
        results.append({
            "case": name, "numeric_correct": correct,
            "deterministic_success": bool(deterministic), "pi_fixture_success": bool(pi),
            "deterministic_seconds": round(deterministic_seconds, 3),
            "pi_fixture_seconds": round(pi_seconds, 3),
            "deterministic_model_calls": 0, "pi_fixture_model_calls": len(calls) + 1,
            "deterministic_model_cost": 0, "pi_fixture_model_cost": 0,
            "pi_tools": calls,
        })
        if not correct:
            raise AssertionError(name)
    print(json.dumps({"evidence": "offline-scripted-model", "cases": results}, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
