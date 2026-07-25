"""合规 HTTP 客户端 + OA 全文规划/抓取测试。

覆盖设计 §1.4 合规红线的技术实现：SSRF 防护、逐跳重定向校验、大小上限，
以及「只走 OA/官方渠道 URL」的规划顺序。
"""

from __future__ import annotations

import httpx
import pytest
from scholar_gateway import (
    OaFulltextTarget,
    SafeHttpClient,
    acquire_oa_fulltext,
    plan_oa_fulltext,
)
from scholar_gateway.http import BLOCKED_HOSTNAMES, is_blocked_ip

PDF_BYTES = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\ntrailer\n%%EOF\n"


class _StubResolver:
    def __init__(self, mapping: dict[str, tuple[str, ...]]) -> None:
        self.mapping = mapping

    def resolve(self, host: str, port: int) -> tuple[str, ...]:
        if host not in self.mapping:
            raise OSError(f"no such host: {host}")
        return self.mapping[host]


def _client(handler, resolver=None, **kwargs) -> SafeHttpClient:
    return SafeHttpClient(
        user_agent="PaperForge/0.1 (mailto:test@example.com)",
        client=httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False),
        resolver=resolver or _StubResolver({"example.org": ("93.184.216.34",)}),
        **kwargs,
    )


def test_fetch_returns_content_and_hash() -> None:
    client = _client(
        lambda request: httpx.Response(
            200, content=PDF_BYTES, headers={"content-type": "application/pdf"}
        )
    )
    result = client.fetch("https://example.org/paper.pdf")
    assert result.ok
    assert result.mime_type == "application/pdf"
    assert result.content == PDF_BYTES
    assert len(result.content_hash or "") == 64


def test_private_ip_target_is_blocked() -> None:
    client = _client(
        lambda request: httpx.Response(200, content=b"secret"),
        resolver=_StubResolver({"internal.example.org": ("10.0.0.5",)}),
    )
    result = client.fetch("https://internal.example.org/x.pdf")
    assert result.error_code == "blocked_ip"
    assert result.content is None


def test_loopback_hostname_is_blocked_before_dns() -> None:
    client = _client(lambda request: httpx.Response(200, content=b"secret"))
    assert client.fetch("http://localhost:8080/x.pdf").error_code == "blocked_hostname"
    assert "localhost" in BLOCKED_HOSTNAMES


def test_non_http_scheme_is_blocked() -> None:
    client = _client(lambda request: httpx.Response(200, content=b"x"))
    assert client.fetch("file:///etc/passwd").error_code == "blocked_scheme"


def test_redirect_hop_to_private_host_is_blocked() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "example.org":
            return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta"})
        return httpx.Response(200, content=b"cloud-metadata")

    client = _client(handler)
    result = client.fetch("https://example.org/redirect")
    # 每一跳都重新校验，防止重定向绕过 SSRF 防护。
    assert result.error_code == "blocked_ip"


def test_response_larger_than_cap_is_rejected() -> None:
    client = _client(
        lambda request: httpx.Response(200, content=b"x" * 5000),
        max_response_bytes=1024,
    )
    assert client.fetch("https://example.org/big.pdf").error_code == "response_too_large"


def test_http_error_status_is_reported_not_raised() -> None:
    client = _client(lambda request: httpx.Response(403, content=b"paywall"))
    result = client.fetch("https://example.org/paywalled.pdf")
    assert result.error_code == "http_error"
    assert result.http_status == 403
    # 不绕 paywall：403 就是终点，不做任何回退尝试。
    assert result.content is None


def test_is_blocked_ip_rejects_non_global_addresses() -> None:
    assert is_blocked_ip("127.0.0.1")
    assert is_blocked_ip("169.254.169.254")
    assert is_blocked_ip("::1")
    assert not is_blocked_ip("93.184.216.34")


def test_plan_orders_persisted_pdf_then_arxiv_then_pmc() -> None:
    target = OaFulltextTarget(
        work_id="w-1",
        arxiv_id="2401.01234",
        pmcid="7654321",
        links=(
            {
                "url": "https://example.org/best-oa.pdf",
                "url_type": "pdf",
                "source_name": "openalex",
            },
            {"url": "https://example.org/landing", "url_type": "landing_page"},
        ),
    )
    plan = plan_oa_fulltext([target])
    urls = [candidate.url for candidate in plan.candidates]
    assert urls == [
        "https://example.org/best-oa.pdf",
        "https://arxiv.org/pdf/2401.01234.pdf",
        "https://europepmc.org/articles/PMC7654321?pdf=render",
    ]
    # 落地页不是全文入口，不进规划。
    assert all("landing" not in url for url in urls)


def test_plan_records_works_without_oa_candidate() -> None:
    plan = plan_oa_fulltext([OaFulltextTarget(work_id="w-2", doi="10.1000/closed")])
    assert plan.candidates == ()
    assert plan.diagnostics["skipped"] == [
        {"work_id": "w-2", "reason": "no_oa_pdf_candidate"}
    ]


def test_acquire_stops_at_first_success_per_work() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, content=PDF_BYTES, headers={"content-type": "application/pdf"})

    plan = plan_oa_fulltext(
        [OaFulltextTarget(work_id="w-1", arxiv_id="2401.01234", pmcid="PMC1")]
    )
    result = acquire_oa_fulltext(
        plan,
        http_client=_client(
            handler,
            resolver=_StubResolver(
                {"arxiv.org": ("151.101.3.42",), "europepmc.org": ("193.62.193.80",)}
            ),
        ),
    )
    assert len(result.documents) == 1
    assert len(calls) == 1
    assert result.documents[0].mime_type == "application/pdf"


def test_acquire_falls_back_to_next_url_then_records_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b"missing")

    plan = plan_oa_fulltext([OaFulltextTarget(work_id="w-1", arxiv_id="2401.01234")])
    result = acquire_oa_fulltext(
        plan,
        http_client=_client(handler, resolver=_StubResolver({"arxiv.org": ("151.101.3.42",)})),
    )
    # Draft-first：抓不到只记 failure，管线继续走摘要级降级。
    assert result.documents == ()
    assert result.failures == ({"work_id": "w-1", "reason": "all_oa_urls_failed"},)


def test_non_fulltext_mime_is_discarded() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"GIF89a", headers={"content-type": "image/gif"})

    plan = plan_oa_fulltext([OaFulltextTarget(work_id="w-1", arxiv_id="2401.01234")])
    result = acquire_oa_fulltext(
        plan,
        http_client=_client(handler, resolver=_StubResolver({"arxiv.org": ("151.101.3.42",)})),
    )
    assert result.documents == ()


@pytest.mark.parametrize("pmcid", ["7654321", "PMC7654321"])
def test_pmcid_normalization_in_plan(pmcid: str) -> None:
    plan = plan_oa_fulltext([OaFulltextTarget(work_id="w", pmcid=pmcid)])
    assert plan.candidates[0].url == "https://europepmc.org/articles/PMC7654321?pdf=render"
