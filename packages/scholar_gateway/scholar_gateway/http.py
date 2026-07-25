"""合规 HTTP 抓取客户端（OA 全文获取专用）。

收敛自 DeepSearch acquisition/http_client.py（设计 §3.2）：保留 SSRF 防护、
逐跳重定向校验、超时/重定向次数/响应大小上限、礼貌 UA；
去掉浏览器回退、域熔断账本与 OSINT 专属失败分类。

合规红线（设计 §1.4）：只抓 OA/官方渠道给出的 URL，不做任何 paywall 绕过、
不伪装 UA、不忽略 robots 语义——URL 由 fulltext.py 的规划器从 OA 元数据产出。
"""

from __future__ import annotations

import hashlib
import ipaddress
import socket
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urljoin, urlsplit

import httpx

BLOCKED_HOSTNAMES = frozenset({"localhost", "metadata", "metadata.google.internal"})
ALLOWED_SCHEMES = frozenset({"http", "https"})
DEFAULT_MAX_RESPONSE_BYTES = 32 * 1024 * 1024


class HostResolver(Protocol):
    def resolve(self, host: str, port: int) -> tuple[str, ...]: ...


class SocketHostResolver:
    def resolve(self, host: str, port: int) -> tuple[str, ...]:
        addresses: list[str] = []
        seen: set[str] = set()
        for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
            sockaddr = info[4]
            if not sockaddr:
                continue
            address = str(sockaddr[0])
            if address in seen:
                continue
            seen.add(address)
            addresses.append(address)
        return tuple(addresses)


@dataclass(frozen=True)
class HttpFetchResult:
    requested_url: str
    final_url: str | None
    http_status: int | None
    error_code: str | None
    mime_type: str | None
    content: bytes | None
    content_hash: str | None
    trace: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.error_code is None and bool(self.content)


class HttpPolicyError(Exception):
    def __init__(self, *, error_code: str, trace: dict[str, Any]) -> None:
        super().__init__(error_code)
        self.error_code = error_code
        self.trace = trace


def normalize_mime_type(content_type: str | None) -> str:
    if content_type is None:
        return "application/octet-stream"
    mime_type = content_type.split(";", 1)[0].strip().lower()
    return mime_type or "application/octet-stream"


def is_blocked_ip(address: str) -> bool:
    """非全局地址（回环/私网/链路本地/保留）一律拒绝，防 SSRF。"""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return True
    return not ip.is_global


