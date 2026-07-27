from __future__ import annotations

import hashlib
import hmac
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from arq.connections import ArqRedis
from db import (
    consume_action_token,
    create_action_token,
    create_user,
    create_user_session,
    get_user,
    get_user_by_email,
    revoke_session,
    revoke_user_sessions,
)
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from paperforge_api.auth_service import (
    action_expiry,
    hash_password,
    new_token,
    password_needs_rehash,
    session_expiry,
    token_hash,
    verify_login_password,
    verify_password,
)
from paperforge_api.config import Settings, get_settings
from paperforge_api.deps import CurrentAuthDep, get_queue, get_session
from paperforge_api.mailer import send_auth_email
from paperforge_api.schemas import (
    AuthMessageResponse,
    ChangePasswordRequest,
    ForgotPasswordRequest,
    LoginRequest,
    LoginResponse,
    RegisterRequest,
    ResetPasswordRequest,
    TokenRequest,
    UserResponse,
)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
QueueDep = Annotated[ArqRedis | None, Depends(get_queue)]
SettingsDep = Annotated[Settings, Depends(get_settings)]

DEV_LOGIN_USERNAME = "admin"
DEV_LOGIN_PASSWORD = "123456"
DEV_LOGIN_EMAIL = "admin@paperforge.local"
LEGACY_USER_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")


@router.post("/register", response_model=AuthMessageResponse, status_code=status.HTTP_202_ACCEPTED)
async def register(
    body: RegisterRequest,
    request: Request,
    session: SessionDep,
    queue: QueueDep,
    settings: SettingsDep,
) -> AuthMessageResponse:
    await _rate_limit(queue, settings, request, "register", str(body.email), limit=5, window=3600)
    try:
        password_hash = hash_password(body.password)
    except ValueError as error:
        raise HTTPException(
            status_code=422,
            detail={"code": "weak_password", "message": str(error)},
        ) from error

    user = await get_user_by_email(session, str(body.email))
    if user is None:
        try:
            async with session.begin_nested():
                user = await create_user(
                    session,
                    email=str(body.email),
                    password_hash=password_hash,
                    display_name=body.display_name,
                )
        except IntegrityError:
            user = await get_user_by_email(session, str(body.email))

    if user is not None and user.email_verified_at is None and user.status == "active":
        raw_token = new_token()
        await create_action_token(
            session,
            user_id=user.id,
            purpose="verify_email",
            token_hash=token_hash(raw_token),
            expires_at=action_expiry(hours=24),
        )
        await send_auth_email(
            settings, recipient=user.email, purpose="verify_email", token=raw_token
        )
    return AuthMessageResponse(
        message="If the address can be registered, a verification email was sent."
    )


@router.post("/verify-email", response_model=AuthMessageResponse)
async def verify_email(body: TokenRequest, session: SessionDep) -> AuthMessageResponse:
    pair = await consume_action_token(
        session, token_hash=token_hash(body.token), purpose="verify_email"
    )
    if pair is None:
        raise HTTPException(status_code=422, detail={"code": "invalid_or_expired_token"})
    _token, user = pair
    user.email_verified_at = datetime.now(UTC)
    return AuthMessageResponse(message="Email verified.")


@router.post("/login", response_model=LoginResponse)
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    session: SessionDep,
    queue: QueueDep,
    settings: SettingsDep,
) -> LoginResponse:
    account = body.email.strip()
    await _rate_limit(queue, settings, request, "login", account, limit=10, window=900)
    dev_login = (
        settings.auth_dev_login_enabled
        and hmac.compare_digest(account.casefold(), DEV_LOGIN_USERNAME)
        and hmac.compare_digest(body.password, DEV_LOGIN_PASSWORD)
    )
    if dev_login:
        user = await _ensure_dev_user(session)
        password_matches = True
    else:
        user = await get_user_by_email(session, account)
        usable_hash = user.password_hash if user is not None and user.status == "active" else None
        password_matches = verify_login_password(usable_hash, body.password)
    if (
        user is None
        or user.status != "active"
        or not password_matches
    ):
        raise HTTPException(status_code=401, detail={"code": "invalid_credentials"})
    if user.email_verified_at is None:
        raise HTTPException(status_code=403, detail={"code": "email_verification_required"})
    if not dev_login and password_needs_rehash(user.password_hash):
        user.password_hash = hash_password(body.password)
    raw_token = new_token()
    await create_user_session(
        session,
        user_id=user.id,
        token_hash=token_hash(raw_token),
        expires_at=session_expiry(settings.auth_session_days),
    )
    user.last_login_at = datetime.now(UTC)
    _set_session_cookie(response, settings, raw_token)
    return LoginResponse(user=_user_response(user))


