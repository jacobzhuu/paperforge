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
    # 这条测试守的是「蓝绿排空时不要把 PostgreSQL 的连接数吃光」，而吃光它的是
    # **常驻**连接——也就是 pool_size。它必须一直保持很小。
    assert captured["pool_size"] == 2
    # max_overflow 是**瞬时**容量，用完即关，不影响稳态占用。2026-09-07 从 6 提到 12：
    # 管线并发化之后，每个并发任务的落库与 context.emit 各要占一条，原来的 8 条上限
    # 会表现为 30 秒的 pool_timeout 停顿而不是报错。
    assert captured["max_overflow"] == 12
    assert captured["pool_use_lifo"] is True
    assert captured["connect_args"] == {
        "server_settings": {"application_name": "paperforge-worker"}
    }
