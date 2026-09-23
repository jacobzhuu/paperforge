"""Exercise real bounded renderers and their production backpressure clients."""

import asyncio
import json
import threading
import time
from collections import Counter
from pathlib import Path

import httpx
from latex_render.compile import TexdClient
from visuals.client import VisualdClient


async def measure(kind, port):
    counts = Counter()
    lock = threading.Lock()
    durations = []
    health = []
    complete = asyncio.Event()

    def response_hook(response):
        with lock:
            counts[str(response.status_code)] += 1

    def render():
        started = time.monotonic()
        with httpx.Client(event_hooks={"response": [response_hook]}, trust_env=False) as http:
            if kind == "texd":
                renderer = TexdClient(f"http://127.0.0.1:{port}", client=http)
                result = renderer.compile(
                    {
                        "main.tex": (
                            r"\documentclass{article}\begin{document}"
                            r"Capacity fixture.\end{document}"
                        )
                    }
                )
                assert result.ok and result.pdf.startswith(b"%PDF"), result.log
            else:
                renderer = VisualdClient(f"http://127.0.0.1:{port}", client=http)
                result = renderer.render_chart(
                    {
                        "kind": "chart",
                        "chart_type": "bar",
                        "source_asset_ref": "ua_12345678",
                        "x": "method",
                        "y": ["score"],
                    },
                    {"headers": ["method", "score"], "rows": [["A", 0.8], ["B", 0.9]]},
                )
                assert {item.format for item in result} == {"png", "svg", "pdf"}
        durations.append(time.monotonic() - started)

    async def probe():
        async with httpx.AsyncClient(trust_env=False) as client:
            while not complete.is_set():
                start = time.monotonic()
                response = await client.get(f"http://127.0.0.1:{port}/healthz", timeout=5)
                response.raise_for_status()
                health.append(time.monotonic() - start)
                await asyncio.sleep(0.1)

    start = time.monotonic()
    checking = asyncio.create_task(probe())
    try:
        await asyncio.gather(*(asyncio.to_thread(render) for _ in range(10)))
    finally:
        complete.set()
        await checking
    return {
        "service": kind,
        "submitted_concurrently": 10,
        "completed": len(durations),
        "elapsed_seconds": time.monotonic() - start,
        "http_attempt_statuses": dict(counts),
        "completion_seconds": sorted(durations),
        "health_max_seconds": max(health),
    }


async def main():
    results = []
    for kind, port in [("texd", 28083), ("visuald", 28084)]:
        results.append(await measure(kind, port))
    Path("evals/capacity/results/renderers.json").write_text(json.dumps(results, indent=2) + "\n")
    print(json.dumps(results, indent=2))


asyncio.run(main())
