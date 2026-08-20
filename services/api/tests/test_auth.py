from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from db import create_user, create_user_session
from db.session import make_engine, make_session_factory
from fastapi.testclient import TestClient
from paperforge_api.deps import get_queue
from paperforge_api.main import create_app

from conftest import run_async

PASSWORD = "correct horse battery staple"
NEW_PASSWORD = "a much better replacement passphrase"


def test_api_settings_resolve_yunwu_specific_image_credentials() -> None:
    from paperforge_api.config import Settings

    settings = Settings(
        _env_file=None,
        image_provider="yunwu",
        image_api_key="cloudflare-key",
        image_model="@cf/model",
        image_base_url="https://api.cloudflare.com/client/v4",
        yunwu_api_key="yunwu-key",
        yunwu_image_model="gpt-image-1",
        yunwu_api_base_url="https://yunwu.ai/v1",
        yunwu_image_timeout_seconds=240,
    )
    config = settings.image_provider_config()
    assert config.provider == "yunwu"
    assert config.api_key == "yunwu-key"
    assert config.model == "gpt-image-1"
    assert config.base_url == "https://yunwu.ai/v1"
    assert config.timeout_seconds == 240


class _Queue:
    async def enqueue_job(self, *_args: Any, **_kwargs: Any) -> Any:
        return SimpleNamespace(job_id=_kwargs.get("_job_id"))


class _CountingQueue(_Queue):
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}

    async def incr(self, key: str) -> int:
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    async def expire(self, _key: str, _window: int) -> None:
        return None


@pytest.fixture
def auth_client(clean_pg_database_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", clean_pg_database_url)
    monkeypatch.setenv("AUTH_RATE_LIMIT_ENABLED", "false")
    monkeypatch.setenv("AUTH_EMAIL_MODE", "console")
    # 与 test_projects_api.py 的 client 夹具同理：必须与开发者的真实 provider
    # 配置隔离，否则本地填了 Cloudflare 凭据时 image_provider_configured 断言会翻车。
    monkeypatch.setenv("LLM_DEFAULT_PROVIDER", "noop")
    monkeypatch.setenv("LLM_OPENAI_API_KEY", "")
    monkeypatch.setenv("IMAGE_API_KEY", "")
    monkeypatch.setenv("IMAGE_ACCOUNT_ID", "")
    import paperforge_api.config as api_config
    import paperforge_api.deps as api_deps
    import paperforge_api.routers.auth as auth_router

    api_config._settings = None
    api_deps._engine = None
    api_deps._session_factory = None
    outbox: list[dict[str, str]] = []

    async def _capture(_settings, *, recipient: str, purpose: str, token: str) -> None:
        outbox.append({"recipient": recipient, "purpose": purpose, "token": token})

    monkeypatch.setattr(auth_router, "send_auth_email", _capture)
    app = create_app()
    app.dependency_overrides[get_queue] = lambda: _Queue()
    with TestClient(app, headers={"Origin": "http://localhost:3000"}) as client:
        client.outbox = outbox  # type: ignore[attr-defined]
        client.database_url = clean_pg_database_url  # type: ignore[attr-defined]
        yield client
    app.dependency_overrides.clear()
    api_config._settings = None
    api_deps._engine = None
    api_deps._session_factory = None


def _register_and_login(client: TestClient, email: str):
    response = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": PASSWORD, "display_name": "Researcher"},
    )
    assert response.status_code == 202, response.text
    token = client.outbox[-1]["token"]  # type: ignore[attr-defined]
    assert client.post("/api/v1/auth/verify-email", json={"token": token}).status_code == 200
    login = client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert login.status_code == 200, login.text
    return login


def _seed_session(
    database_url: str,
    email: str,
    raw_token: str,
    *,
    expires_at: datetime | None = None,
    last_seen_at: datetime | None = None,
) -> str:
    async def _seed():
        engine = make_engine(database_url)
        factory = make_session_factory(engine)
        try:
            async with factory() as session:
                user = await create_user(
                    session,
                    email=email,
                    password_hash="!test-only",
                    verified=True,
                )
                auth_session = await create_user_session(
                    session,
                    user_id=user.id,
                    token_hash=hashlib.sha256(raw_token.encode()).hexdigest(),
                    expires_at=expires_at or datetime.now(UTC) + timedelta(days=30),
                )
                if last_seen_at is not None:
                    auth_session.last_seen_at = last_seen_at
                await session.commit()
                return str(user.id)
        finally:
            await engine.dispose()

    return run_async(_seed())


def test_anonymous_business_api_is_rejected(auth_client: TestClient) -> None:
    assert auth_client.get("/api/v1/projects").status_code == 401
    assert auth_client.get("/api/v1/settings").status_code == 401


