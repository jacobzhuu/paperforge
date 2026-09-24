"""Task-local trace context; provider threads inherit it through asyncio.to_thread."""

from contextvars import ContextVar
from functools import wraps

current_span: ContextVar[dict | None] = ContextVar("agent_span", default=None)


def traced(kind: str, name: str):
    def decorate(fn):
        @wraps(fn)
        async def run(*args, **kwargs):
            context = kwargs.get("context") or args[0]
            if not hasattr(context, "span"):
                return await fn(*args, **kwargs)
            async with context.span(kind, name):
                return await fn(*args, **kwargs)

        return run

    return decorate
