from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from observability import configure_logging, get_logger

from paperforge_api.config import get_settings
from paperforge_api.deps import create_arq_pool, dispose_engine
from paperforge_api.routers import (
    agent,
    assets,
    auth,
    events,
    health,
    library_pdf,
    projects,
    visuals,
    web_research,
    writing,
)

# 别名：create_app 内的局部变量 settings 是配置对象，避免与路由模块重名。
from paperforge_api.routers import settings as settings_router

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    try:
        app.state.arq_pool = await create_arq_pool(settings)
    except Exception as error:  # noqa: BLE001 - Redis 不可用时 API 仍应可读
        logger.warning(
            "task queue unavailable at startup",
            extra={"error": type(error).__name__},
        )
        app.state.arq_pool = None
    from paperforge_api.dispatch import dispatch_loop

    dispatcher = asyncio.create_task(dispatch_loop(app))
    try:
        yield
    finally:
        dispatcher.cancel()
        with suppress(asyncio.CancelledError):
            await dispatcher
        pool = getattr(app.state, "arq_pool", None)
        if pool is not None:
            await pool.aclose()
        await dispose_engine()


def create_app() -> FastAPI:
    settings = get_settings()
    public_host = urlparse(settings.public_app_url).hostname
    if settings.auth_dev_login_enabled and (
        settings.auth_cookie_secure or public_host not in {"localhost", "127.0.0.1", "::1"}
    ):
        raise RuntimeError(
            "AUTH_DEV_LOGIN_ENABLED is only allowed for an insecure localhost development site"
        )
    if settings.auth_cookie_secure and settings.auth_email_mode not in {"smtp", "disabled"}:
        raise RuntimeError("secure authentication requires AUTH_EMAIL_MODE=smtp or disabled")
    if settings.auth_cookie_secure and not settings.public_app_url.startswith("https://"):
        raise RuntimeError("secure authentication requires an HTTPS PUBLIC_APP_URL")
    cors_origins = [o.strip() for o in settings.cors_allow_origins.split(",") if o.strip()]
    if settings.auth_cookie_secure and "*" in cors_origins:
        raise RuntimeError("secure authentication does not allow wildcard CORS origins")
    configure_logging(settings.log_level)

    app = FastAPI(
        title="PaperForge API",
        version="0.1.0",
        description="成稿优先、引用真实、流程宽松的科研论文生成系统",
        lifespan=lifespan,
    )
    from observability.http import HttpMetricsMiddleware

    from paperforge_api.request_limits import RequestLimitsMiddleware

    app.add_middleware(HttpMetricsMiddleware)
    app.add_middleware(RequestLimitsMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Accept", "Content-Type", "Last-Event-ID"],
        allow_credentials=True,
    )

    allowed_origins = {
        origin.strip().rstrip("/")
        for origin in settings.cors_allow_origins.split(",")
        if origin.strip()
    }
    allowed_origins.add(settings.public_app_url.rstrip("/"))

    @app.middleware("http")
    async def verify_browser_origin(request: Request, call_next):
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.url.path.startswith(
            "/api/v1/"
        ):
            origin = request.headers.get("origin")
            if not origin or origin.rstrip("/") not in allowed_origins:
                return JSONResponse(
                    status_code=403,
                    content={"detail": {"code": "origin_not_allowed"}},
                )
        return await call_next(request)

    from paperforge_api.routers import research

    app.include_router(research.router)
    app.include_router(agent.router)
    app.include_router(health.router)
    app.include_router(auth.router)
    from paperforge_api.routers import intake

    app.include_router(intake.router)
    app.include_router(projects.router)
    app.include_router(library_pdf.router)
    app.include_router(writing.router)
    app.include_router(web_research.router)
    app.include_router(assets.router)
    app.include_router(visuals.router)
    app.include_router(settings_router.router)
    app.include_router(events.router)
    return app


app = create_app()
