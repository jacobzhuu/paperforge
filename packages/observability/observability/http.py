"""ASGI metrics include streaming duration without buffering responses."""

import time

from observability.metrics import observe_http_request


class HttpMetricsMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        start = time.monotonic()
        status = 500

        async def observe_send(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message.setdefault("headers", []).append(
                    (b"x-paperforge-dispatch-version", b"durable-v1")
                )
            await send(message)

        try:
            await self.app(scope, receive, observe_send)
        finally:
            route = scope.get("route")
            path = getattr(route, "path", "unmatched")
            observe_http_request(scope["method"], path, status, time.monotonic() - start)


def post_when_available(client, url, *, json, timeout):
    """Retry explicit stateless-renderer backpressure within one wall-time budget."""
    import random

    deadline = time.monotonic() + timeout
    remaining = timeout
    while True:
        response = client.post(url, json=json, timeout=remaining)
        if response.status_code not in {429, 503}:
            return response
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return response
        try:
            delay = float(response.headers.get("Retry-After", "0.5"))
        except ValueError:
            delay = 0.5
        time.sleep(min(remaining, max(0.05, min(5, delay)) * random.uniform(1, 1.2)))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return response