class SafeHttpClient:
    """带 SSRF 与体积防护的 GET 客户端。"""

    def __init__(
        self,
        *,
        user_agent: str,
        timeout_seconds: float = 30.0,
        max_redirects: int = 5,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        accept_language: str = "en-US,en;q=0.9",
        resolver: HostResolver | None = None,
        client: httpx.Client | None = None,
        trust_env_proxy: bool = False,
    ) -> None:
        self.user_agent = user_agent
        self.timeout_seconds = timeout_seconds
        self.max_redirects = max(0, int(max_redirects))
        self.max_response_bytes = max(1, int(max_response_bytes))
        self.accept_language = accept_language
        self.resolver = resolver or SocketHostResolver()
        self._client = client
        self._owns_client = client is None
        self.trust_env_proxy = trust_env_proxy

    def __enter__(self) -> SafeHttpClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                follow_redirects=False,
                trust_env=self.trust_env_proxy,
                timeout=self.timeout_seconds,
            )
        return self._client

    def fetch(self, url: str, *, accept: str | None = None) -> HttpFetchResult:
        trace: dict[str, Any] = {"redirect_chain": []}
        current = url
        try:
            for hop in range(self.max_redirects + 1):
                validation = self._validate_target(current)
                trace.setdefault("targets", []).append(validation)
                response = self.client.get(
                    current,
                    headers=self._headers(accept),
                    timeout=self.timeout_seconds,
                    follow_redirects=False,
                )
                if response.is_redirect and hop < self.max_redirects:
                    location = response.headers.get("location")
                    if not location:
                        raise HttpPolicyError(
                            error_code="redirect_without_location",
                            trace={"url": current, "status": response.status_code},
                        )
                    response.close()
                    current = urljoin(current, location)
                    trace["redirect_chain"].append(current)
                    continue
                if response.is_redirect:
                    raise HttpPolicyError(
                        error_code="too_many_redirects",
                        trace={"redirect_chain": trace["redirect_chain"]},
                    )
                content = self._read_capped(response)
                mime_type = normalize_mime_type(response.headers.get("content-type"))
                if response.status_code >= 400:
                    return HttpFetchResult(
                        requested_url=url,
                        final_url=current,
                        http_status=response.status_code,
                        error_code="http_error",
                        mime_type=mime_type,
                        content=None,
                        content_hash=None,
                        trace=trace,
                    )
                return HttpFetchResult(
                    requested_url=url,
                    final_url=current,
                    http_status=response.status_code,
                    error_code=None,
                    mime_type=mime_type,
                    content=content,
                    content_hash=hashlib.sha256(content).hexdigest(),
                    trace={**trace, "bytes": len(content)},
                )
            raise HttpPolicyError(
                error_code="too_many_redirects",
                trace={"redirect_chain": trace["redirect_chain"]},
            )
        except HttpPolicyError as error:
            return HttpFetchResult(
                requested_url=url,
                final_url=current,
                http_status=None,
                error_code=error.error_code,
                mime_type=None,
                content=None,
                content_hash=None,
                trace={**trace, **error.trace},
            )
        except httpx.TimeoutException:
            return HttpFetchResult(
                requested_url=url,
                final_url=current,
                http_status=None,
                error_code="timeout",
                mime_type=None,
                content=None,
                content_hash=None,
                trace=trace,
            )
        except httpx.HTTPError as error:
            return HttpFetchResult(
                requested_url=url,
                final_url=current,
                http_status=None,
                error_code="request_error",
                mime_type=None,
                content=None,
                content_hash=None,
                trace={**trace, "error": type(error).__name__},
            )

    def _headers(self, accept: str | None) -> dict[str, str]:
        headers = {
            "User-Agent": self.user_agent,
            "Accept-Language": self.accept_language,
        }
        if accept:
            headers["Accept"] = accept
        return headers

    def _read_capped(self, response: httpx.Response) -> bytes:
        declared = response.headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > self.max_response_bytes:
                    response.close()
                    raise HttpPolicyError(
                        error_code="response_too_large",
                        trace={"declared_bytes": int(declared), "cap": self.max_response_bytes},
                    )
            except ValueError:
                pass
        content = response.content
        if len(content) > self.max_response_bytes:
            raise HttpPolicyError(
                error_code="response_too_large",
                trace={"bytes": len(content), "cap": self.max_response_bytes},
            )
        return content

    def _validate_target(self, url: str) -> dict[str, Any]:
        parsed = urlsplit(url)
        scheme = (parsed.scheme or "").lower()
        if scheme not in ALLOWED_SCHEMES:
            raise HttpPolicyError(
                error_code="blocked_scheme",
                trace={"url": url, "scheme": scheme},
            )
        host = (parsed.hostname or "").lower().rstrip(".")
        if not host:
            raise HttpPolicyError(error_code="invalid_url", trace={"url": url})
        if host in BLOCKED_HOSTNAMES:
            raise HttpPolicyError(
                error_code="blocked_hostname",
                trace={"url": url, "host": host},
            )
        port = parsed.port or (443 if scheme == "https" else 80)
        try:
            literal_ip = ipaddress.ip_address(host)
        except ValueError:
            literal_ip = None
        if literal_ip is not None:
            resolved = (str(literal_ip),)
        else:
            try:
                resolved = self.resolver.resolve(host, port)
            except OSError as error:
                raise HttpPolicyError(
                    error_code="dns_resolution_failed",
                    trace={"host": host, "error": type(error).__name__},
                ) from error
        if not resolved:
            raise HttpPolicyError(
                error_code="dns_resolution_failed",
                trace={"host": host, "reason": "no_addresses"},
            )
        blocked = [address for address in resolved if is_blocked_ip(address)]
        if blocked:
            raise HttpPolicyError(
                error_code="blocked_ip",
                trace={"host": host, "blocked_ips": blocked, "resolved_ips": list(resolved)},
            )
        return {"host": host, "resolved_ips": list(resolved)}
