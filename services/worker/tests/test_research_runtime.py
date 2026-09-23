import asyncio
import json
import os
from pathlib import Path

import pytest
from paperforge_worker.orchestration.research_graph import compute_process


async def test_real_restricted_computation():
    result = await compute_process(
        b"g,x\na,1\na,3\n", "data.csv", {"columns": ["x"], "group_by": "g", "units": {"x": "1"}}
    )
    assert result["records"][0]["mean"] == 2
    assert result["chart_base64"].startswith("iVBOR")


async def test_pi_runs_tool_loop_with_gateway_fixture():
    script = Path(__file__).resolve().parents[2] / "pi" / "agent.mjs"
    proc = await asyncio.create_subprocess_exec(
        "node",
        str(script),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={"PATH": os.environ["PATH"], "HOME": "/nonexistent"},
    )

    async def send(value):
        proc.stdin.write((json.dumps(value) + "\n").encode())
        await proc.stdin.drain()

    calls = []
    await send({"type": "start", "model": "test", "goal": "summarize"})
    try:
        for _ in range(12):
            line = await asyncio.wait_for(proc.stdout.readline(), 10)
            assert line, (await proc.stderr.read()).decode()
            request = json.loads(line)
            if request["type"] == "settled":
                break
            if request["type"] == "model":
                assert request["messages"][0]["role"] == "system"
                result = {"text": "done", "tool_calls": []}
                if not calls:
                    result = {
                        "text": "",
                        "tool_calls": [
                            {
                                "id": "t1",
                                "type": "function",
                                "function": {"name": "compute_descriptive", "arguments": "{}"},
                            }
                        ],
                    }
                await send({"id": request["id"], "result": result})
            elif request["type"] == "tool":
                calls.append(request["name"])
                await send({"id": request["id"], "result": {"mean": 2}})
            else:
                pytest.fail(str(request))
        assert calls == ["compute_descriptive"]
    finally:
        proc.terminate()
        await proc.wait()


async def test_compute_sandbox_denies_other_files_network_and_truncation(tmp_path):
    import sys

    secret = tmp_path / "private"
    secret.write_text("keep")
    workspace = tmp_path / "scratch"
    workspace.mkdir()
    script = f"""
import os, socket
from paperforge_worker.orchestration.analysis_process import restrict, restrict_files
restrict_files({str(workspace)!r})
restrict()
blocked = 0
for operation in (lambda: open({str(secret)!r}).read(), lambda: socket.socket(),
                  lambda: os.open({str(secret)!r}, os.O_RDONLY | os.O_TRUNC)):
    try:
        operation()
    except PermissionError:
        blocked += 1
print(blocked)
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", script, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    output, error = await process.communicate()
    assert process.returncode == 0, error.decode()
    assert output.strip() == b"3"
    assert secret.read_text() == "keep"


async def test_python_gateway_and_real_pi_process_share_typed_calls(monkeypatch):
    from types import SimpleNamespace

    from llm_runtime import LLMConfig, LLMRunner
    from llm_runtime.config import ModelPrice
    from llm_runtime.types import LLMResponse
    from paperforge_worker.orchestration import pi_analysis

    config = LLMConfig(
        provider="openai",
        api_key="test",
        base_url="http://unused",
        model="gpt-4o-mini",
        model_prices={"gpt-4o-mini": ModelPrice(1, 1)},
    )
    tools, records, reservations = [], [], []

    class Provider:
        def generate(self, request):
            assert request.messages[0]["role"] == "system"
            assert request.tools[0]["function"]["name"] == "read_table_summary"
            calls = (
                []
                if tools
                else [
                    {
                        "id": "t1",
                        "type": "function",
                        "function": {"name": "compute_descriptive", "arguments": "{}"},
                    }
                ]
            )
            return LLMResponse(
                text="done" if tools else "",
                tool_calls=calls,
                model="gpt-4o-mini",
                provider="test",
                usage={"prompt_tokens": 10, "completion_tokens": 5},
            )

    monkeypatch.setattr(pi_analysis, "create_llm_provider", lambda _: Provider())

    class Context:
        settings = SimpleNamespace(llm_config=lambda: config)
        checkpoint = {}

        async def emit(self, *args, **kwargs):
            self.checkpoint.update(kwargs.get("checkpoint") or {})

        async def raise_if_stopped(self):
            pass

        def llm_runner(self, *, config, provider):
            return LLMRunner(config, provider=provider, on_call=records.append)

    async def tool(name, args):
        tools.append(name)
        return {"mean": 2}

    async def reserve():
        reservations.append(True)

    await pi_analysis.run_pi(Context(), "analyze", tool, reserve)
    assert tools == ["compute_descriptive"]
    assert len(records) == len(reservations) == 2
    assert all(record.cost_estimate is not None for record in records)
