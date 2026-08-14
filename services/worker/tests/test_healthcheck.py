"""容器健康检查必须真的断言外部依赖，而不只是队列。

回归背景：共享 MinIO 容器退出后，只探 Redis 的 worker 一直报告 healthy，
11 天里每条依赖对象存储的生成都在 INGEST 崩掉。
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any

import pytest
from paperforge_worker.healthcheck import check_object_store, check_queue, run_checks


@dataclass
class _Settings:
    redis_url: str
    storage_backend: str
    storage_fs_root: str = ""


@pytest.fixture
def listening_port() -> Any:
    server = socket.create_server(("127.0.0.1", 0))
    try:
        yield server.getsockname()[1]
    finally:
        server.close()


def test_queue_check_reports_an_unreachable_queue(listening_port: int) -> None:
    check_queue(f"redis://127.0.0.1:{listening_port}/0", timeout=2)
    with pytest.raises(RuntimeError, match="no host"):
        check_queue("redis:///0")

    closed = socket.create_server(("127.0.0.1", 0))
    closed_port = closed.getsockname()[1]
    closed.close()
    with pytest.raises(OSError):
        check_queue(f"redis://127.0.0.1:{closed_port}/0", timeout=2)


def test_object_store_check_fails_when_the_backend_is_unusable(tmp_path: Any) -> None:
    check_object_store(
        _Settings(
            redis_url="redis://127.0.0.1:6379/0",
            storage_backend="filesystem",
            storage_fs_root=str(tmp_path / "objects"),
        )
    )
    with pytest.raises(ValueError, match="unsupported storage backend"):
        check_object_store(
            _Settings(redis_url="redis://127.0.0.1:6379/0", storage_backend="nowhere")
        )


def test_health_is_not_reported_when_only_the_queue_answers(
    listening_port: int, tmp_path: Any
) -> None:
    """核心回归：队列可达但对象存储不可用时，容器不得报告健康。"""
    healthy = _Settings(
        redis_url=f"redis://127.0.0.1:{listening_port}/0",
        storage_backend="filesystem",
        storage_fs_root=str(tmp_path / "objects"),
    )
    assert run_checks(healthy) == []

    storage_down = _Settings(
        redis_url=f"redis://127.0.0.1:{listening_port}/0",
        storage_backend="nowhere",
    )
    failures = run_checks(storage_down)
    assert len(failures) == 1
    assert failures[0].startswith("object store unavailable: ValueError")


def test_probe_deadline_survives_a_caller_that_swallows_exceptions() -> None:
    """封顶必须硬退出。

    只 raise 是不够的：urllib3 把 `TimeoutError`（`OSError` 的子类）当成可重试的
    连接错误吞掉再重试，对黑洞端点实测挂过 120s 以上。这里用「调用方 except
    Exception 后继续 sleep」来复现那种吞异常的重试循环。
    """
    program = (
        "from paperforge_worker.healthcheck import enforce_deadline\n"
        "import time\n"
        "enforce_deadline(0.2)\n"
        "for _ in range(3):\n"
        "    try:\n"
        "        time.sleep(2)\n"
        "    except Exception:\n"
        "        pass\n"
        "print('probe was not bounded')\n"
    )
    started = time.monotonic()
    result = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
    )
    assert result.returncode == 1
    assert "exceeded 0.2s" in result.stderr
    assert "probe was not bounded" not in result.stdout
    assert time.monotonic() - started < 5
