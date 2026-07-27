"""HTTP 缓存后端测试（缓存键稳定性 + TTL + 只缓存 200）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from scholar_gateway import (
    InMemoryHttpCache,
    NullHttpCache,
    SqlAlchemyHttpCache,
    canonical_request_url,
)
from scholar_gateway.cache import hash_canonical_url
from sqlalchemy import DateTime, Integer, LargeBinary, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


class _CacheBase(DeclarativeBase):
    pass


class HttpCacheRow(_CacheBase):
    """与 db.models.library.ScholarlyHttpCache 等价的 sqlite 可测模型。"""

    __tablename__ = "scholarly_http_cache"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    url_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(128))
    body: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    provider_name: Mapped[str | None] = mapped_column(String(32))


def test_canonical_url_is_parameter_order_independent() -> None:
    left = canonical_request_url("https://api.example.org/works", {"b": 2, "a": 1})
    right = canonical_request_url("https://api.example.org/works?a=1", {"b": 2})
    assert left == right
    assert hash_canonical_url(left) == hash_canonical_url(right)


def test_canonical_url_drops_none_values_and_expands_sequences() -> None:
    url = canonical_request_url("https://api.example.org/x", {"a": None, "b": [1, 2]})
    assert url == "https://api.example.org/x?b=1&b=2"


def test_in_memory_cache_round_trip() -> None:
    cache = InMemoryHttpCache()
    assert cache.get(url="https://x.org/a", params=None, ttl_hours=1.0) is None

    cache.put(
        url="https://x.org/a",
        params={"q": "test"},
        status_code=200,
        content_type="application/json",
        body=b'{"ok": true}',
        provider_name="crossref",
    )
    hit = cache.get(url="https://x.org/a", params={"q": "test"}, ttl_hours=1.0)
    assert hit is not None
    assert hit.cache_hit is True
    assert hit.text == '{"ok": true}'
    assert hit.provider_name == "crossref"


def test_in_memory_cache_respects_ttl() -> None:
    cache = InMemoryHttpCache()
    stale = datetime.now(UTC) - timedelta(hours=5)
    cache.put(
        url="https://x.org/a",
        params=None,
        status_code=200,
        content_type=None,
        body=b"old",
        now=stale,
    )
    assert cache.get(url="https://x.org/a", params=None, ttl_hours=1.0) is None
    assert cache.get(url="https://x.org/a", params=None, ttl_hours=24.0) is not None


def test_non_200_responses_are_not_cached() -> None:
    cache = InMemoryHttpCache()
    assert (
        cache.put(
            url="https://x.org/a",
            params=None,
            status_code=429,
            content_type=None,
            body=b"rate limited",
        )
        is None
    )
    assert cache.get(url="https://x.org/a", params=None, ttl_hours=1.0) is None


def test_zero_ttl_disables_reads() -> None:
    cache = InMemoryHttpCache()
    cache.put(url="https://x.org/a", params=None, status_code=200, content_type=None, body=b"x")
    assert cache.get(url="https://x.org/a", params=None, ttl_hours=0.0) is None


def test_null_cache_never_stores() -> None:
    cache = NullHttpCache()
    assert (
        cache.put(url="https://x.org/a", params=None, status_code=200, content_type=None, body=b"x")
        is None
    )
    assert cache.get(url="https://x.org/a", params=None, ttl_hours=99.0) is None


def test_sqlalchemy_cache_backend_round_trip_and_ttl() -> None:
    """SQLAlchemy 后端逻辑用等价的 sqlite 模型验证（表结构见 db.models.library）。"""
    engine = create_engine("sqlite://")
    _CacheBase.metadata.create_all(engine)
    cache = SqlAlchemyHttpCache(sessionmaker(engine), model=HttpCacheRow)

    assert cache.get(url="https://x.org/a", params=None, ttl_hours=1.0) is None
    cache.put(
        url="https://x.org/a",
        params={"q": 1},
        status_code=200,
        content_type="application/json",
        body=b'{"ok":1}',
        provider_name="openalex",
    )
    hit = cache.get(url="https://x.org/a", params={"q": 1}, ttl_hours=1.0)
    assert hit is not None and hit.cache_hit is True and hit.text == '{"ok":1}'

    # 同一缓存键再写一次是覆盖而非重复插入。
    cache.put(
        url="https://x.org/a",
        params={"q": 1},
        status_code=200,
        content_type="application/json",
        body=b'{"ok":2}',
        provider_name="openalex",
    )
    refreshed = cache.get(url="https://x.org/a", params={"q": 1}, ttl_hours=1.0)
    assert refreshed is not None and refreshed.text == '{"ok":2}'

    # TTL 之外读不到（时钟前移等价于条目过期）。
    stale_clock = datetime.now(UTC) + timedelta(hours=100)
    assert cache.get(url="https://x.org/a", params={"q": 1}, ttl_hours=1.0, now=stale_clock) is None


def test_sqlalchemy_cache_backend_ignores_non_200() -> None:
    engine = create_engine("sqlite://")
    _CacheBase.metadata.create_all(engine)
    cache = SqlAlchemyHttpCache(sessionmaker(engine), model=HttpCacheRow)
    assert (
        cache.put(
            url="https://x.org/b",
            params=None,
            status_code=500,
            content_type=None,
            body=b"boom",
        )
        is None
    )
    assert cache.get(url="https://x.org/b", params=None, ttl_hours=1.0) is None
