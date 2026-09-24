"""Bound request concurrency before parsing bodies; retain slots through streaming."""

from __future__ import annotations

import json


class AdmissionMiddleware:
    def __init__(self, app, *, limit=1, max_bytes=96 * 1024 * 1024):
        self.app = app
        self.limit = limit
        self.max_bytes = max_bytes
        self.active = 0

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        try:
            size = int(headers.get(b"content-length", b"0"))
        except ValueError:
            size = self.max_bytes + 1
        code = 413 if size > self.max_bytes else 503 if self.active >= self.limit else None
        if code:
            body = json.dumps(
                {"detail": "request too large" if code == 413 else "renderer busy"}
            ).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": code,
                    "headers": [(b"content-type", b"application/json"), (b"retry-after", b"2")],
                }
            )
            return await send({"type": "http.response.body", "body": body})
        self.active += 1
        received = 0

        async def bounded_receive():
            nonlocal received
            message = await receive()
            received += len(message.get("body", b""))
            if received > self.max_bytes:
                from starlette.exceptions import HTTPException

                raise HTTPException(413, "request too large")
            return message

        try:
            await self.app(scope, bounded_receive, send)
        finally:
            self.active -= 1