def test_unsafe_cross_origin_request_is_rejected(auth_client: TestClient) -> None:
    response = auth_client.post(
        "/api/v1/auth/login",
        headers={"Origin": "https://attacker.example"},
        json={"email": "nobody@example.com", "password": PASSWORD},
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "origin_not_allowed"
    missing = auth_client.post(
        "/api/v1/auth/login",
        headers={"Origin": ""},
        json={"email": "nobody@example.com", "password": PASSWORD},
    )
    assert missing.status_code == 403


def test_register_verify_login_me_and_logout(auth_client: TestClient) -> None:
    login = _register_and_login(auth_client, "User@Example.com")
    cookie = login.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=lax" in cookie
    assert "path=/" in cookie
    me = auth_client.get("/api/v1/auth/me")
    assert me.status_code == 200
    assert me.json()["email"] == "user@example.com"
    assert me.json()["email_verified"] is True
    logout = auth_client.post("/api/v1/auth/logout")
    assert logout.status_code == 204
    assert "max-age=0" in logout.headers["set-cookie"].lower()
    assert auth_client.get("/api/v1/auth/me").status_code == 401


def test_development_admin_login_uses_a_real_scoped_session(auth_client: TestClient) -> None:
    login = auth_client.post("/api/v1/auth/login", json={"email": "admin", "password": "123456"})
    assert login.status_code == 200, login.text
    user = login.json()["user"]
    uuid.UUID(user["id"])
    assert user["email"] == "admin@paperforge.local"
    assert user["display_name"] == "Admin"
    assert user["email_verified"] is True
    assert "paperforge_session=" in login.headers["set-cookie"]
    assert auth_client.get("/api/v1/auth/me").status_code == 200
    project = auth_client.post(
        "/api/v1/projects",
        json={"title": "Admin project", "paper_type": "review"},
    )
    assert project.status_code == 201, project.text


def test_development_admin_login_rejects_wrong_password(auth_client: TestClient) -> None:
    response = auth_client.post("/api/v1/auth/login", json={"email": "admin", "password": "wrong"})
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "invalid_credentials"


def test_development_admin_login_can_be_disabled(auth_client: TestClient) -> None:
    from paperforge_api.config import get_settings

    settings = get_settings()
    settings.auth_dev_login_enabled = False
    try:
        response = auth_client.post(
            "/api/v1/auth/login", json={"email": "admin", "password": "123456"}
        )
    finally:
        settings.auth_dev_login_enabled = True
    assert response.status_code == 401


def test_development_admin_login_is_forbidden_on_secure_sites(monkeypatch) -> None:
    import paperforge_api.config as api_config

    monkeypatch.setenv("AUTH_DEV_LOGIN_ENABLED", "true")
    monkeypatch.setenv("AUTH_COOKIE_SECURE", "true")
    monkeypatch.setenv("AUTH_EMAIL_MODE", "smtp")
    monkeypatch.setenv("PUBLIC_APP_URL", "https://paperforge.example")
    api_config._settings = None
    try:
        with pytest.raises(RuntimeError, match="AUTH_DEV_LOGIN_ENABLED"):
            create_app()
    finally:
        api_config._settings = None


def test_settings_exposes_status_not_internal_addresses(auth_client: TestClient) -> None:
    _register_and_login(auth_client, "settings@example.com")
    payload = auth_client.get("/api/v1/settings").json()
    assert "texd_url" not in payload
    assert "visuald_url" not in payload
    assert "llm_base_url" not in payload
    assert "image_base_url" not in payload
    assert "image_api_key" not in payload
    assert "image_account_id" not in payload
    assert "scholar_contact_email" not in payload
    assert isinstance(payload["scholar_contact_email_configured"], bool)
    assert payload["image_provider"] == "yunwu"
    assert payload["image_model"] == "gpt-image-1"
    assert payload["image_provider_configured"] is False


def test_weak_password_and_unverified_login_are_rejected(auth_client: TestClient) -> None:
    weak = auth_client.post(
        "/api/v1/auth/register", json={"email": "weak@example.com", "password": "short"}
    )
    assert weak.status_code == 422
    registered = auth_client.post(
        "/api/v1/auth/register",
        json={"email": "pending@example.com", "password": PASSWORD},
    )
    assert registered.status_code == 202
    login = auth_client.post(
        "/api/v1/auth/login", json={"email": "pending@example.com", "password": PASSWORD}
    )
    assert login.status_code == 403
    assert login.json()["detail"]["code"] == "email_verification_required"


def test_password_policy_allows_unicode_and_spaces_but_blocks_common_passwords(
    auth_client: TestClient,
) -> None:
    accepted = auth_client.post(
        "/api/v1/auth/register",
        json={"email": "unicode@example.com", "password": "中文密码 可以包含空格 123456"},
    )
    assert accepted.status_code == 202
    too_short = auth_client.post(
        "/api/v1/auth/register",
        json={"email": "short@example.com", "password": "abc 123"},
    )
    assert too_short.status_code == 422
    rejected = auth_client.post(
        "/api/v1/auth/register",
        json={"email": "common@example.com", "password": "passwordpassword"},
    )
    assert rejected.status_code == 422


def test_password_reset_token_is_single_use_and_revokes_sessions(auth_client: TestClient) -> None:
    _register_and_login(auth_client, "reset@example.com")
    requested = auth_client.post(
        "/api/v1/auth/forgot-password", json={"email": "reset@example.com"}
    )
    assert requested.status_code == 202
    token = auth_client.outbox[-1]["token"]  # type: ignore[attr-defined]
    reset = auth_client.post(
        "/api/v1/auth/reset-password",
        json={"token": token, "new_password": NEW_PASSWORD},
    )
    assert reset.status_code == 200
    assert auth_client.get("/api/v1/auth/me").status_code == 401
    assert (
        auth_client.post(
            "/api/v1/auth/reset-password",
            json={"token": token, "new_password": "another very long replacement password"},
        ).status_code
        == 422
    )
    assert (
        auth_client.post(
            "/api/v1/auth/login", json={"email": "reset@example.com", "password": PASSWORD}
        ).status_code
        == 401
    )
    assert (
        auth_client.post(
            "/api/v1/auth/login",
            json={"email": "reset@example.com", "password": NEW_PASSWORD},
        ).status_code
        == 200
    )


def test_change_password_revokes_current_session(auth_client: TestClient) -> None:
    _register_and_login(auth_client, "change@example.com")
    changed = auth_client.post(
        "/api/v1/auth/change-password",
        json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
    )
    assert changed.status_code == 200
    assert auth_client.get("/api/v1/auth/me").status_code == 401
    assert (
        auth_client.post(
            "/api/v1/auth/login",
            json={"email": "change@example.com", "password": NEW_PASSWORD},
        ).status_code
        == 200
    )


@pytest.mark.parametrize(
    ("email", "expires_at", "last_seen_at"),
    [
        ("absolute@example.com", datetime.now(UTC) - timedelta(seconds=1), None),
        (
            "idle@example.com",
            datetime.now(UTC) + timedelta(days=30),
            datetime.now(UTC) - timedelta(days=8),
        ),
    ],
)
def test_expired_sessions_are_rejected(
    auth_client: TestClient,
    email: str,
    expires_at: datetime,
    last_seen_at: datetime | None,
) -> None:
    raw_token = f"expired-session-{email}"
    _seed_session(
        auth_client.database_url,  # type: ignore[attr-defined]
        email,
        raw_token,
        expires_at=expires_at,
        last_seen_at=last_seen_at,
    )
    auth_client.cookies.set("paperforge_session", raw_token)
    assert auth_client.get("/api/v1/auth/me").status_code == 401


def test_rate_limiter_failure_closes_auth_writes(auth_client: TestClient) -> None:
    from paperforge_api.config import get_settings

    settings = get_settings()
    settings.auth_rate_limit_enabled = True
    try:
        response = auth_client.post(
            "/api/v1/auth/register",
            json={"email": "rate@example.com", "password": PASSWORD},
        )
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "rate_limiter_unavailable"
    finally:
        settings.auth_rate_limit_enabled = False


def test_rate_limiter_applies_independent_ip_budget(auth_client: TestClient) -> None:
    from paperforge_api.config import get_settings

    settings = get_settings()
    settings.auth_rate_limit_enabled = True
    queue = _CountingQueue()
    auth_client.app.dependency_overrides[get_queue] = lambda: queue
    try:
        statuses = [
            auth_client.post(
                "/api/v1/auth/register",
                json={"email": f"rate-{index}@example.com", "password": PASSWORD},
            ).status_code
            for index in range(6)
        ]
        assert statuses[:5] == [202] * 5
        assert statuses[5] == 429
    finally:
        settings.auth_rate_limit_enabled = False


@pytest.mark.parametrize(
    ("method", "suffix"),
    [
        ("get", ""),
        ("get", "/library"),
        ("get", "/assets"),
        ("get", "/visuals"),
        ("get", "/outline"),
        ("get", "/sections"),
        ("get", "/exports"),
        ("get", "/quality"),
        ("get", "/versions"),
        ("get", "/cost/detail"),
        ("get", "/jobs/00000000-0000-0000-0000-000000000002/events"),
        ("post", "/visuals/suggest"),
        ("post", "/outline/generate"),
    ],
)
def test_foreign_project_is_hidden_across_all_routers(
    auth_client: TestClient, method: str, suffix: str
) -> None:
    _register_and_login(auth_client, "owner@example.com")
    project = auth_client.post(
        "/api/v1/projects",
        json={"title": "Private", "paper_type": "review", "citation_style": "author_year"},
    ).json()
    foreign_token = "foreign-session-token"
    _seed_session(
        auth_client.database_url,
        "foreign@example.com",
        foreign_token,  # type: ignore[attr-defined]
    )
    auth_client.cookies.clear()
    auth_client.cookies.set("paperforge_session", foreign_token)
    response = getattr(auth_client, method)(f"/api/v1/projects/{project['id']}{suffix}")
    assert response.status_code == 404, response.text
    assert auth_client.get("/api/v1/projects").json() == []
