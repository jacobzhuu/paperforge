from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from observability import configure_logging, get_logger

from paperforge_api.config import get_settings
from paperforge_api.deps import create_arq_pool, dispose_engine
from paperforge_api.routers import assets, events, health, projects, writing

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
    try:
        yield
    finally:
        pool = getattr(app.state, "arq_pool", None)
        if pool is not None:
            await pool.aclose()
        await dispose_engine()


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title="PaperForge API",
        version="0.1.0",
        description="成稿优先、引用真实、流程宽松的科研论文生成系统",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in settings.cors_allow_origins.split(",") if o.strip()],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(health.router)
    app.include_router(projects.router)
    app.include_router(writing.router)
    app.include_router(assets.router)
    app.include_router(settings_router.router)
    app.include_router(events.router)
    return app


app = create_app()
