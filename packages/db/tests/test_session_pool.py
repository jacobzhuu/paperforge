from __future__ import annotations

import db.session as session_module


def test_engine_pool_is_bounded_for_blue_green_draining(monkeypatch) -> None:
    captured: dict = {}
    sentinel = object()

    def _create(url, **kwargs):
        captured.update({"url": url, **kwargs})
        return sentinel

    monkeypatch.setattr(session_module, "create_async_engine", _create)
    engine = session_module.make_engine(
        "postgresql+asyncpg://example/db",
        application_name="paperforge-worker",
    )

    assert engine is sentinel
    assert captured["pool_size"] == 2
    assert captured["max_overflow"] == 6
    assert captured["pool_use_lifo"] is True
    assert captured["connect_args"] == {
        "server_settings": {"application_name": "paperforge-worker"}
    }
