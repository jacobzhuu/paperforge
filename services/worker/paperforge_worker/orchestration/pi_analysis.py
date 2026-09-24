"""Pinned Pi Agent Core process; Python owns tools, credentials and model admission."""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import suppress
from dataclasses import replace
from pathlib import Path

from llm_runtime.client import create_llm_provider
from llm_runtime.tool_chat import ToolChatProvider


async def run_pi(context, goal, tool, reserve_turn):
    config = context.settings.llm_config()
    pinned = context.checkpoint.get("research_model") or config.model_for_role("planner")
    if config.price_for_model(pinned) is None:
        raise ValueError("分析模型未配置价格，无法启动付费运行")
    config = replace(config, role_models={**config.role_models, "research": pinned})
    await context.emit("research.model", {"model": pinned}, checkpoint={"research_model": pinned})
    runner = context.llm_runner(
        config=config, provider=ToolChatProvider(create_llm_provider(config))
    )
    script = Path(os.environ.get("PAPERFORGE_PI_SCRIPT", "/app/pi/agent.mjs"))
    if not script.exists():
        script = Path(__file__).resolve().parents[3] / "pi" / "agent.mjs"
    proc = await asyncio.create_subprocess_exec(
        "node",
        str(script),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        limit=4 * 1024 * 1024,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": "/nonexistent"},
    )

    async def send(value):
        proc.stdin.write((json.dumps(value, ensure_ascii=False) + "\n").encode())
        await proc.stdin.drain()

    async def consume():
        await send({"type": "start", "goal": goal, "model": pinned})
        while True:
            await context.raise_if_stopped()
            line = await proc.stdout.readline()
            if not line:
                raise ValueError("Pi process exited before completion")
            record = json.loads(line)
            kind = record.get("type")
            if kind == "settled":
                return
            if kind == "error":
                raise ValueError("Pi analysis failed")
            if kind not in {"model", "tool"}:
                raise ValueError("invalid Pi protocol record")
            try:
                if kind == "model":
                    await reserve_turn()
                    response = await runner.agenerate(
                        "research",
                        system_prompt="Research tool session",
                        user_prompt=json.dumps(
                            {"messages": record["messages"], "tools": record["tools"]}
                        ),
                        max_output_tokens=2048,
                        metadata={"stage": "research", "engine": "pi-0.87.1"},
                    )
                    if response is None:
                        raise ValueError("model unavailable or budget exhausted")
                    result = json.loads(response.text)
                else:
                    result = await tool(record["name"], record.get("args") or {})
                await send({"id": record["id"], "result": result})
            except ValueError as error:
                if kind == "model":
                    raise
                await send({"id": record["id"], "error": str(error)})

    task = asyncio.create_task(consume())
    try:
        while not task.done():
            await asyncio.wait({task}, timeout=1)
            await context.raise_if_stopped()
        await task
    finally:
        task.cancel()
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 3)
            except TimeoutError:
                proc.kill()
                await proc.wait()
        with suppress(asyncio.CancelledError):
            await task
