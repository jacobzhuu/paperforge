"""API 依赖：数据库会话与 ARQ 任务队列。"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings
from db import get_active_session_by_hash, get_owned_project, revoke_session
from db.models.auth import AppUser, UserSession
from db.models.paper import PaperProject
from db.session import make_engine, make_session_factory
from fastapi import Cookie, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from paperforge_api.config import Settings, get_settings

_engine = None
_session_factory = None


def get_session_factory():
    global _engine, _session_factory
    if _session_factory is None:
        settings = get_settings()
        _engine = make_engine(settings.database_url, application_name="paperforge-api")
        _session_factory = make_session_factory(_engine)
    return _session_factory


async def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


async def get_session() -> AsyncIterator[AsyncSession]:
    """请求级会话：正常返回时提交，异常回滚。"""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_queue(request: Request) -> ArqRedis | None:
    """ARQ 连接池。Redis 不可用时返回 None，路由据此降级为 503 而不是崩溃。"""
    pool = getattr(request.app.state, "arq_pool", None)
    return pool


async def create_arq_pool(settings: Settings) -> Any:
    return await create_pool(RedisSettings.from_dsn(settings.redis_url))


@dataclass(frozen=True)
class AuthContext:
    user: AppUser
    session: UserSession


def _session_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def get_current_auth(
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    session_token: Annotated[str | None, Cookie(alias="paperforge_session")] = None,
    secure_session_token: Annotated[str | None, Cookie(alias="__Host-paperforge_session")] = None,
) -> AuthContext:
    token = secure_session_token if settings.auth_cookie_secure else session_token
    if not token:
        raise HTTPException(status_code=401, detail={"code": "authentication_required"})
    pair = await get_active_session_by_hash(session, _session_hash(token))
    if pair is None:
        raise HTTPException(status_code=401, detail={"code": "session_invalid"})
    auth_session, user = pair
    now = datetime.now(UTC)
    if auth_session.last_seen_at <= now - timedelta(days=settings.auth_idle_days):
        await revoke_session(session, auth_session.id)
        raise HTTPException(status_code=401, detail={"code": "session_expired"})
    # Bound write amplification while retaining an accurate idle timeout.
    if auth_session.last_seen_at <= now - timedelta(hours=1):
        auth_session.last_seen_at = now
    return AuthContext(user=user, session=auth_session)


async def get_current_user(
    auth: Annotated[AuthContext, Depends(get_current_auth)],
) -> AppUser:
    return auth.user


async def require_owned_project(
    project_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    auth: Annotated[AuthContext, Depends(get_current_auth)],
) -> PaperProject:
    try:
        project_uuid = uuid.UUID(project_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail="project not found") from error
    project = await get_owned_project(session, project_uuid, auth.user.id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return project


async def authorize_project_request(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    auth: Annotated[AuthContext, Depends(get_current_auth)],
) -> AuthContext:
    """Authenticate a router and enforce ownership whenever its route has project_id."""
    project_id = request.path_params.get("project_id")
    if project_id is None:
        return auth
    try:
        project_uuid = uuid.UUID(project_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail="project not found") from error
    # 已软删除的项目也在这里取回来（include_deleted=True），但**不**放行：
    # 过滤留给 get_authorized_project。否则「恢复」端点自己就 404 了，
    # 回收站里的项目再也捞不回来。
    project = await get_owned_project(session, project_uuid, auth.user.id, include_deleted=True)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    request.state.owned_project = project
    session.info["paperforge_owned_project"] = project
    return auth


async def get_authorized_project(
    session: AsyncSession,
    project_id: str,
    *,
    include_deleted: bool = False,
) -> PaperProject:
    """Return only the project already authorized for this request.

    Business routers deliberately cannot fall back to a project-id-only database lookup.
    The router dependency above is the sole place that resolves ownership.

    软删除在这里收口：除了「恢复」这类必须看见墓碑的端点，其余一律当作不存在——
    删掉的项目，它的每一个子路由都应该 404，而不是各路由自己记得判一次。
    """
    try:
        project_uuid = uuid.UUID(project_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail="project not found") from error
    project = session.info.get("paperforge_owned_project")
    if not isinstance(project, PaperProject) or project.id != project_uuid:
        raise HTTPException(status_code=404, detail="project not found")
    if project.deleted_at is not None and not include_deleted:
        raise HTTPException(status_code=404, detail="project not found")
    return project


CurrentAuthDep = Annotated[AuthContext, Depends(get_current_auth)]
CurrentUserDep = Annotated[AppUser, Depends(get_current_user)]
OwnedProjectDep = Annotated[PaperProject, Depends(require_owned_project)]
