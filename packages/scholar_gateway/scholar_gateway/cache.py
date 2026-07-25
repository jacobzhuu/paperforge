"""学术 API 响应的 TTL 缓存。

迁移自 DeepSearch literature_review/http_cache.py + ``scholarly_http_cache`` 表
（设计 §3.1「保留表结构」）。改动：从「直接吃 SQLAlchemy Session」解耦为
可注入的 ``HttpCacheBackend`` 协议，提供 Null / InMemory / SQLAlchemy 三种实现，
使同步 provider 适配器不必绑定异步 DB 会话。

只缓存 HTTP 200 响应体。OA 全文 PDF 抓取**不得**走这个缓存（体积与版权语义不同）。
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


@dataclass(frozen=True)
class CachedHttpResponse:
    url: str
    url_hash: str
    status_code: int
    content_type: str | None
    body: bytes
    retrieved_at: datetime
    provider_name: str | None
    cache_hit: bool = True

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


def canonical_request_url(url: str, params: Mapping[str, Any] | None = None) -> str:
    """构造查询参数排序后的稳定 URL 串，用于缓存键哈希。"""
    parsed = urlsplit(url)
    items: list[tuple[str, str]] = []
    if parsed.query:
        items.extend(parse_qsl(parsed.query, keep_blank_values=True))
    if params:
        for key, value in params.items():
            if value is None:
                continue
            if isinstance(value, list | tuple):
                for item in value:
                    items.append((str(key), str(item)))
            else:
                items.append((str(key), str(value)))
    items.sort(key=lambda pair: (pair[0], pair[1]))
    query = urlencode(items, doseq=True)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ""))


def hash_canonical_url(canonical_url: str) -> str:
    return hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()


class HttpCacheBackend(Protocol):
    """同步缓存后端。适配器在工作线程内调用，故不使用 async 接口。"""

    def get(
        self,
        *,
        url: str,
        params: Mapping[str, Any] | None,
        ttl_hours: float,
        now: datetime | None = None,
    ) -> CachedHttpResponse | None: ...

    def put(
        self,
        *,
        url: str,
        params: Mapping[str, Any] | None,
        status_code: int,
        content_type: str | None,
        body: bytes,
        provider_name: str | None = None,
        now: datetime | None = None,
    ) -> CachedHttpResponse | None: ...


class NullHttpCache:
    """默认后端：不缓存（单测与一次性调用）。"""

    def get(
        self,
        *,
        url: str,
        params: Mapping[str, Any] | None,
        ttl_hours: float,
        now: datetime | None = None,
    ) -> CachedHttpResponse | None:
        return None

    def put(
        self,
        *,
        url: str,
        params: Mapping[str, Any] | None,
        status_code: int,
        content_type: str | None,
        body: bytes,
        provider_name: str | None = None,
        now: datetime | None = None,
    ) -> CachedHttpResponse | None:
        return None


class InMemoryHttpCache:
    """进程内缓存：单次 worker 任务里多阶段复用同一 provider 响应。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._entries: dict[str, CachedHttpResponse] = {}

    def get(
        self,
        *,
        url: str,
        params: Mapping[str, Any] | None,
        ttl_hours: float,
        now: datetime | None = None,
    ) -> CachedHttpResponse | None:
        if ttl_hours <= 0:
            return None
        digest = hash_canonical_url(canonical_request_url(url, params))
        clock = now or datetime.now(UTC)
        with self._lock:
            entry = self._entries.get(digest)
            if entry is None:
                return None
            if clock - entry.retrieved_at > timedelta(hours=ttl_hours):
                return None
            return replace(entry, cache_hit=True)

    def put(
        self,
        *,
        url: str,
        params: Mapping[str, Any] | None,
        status_code: int,
        content_type: str | None,
        body: bytes,
        provider_name: str | None = None,
        now: datetime | None = None,
    ) -> CachedHttpResponse | None:
        if status_code != 200:
            return None
        canonical = canonical_request_url(url, params)
        digest = hash_canonical_url(canonical)
        entry = CachedHttpResponse(
            url=canonical,
            url_hash=digest,
            status_code=status_code,
            content_type=content_type,
            body=bytes(body),
            retrieved_at=now or datetime.now(UTC),
            provider_name=provider_name,
            cache_hit=False,
        )
        with self._lock:
            self._entries[digest] = entry
        return entry

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


class SqlAlchemyHttpCache:
    """``scholarly_http_cache`` 表后端（同步 Session）。

    Session 非线程安全，且并行 provider 检索会并发访问缓存，故整体加锁。
    ``session_factory`` 每次调用返回一个新的同步 Session（用完即关）。
    """

    def __init__(
        self,
        session_factory: Any,
        *,
        model: Any | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._lock = threading.RLock()
        if model is None:
            from db.models.library import ScholarlyHttpCache  # 延迟导入：避免包环依赖

            model = ScholarlyHttpCache
        self._model = model

    def get(
        self,
        *,
        url: str,
        params: Mapping[str, Any] | None,
        ttl_hours: float,
        now: datetime | None = None,
    ) -> CachedHttpResponse | None:
        if ttl_hours <= 0:
            return None
        from sqlalchemy import select

        digest = hash_canonical_url(canonical_request_url(url, params))
        clock = now or datetime.now(UTC)
        with self._lock, self._session_factory() as session:
            row = session.scalar(select(self._model).where(self._model.url_hash == digest))
            if row is None:
                return None
            retrieved = row.retrieved_at
            if retrieved.tzinfo is None:
                retrieved = retrieved.replace(tzinfo=UTC)
            if clock - retrieved > timedelta(hours=ttl_hours):
                return None
            return CachedHttpResponse(
                url=row.url,
                url_hash=row.url_hash,
                status_code=row.status_code,
                content_type=row.content_type,
                body=bytes(row.body),
                retrieved_at=retrieved,
                provider_name=row.provider_name,
                cache_hit=True,
            )

    def put(
        self,
        *,
        url: str,
        params: Mapping[str, Any] | None,
        status_code: int,
        content_type: str | None,
        body: bytes,
        provider_name: str | None = None,
        now: datetime | None = None,
    ) -> CachedHttpResponse | None:
        if status_code != 200:
            return None
        from sqlalchemy import select

        canonical = canonical_request_url(url, params)
        digest = hash_canonical_url(canonical)
        clock = now or datetime.now(UTC)
        with self._lock, self._session_factory() as session:
            row = session.scalar(select(self._model).where(self._model.url_hash == digest))
            if row is None:
                row = self._model(
                    url_hash=digest,
                    url=canonical,
                    status_code=status_code,
                    content_type=content_type,
                    body=bytes(body),
                    retrieved_at=clock,
                    provider_name=provider_name,
                )
                session.add(row)
            else:
                row.url = canonical
                row.status_code = status_code
                row.content_type = content_type
                row.body = bytes(body)
                row.retrieved_at = clock
                row.provider_name = provider_name
            session.commit()
            return CachedHttpResponse(
                url=canonical,
                url_hash=digest,
                status_code=status_code,
                content_type=content_type,
                body=bytes(body),
                retrieved_at=clock,
                provider_name=provider_name,
                cache_hit=False,
            )
