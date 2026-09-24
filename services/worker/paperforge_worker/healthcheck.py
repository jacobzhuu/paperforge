"""容器健康检查：队列和对象存储都答话，worker 才算健康。

只探 Redis 是不够的。对象存储掉线的 worker 照样健康、照样领任务，然后每条
生成都在 INGEST 崩掉——而 SEARCH/SCREEN 的钱已经花完了。2026-08-03 共享
MinIO 容器因 snap 刷新退出后停了 11 天，其间还滚过 5 次部署，没有任何一处
断言过这个依赖，直到用户的项目页面上出现一句 `NameResolutionError: minio`。

把依赖探测放进健康检查，这类停摆一个 interval 内就会显示在 `docker ps` 上。
"""

from __future__ import annotations

import os
import signal
import socket
import sys
from typing import Any
from urllib.parse import urlparse

DEFAULT_TIMEOUT_SECONDS = 2.0
# 封顶要同时满足两头：留得住 MinIO 客户端自己的失败（DNS 解析不了要重试 5 次，
# 约 6.5s，而那条 NameResolutionError 正是最有用的诊断），又要小于 compose 里的
# healthcheck timeout——被容器运行时 kill 掉的探测只留下一个「unhealthy」，不留原因。
DEFAULT_DEADLINE_SECONDS = 8.0


def enforce_deadline(seconds: float = DEFAULT_DEADLINE_SECONDS) -> None:
    """给整轮探测封顶，并且封得住。

    MinIO 客户端的连接/读超时是 5 分钟并且自带 5 次重试：端口被黑洞掉（路由不可达，
    而不是 DNS 解析失败）时，`bucket_exists` 能挂十几分钟。

    这里不能只 raise：urllib3 把 `OSError`（`TimeoutError` 正是其子类）当成可重试
    的连接错误，异常被吞掉之后它接着重试，探测照样挂住——实测超过 120s。探测进程
    没有别的活要干，超时就直接硬退出，谁也吞不掉。
    """

    def _expire(_signum: int, _frame: Any) -> None:
        print(f"dependency probe exceeded {seconds:g}s", file=sys.stderr)
        sys.stderr.flush()
        os._exit(1)

    signal.signal(signal.SIGALRM, _expire)
    signal.setitimer(signal.ITIMER_REAL, seconds)


def check_queue(redis_url: str, *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> None:
    """任务队列可连。arq 自己会重连，这里只回答「现在能不能领到活」。"""
    parsed = urlparse(redis_url)
    if not parsed.hostname:
        raise RuntimeError(f"REDIS_URL has no host: {redis_url}")
    socket.create_connection((parsed.hostname, parsed.port or 6379), timeout=timeout).close()


def check_object_store(settings: Any) -> None:
    """对象存储可达且桶还在。构造 store 本身就是探测——失败即不可用。"""
    from storage import make_object_store

    make_object_store(settings).probe()


def run_checks(settings: Any) -> list[str]:
    """返回失败依赖的说明；空列表代表健康。"""
    failures: list[str] = []
    for name, check in (
        ("queue", lambda: check_queue(str(settings.redis_url))),
        ("object store", lambda: check_object_store(settings)),
    ):
        try:
            check()
        except Exception as error:  # noqa: BLE001 - 任何异常都意味着依赖不可用
            failures.append(f"{name} unavailable: {type(error).__name__}: {error}")
    return failures


def main() -> int:
    from paperforge_worker.config import get_settings

    # 导入配置本身要 ~2s，计时从探测真正开始的地方起算。
    settings = get_settings()
    enforce_deadline()
    failures = run_checks(settings)
    for failure in failures:
        print(failure, file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
