import asyncio

import httpx
from observability.admission import AdmissionMiddleware


async def test_render_admission_bounds_work_and_keeps_health_available():
    entered = asyncio.Event()
    release = asyncio.Event()

    async def app(scope, receive, send):
        if scope["method"] == "POST":
            entered.set()
            await release.wait()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    middleware = AdmissionMiddleware(app, limit=1, max_bytes=16)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=middleware), base_url="http://test"
    ) as client:
        first = asyncio.create_task(client.post("/render", content=b"x"))
        await entered.wait()
        assert (await client.get("/healthz")).status_code == 200
        assert (await client.post("/render", content=b"x")).status_code == 503
        assert (await client.post("/render", content=b"x" * 17)).status_code == 413
        release.set()
        assert (await first).status_code == 200
        assert (await client.post("/render", content=b"x")).status_code == 200
    assert middleware.active == 0


def test_renderer_client_retries_backpressure_without_repairing_valid_input():
    from observability.http import post_when_available

    requests = []

    def handle(request):
        requests.append(request.content)
        if len(requests) == 1:
            return httpx.Response(503, headers={"Retry-After": "0.01"})
        return httpx.Response(200, json={"ok": True})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = post_when_available(
            client, "http://renderer/compile", json={"valid": "input"}, timeout=1
        )
    assert result.status_code == 200
    assert len(requests) == 2
    assert requests[0] == requests[1]