async def _ensure_dev_user(session: AsyncSession):
    """Activate the legacy owner so local development keeps access to migrated projects."""
    user = await get_user(session, LEGACY_USER_ID)
    if user is None:
        user = await get_user_by_email(session, DEV_LOGIN_EMAIL)
    if user is None:
        user = await create_user(
            session,
            email=DEV_LOGIN_EMAIL,
            password_hash="!development-login-only",
            display_name="Admin",
            verified=True,
        )
    elif user.status == "disabled" and user.email == "legacy@paperforge.local":
        user.email = DEV_LOGIN_EMAIL
        user.display_name = "Admin"
        user.password_hash = "!development-login-only"
    user.status = "active"
    user.email_verified_at = user.email_verified_at or datetime.now(UTC)
    return user


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    response: Response, auth: CurrentAuthDep, settings: SettingsDep, session: SessionDep
) -> None:
    await revoke_session(session, auth.session.id)
    _clear_session_cookie(response, settings)


def _clear_session_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        settings.auth_cookie_name,
        path="/",
        secure=settings.auth_cookie_secure,
        httponly=True,
        samesite="lax",
    )


@router.get("/me", response_model=UserResponse)
async def me(auth: CurrentAuthDep) -> UserResponse:
    return _user_response(auth.user)


@router.post(
    "/forgot-password", response_model=AuthMessageResponse, status_code=status.HTTP_202_ACCEPTED
)
async def forgot_password(
    body: ForgotPasswordRequest,
    request: Request,
    session: SessionDep,
    queue: QueueDep,
    settings: SettingsDep,
) -> AuthMessageResponse:
    await _rate_limit(queue, settings, request, "recover", str(body.email), limit=5, window=3600)
    user = await get_user_by_email(session, str(body.email))
    if user is not None and user.status == "active":
        raw_token = new_token()
        await create_action_token(
            session,
            user_id=user.id,
            purpose="reset_password",
            token_hash=token_hash(raw_token),
            expires_at=action_expiry(minutes=30),
        )
        await send_auth_email(
            settings, recipient=user.email, purpose="reset_password", token=raw_token
        )
    return AuthMessageResponse(message="If the account exists, a reset email was sent.")


@router.post("/reset-password", response_model=AuthMessageResponse)
async def reset_password(
    body: ResetPasswordRequest,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
) -> AuthMessageResponse:
    try:
        password_hash = hash_password(body.new_password)
    except ValueError as error:
        raise HTTPException(
            status_code=422,
            detail={"code": "weak_password", "message": str(error)},
        ) from error
    pair = await consume_action_token(
        session, token_hash=token_hash(body.token), purpose="reset_password"
    )
    if pair is None:
        raise HTTPException(status_code=422, detail={"code": "invalid_or_expired_token"})
    _token, user = pair
    user.password_hash = password_hash
    user.email_verified_at = user.email_verified_at or datetime.now(UTC)
    await revoke_user_sessions(session, user.id)
    _clear_session_cookie(response, settings)
    return AuthMessageResponse(message="Password reset complete.")


@router.post("/change-password", response_model=AuthMessageResponse)
async def change_password(
    body: ChangePasswordRequest,
    response: Response,
    auth: CurrentAuthDep,
    session: SessionDep,
    settings: SettingsDep,
) -> AuthMessageResponse:
    if not verify_password(auth.user.password_hash, body.current_password):
        raise HTTPException(status_code=401, detail={"code": "invalid_credentials"})
    try:
        auth.user.password_hash = hash_password(body.new_password)
    except ValueError as error:
        raise HTTPException(
            status_code=422,
            detail={"code": "weak_password", "message": str(error)},
        ) from error
    await revoke_user_sessions(session, auth.user.id)
    _clear_session_cookie(response, settings)
    return AuthMessageResponse(message="Password changed. Sign in again.")


def _set_session_cookie(response: Response, settings: Settings, token: str) -> None:
    response.set_cookie(
        settings.auth_cookie_name,
        token,
        max_age=settings.auth_session_days * 24 * 60 * 60,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite="lax",
        path="/",
    )


def _user_response(user: Any) -> UserResponse:
    return UserResponse(
        id=str(user.id),
        email=user.email,
        display_name=user.display_name,
        email_verified=user.email_verified_at is not None,
    )


async def _rate_limit(
    queue: ArqRedis | None,
    settings: Settings,
    request: Request,
    action: str,
    email: str,
    *,
    limit: int,
    window: int,
) -> None:
    if not settings.auth_rate_limit_enabled:
        return
    if queue is None:
        raise HTTPException(status_code=503, detail={"code": "rate_limiter_unavailable"})
    client_ip = request.client.host if request.client else "unknown"
    try:
        attempts = []
        for dimension, identity in (
            ("ip", client_ip),
            ("email", email.strip().lower()),
        ):
            digest = hashlib.sha256(identity.encode()).hexdigest()
            key = f"paperforge:auth:{action}:{dimension}:{digest}"
            count = int(await queue.incr(key))
            attempts.append(count)
            if count == 1:
                await queue.expire(key, window)
    except Exception as error:  # noqa: BLE001 - security control fails closed
        raise HTTPException(
            status_code=503, detail={"code": "rate_limiter_unavailable"}
        ) from error
    if any(count > limit for count in attempts):
        raise HTTPException(status_code=429, detail={"code": "too_many_attempts"})
