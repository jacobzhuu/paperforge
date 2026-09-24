"""Cross-replica upload and SSE admission, before request bodies are parsed."""

from __future__ import annotations

import asyncio
import uuid
from contextlib import suppress

from fastapi import HTTPException, Request
from starlette.responses import JSONResponse

_ACQUIRE = """
local now = redis.call('TIME')[1]
for i=1,2 do redis.call('ZREMRANGEBYSCORE', KEYS[i], '-inf', now) end
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[2]) or
   redis.call('ZCARD', KEYS[2]) >= tonumber(ARGV[3]) then return 0 end
for i=1,2 do
  redis.call('ZADD', KEYS[i], now+90, ARGV[1])
  redis.call('EXPIRE', KEYS[i], 120)
end
return 1
"""
_RENEW = """
local now = redis.call('TIME')[1]
for i=1,2 do
 local expiry = redis.call('ZSCORE', KEYS[i], ARGV[1])
 if not expiry or tonumber(expiry) <= tonumber(now) then return 0 end
end
for i=1,2 do
 redis.call('ZADD', KEYS[i], now+90, ARGV[1])
 redis.call('EXPIRE', KEYS[i], 120)
end
return 1
"""


class RequestLimitsMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        from paperforge_api.config import get_settings

        settings = get_settings()
        if not settings.request_limits_enabled:
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        upload = scope["method"] == "POST" and path.endswith(("/assets", "/pdf-uploads"))
        stream = scope["method"] == "GET" and path.endswith("/events")
        if not upload and not stream:
            return await self.app(scope, receive, send)
        from paperforge_api.deps import _authenticate_session, get_session_factory

        request = Request(scope)
        try:
            async with get_session_factory()() as session:
                auth = await _authenticate_session(
                    session,
                    settings,
                    session_token=request.cookies.get("paperforge_session"),
                    secure_session_token=request.cookies.get("__Host-paperforge_session"),
                )
                owner = str(auth.user.id)
                await session.commit()
        except HTTPException as error:
            return await JSONResponse({"detail": error.detail}, error.status_code)(
                scope, receive, send
            )
        queue = getattr(scope["app"].state, "arq_pool", None)
        if queue is None:
            return await JSONResponse({"detail": "admission unavailable"}, 503)(
                scope, receive, send
            )
        category = "upload" if upload else "sse"
        keys = [f"paperforge:admission:{category}", f"paperforge:admission:{category}:{owner}"]
        token = uuid.uuid4().hex
        try:
            granted = queue is not None and await asyncio.wait_for(
                queue.eval(_ACQUIRE, 2, *keys, token, 4 if upload else 120, 2 if upload else 6), 3
            )
        except Exception:
            return await JSONResponse({"detail": "admission unavailable"}, 503)(
                scope, receive, send
            )
        if not granted:
            return await JSONResponse(
                {"detail": {"code": "request_capacity_exceeded"}}, 429, headers={"Retry-After": "5"}
            )(scope, receive, send)

        async def renew():
            while True:
                await asyncio.sleep(20)
                try:
                    renewed = await asyncio.wait_for(queue.eval(_RENEW, 2, *keys, token), 3)
                except Exception:
                    if stream:
                        # Existing SSE connections can replay committed DB events
                        # during a Redis outage. New connections still fail closed.
                        continue
                    raise
                if not renewed:
                    if stream and await asyncio.wait_for(
                        queue.eval(_ACQUIRE, 2, *keys, token, 120, 6), 3
                    ):
                        continue
                    raise RuntimeError("request lease lost")

        task = asyncio.create_task(self.app(scope, receive, send))
        renewal = asyncio.create_task(renew())
        try:
            done, _ = await asyncio.wait({task, renewal}, return_when=asyncio.FIRST_COMPLETED)
            if renewal in done:
                task.cancel()
                await renewal
            await task
        finally:
            task.cancel()
            renewal.cancel()
            for pending in (task, renewal):
                with suppress(asyncio.CancelledError, Exception):
                    await pending
            with suppress(Exception):
                for key in keys:
                    await asyncio.wait_for(queue.zrem(key, token), 3)
