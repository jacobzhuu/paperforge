from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from db.models.auth import AppUser, AuthActionToken, UserSession


def normalize_email(email: str) -> str:
    return email.strip().lower()


async def create_user(
    session: AsyncSession,
    *,
    email: str,
    password_hash: str,
    display_name: str | None = None,
    status: str = "active",
    verified: bool = False,
) -> AppUser:
    user = AppUser(
        email=normalize_email(email),
        password_hash=password_hash,
        display_name=display_name.strip() if display_name and display_name.strip() else None,
        status=status,
        email_verified_at=datetime.now(UTC) if verified else None,
    )
    session.add(user)
    await session.flush()
    return user


async def get_user(session: AsyncSession, user_id: uuid.UUID) -> AppUser | None:
    return await session.get(AppUser, user_id)


async def get_user_by_email(session: AsyncSession, email: str) -> AppUser | None:
    return await session.scalar(select(AppUser).where(AppUser.email == normalize_email(email)))


async def create_user_session(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    token_hash: str,
    expires_at: datetime,
) -> UserSession:
    row = UserSession(user_id=user_id, token_hash=token_hash, expires_at=expires_at)
    session.add(row)
    await session.flush()
    return row


async def get_active_session_by_hash(
    session: AsyncSession, token_hash: str, *, now: datetime | None = None
) -> tuple[UserSession, AppUser] | None:
    now = now or datetime.now(UTC)
    result = await session.execute(
        select(UserSession, AppUser)
        .join(AppUser, AppUser.id == UserSession.user_id)
        .where(
            UserSession.token_hash == token_hash,
            UserSession.revoked_at.is_(None),
            UserSession.expires_at > now,
            AppUser.status == "active",
        )
    )
    return result.one_or_none()


async def revoke_session(session: AsyncSession, session_id: uuid.UUID) -> None:
    await session.execute(
        update(UserSession)
        .where(UserSession.id == session_id, UserSession.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )


async def revoke_user_sessions(session: AsyncSession, user_id: uuid.UUID) -> None:
    await session.execute(
        update(UserSession)
        .where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )


async def create_action_token(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    purpose: str,
    token_hash: str,
    expires_at: datetime,
) -> AuthActionToken:
    await session.execute(
        update(AuthActionToken)
        .where(
            AuthActionToken.user_id == user_id,
            AuthActionToken.purpose == purpose,
            AuthActionToken.consumed_at.is_(None),
        )
        .values(consumed_at=datetime.now(UTC))
    )
    row = AuthActionToken(
        user_id=user_id,
        purpose=purpose,
        token_hash=token_hash,
        expires_at=expires_at,
    )
    session.add(row)
    await session.flush()
    return row


async def consume_action_token(
    session: AsyncSession,
    *,
    token_hash: str,
    purpose: str,
    now: datetime | None = None,
) -> tuple[AuthActionToken, AppUser] | None:
    now = now or datetime.now(UTC)
    result = await session.execute(
        select(AuthActionToken, AppUser)
        .join(AppUser, AppUser.id == AuthActionToken.user_id)
        .where(
            AuthActionToken.token_hash == token_hash,
            AuthActionToken.purpose == purpose,
            AuthActionToken.consumed_at.is_(None),
            AuthActionToken.expires_at > now,
        )
        .with_for_update(of=AuthActionToken)
    )
    pair = result.one_or_none()
    if pair is None:
        return None
    pair[0].consumed_at = now
    await session.flush()
    return pair
