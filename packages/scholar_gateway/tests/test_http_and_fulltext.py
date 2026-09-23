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
    discover_doaj_links,
    discover_unpaywall_links,
    plan_oa_fulltext,
)
from scholar_gateway.fulltext import _looks_like_xml
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


def test_plan_prefers_structured_sources_before_pdf_fallbacks() -> None:
    target = OaFulltextTarget(
        work_id="w-1",
        arxiv_id="2401.01234",
        pmcid="7654321",
        links=(
            {
                "url": "https://example.org/best-oa.pdf",
                "url_type": "pdf",
                "source_name": "openalex",
                "is_oa": True,
            },
            {"url": "https://example.org/landing", "url_type": "landing_page"},
        ),
    )
    plan = plan_oa_fulltext([target])
    urls = [candidate.url for candidate in plan.candidates]
    assert urls == [
        "https://pmc.ncbi.nlm.nih.gov/api/oai/v1/mh/"
        "?verb=GetRecord&identifier=oai:pubmedcentral.nih.gov:7654321&metadataPrefix=pmc",
        "https://arxiv.org/e-print/2401.01234",
        "https://example.org/best-oa.pdf",
        "https://arxiv.org/pdf/2401.01234.pdf",
        "https://europepmc.org/articles/PMC7654321?pdf=render",
    ]
    # 落地页不是全文入口，不进规划。
    assert all("landing" not in url for url in urls)


def test_plan_records_works_without_oa_candidate() -> None:
    plan = plan_oa_fulltext([OaFulltextTarget(work_id="w-2", doi="10.1000/closed")])
    assert plan.candidates == ()
    assert plan.diagnostics["skipped"] == [{"work_id": "w-2", "reason": "no_oa_pdf_candidate"}]


def test_acquire_stops_at_first_success_per_work() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, content=PDF_BYTES, headers={"content-type": "application/pdf"})

    plan = plan_oa_fulltext([OaFulltextTarget(work_id="w-1", arxiv_id="2401.01234", pmcid="PMC1")])
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
    assert result.attempts
    assert all(item.status == "failed" for item in result.attempts)


def test_jats_bytes_are_detected_and_a_failed_work_does_not_abort_the_batch() -> None:
    assert _looks_like_xml(b"  <?xml version='1.0'?><article><body>ok</body></article>")

    def handler(request: httpx.Request) -> httpx.Response:
        if "2401.00001" in str(request.url):
            return httpx.Response(200, content=b"<html>not a paper</html>")
        return httpx.Response(
            200,
            content=b"<?xml version='1.0'?><article><body>JATS text</body></article>",
            headers={"content-type": "application/xml"},
        )

    plan = plan_oa_fulltext(
        [
            OaFulltextTarget(work_id="bad", arxiv_id="2401.00001"),
            OaFulltextTarget(work_id="good", pmcid="PMC123"),
        ]
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
    assert [item.work_id for item in result.documents] == ["good"]
    assert any(item.work_id == "bad" and item.status != "acquired" for item in result.attempts)
    assert any(item.work_id == "good" and item.status == "acquired" for item in result.attempts)


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
    assert "identifier=oai:pubmedcentral.nih.gov:7654321" in plan.candidates[0].url
    assert plan.candidates[0].url_type == "jats"


def test_fulltext_budget_prefers_relevance_then_influential_citations() -> None:
    targets = [
        OaFulltextTarget(
            work_id="low",
            arxiv_id="1",
            relevance_score=0.2,
            influential_citation_count=100,
        ),
        OaFulltextTarget(
            work_id="relevant",
            arxiv_id="2",
            relevance_score=0.9,
            influential_citation_count=1,
        ),
        OaFulltextTarget(
            work_id="tie-break",
            arxiv_id="3",
            relevance_score=0.9,
            influential_citation_count=10,
        ),
    ]
    plan = plan_oa_fulltext(targets, max_works=2)
    ordered_works = list(dict.fromkeys(candidate.work_id for candidate in plan.candidates))
    assert ordered_works == ["tie-break", "relevant"]


def test_unpaywall_discovery_uses_best_oa_pdf_and_license() -> None:
    client = _client(
        lambda request: httpx.Response(
            200,
            json={
                "best_oa_location": {
                    "url_for_pdf": "https://example.org/open.pdf",
                    "license": "cc-by",
                },
                "oa_locations": [],
            },
        ),
        resolver=_StubResolver({"api.unpaywall.org": ("104.20.42.59",)}),
    )
    links = discover_unpaywall_links(
        OaFulltextTarget(work_id="w", doi="10.1000/example"),
        http_client=client,
        contact_email="researcher@example.org",
    )
    assert links == (
        {
            "url": "https://example.org/open.pdf",
            "url_type": "pdf",
            "source_name": "unpaywall",
            "is_oa": True,
            "license": "cc-by",
        },
    )


def test_arxiv_source_archive_is_classified_for_latex_parser() -> None:
    archive = b"\x1f\x8b" + b"source-bytes"
    plan = plan_oa_fulltext([OaFulltextTarget(work_id="w", arxiv_id="2401.1")])
    result = acquire_oa_fulltext(
        plan,
        http_client=_client(
            lambda request: httpx.Response(
                200,
                content=archive,
                headers={"content-type": "application/octet-stream"},
            ),
            resolver=_StubResolver({"arxiv.org": ("151.101.3.42",)}),
        ),
    )
    assert result.documents[0].mime_type == "application/x-arxiv-source"


def test_doaj_discovery_reads_fulltext_url_and_license() -> None:
    client = _client(
        lambda request: httpx.Response(
            200,
            json={
                "results": [
                    {
                        "bibjson": {
                            "link": [
                                {
                                    "url": "https://journal.example/article.pdf",
                                    "type": "fulltext",
                                    "content_type": "application/pdf",
                                }
                            ],
                            "license": [{"title": "CC BY"}],
                        }
                    }
                ]
            },
        ),
        resolver=_StubResolver({"doaj.org": ("104.20.8.99",)}),
    )
    links = discover_doaj_links(
        OaFulltextTarget(work_id="w", doi="10.1000/example"),
        http_client=client,
    )
    assert links[0]["url"] == "https://journal.example/article.pdf"
    assert links[0]["license"] == "CC BY"


# ---------------------------------------------------------------------------
# 并发抓取（2026-09-07）。串行的三次 ingest 合计约 6 分钟墙钟，而每篇之间完全独立。
# 下面这组钉住并发**不能**改变的三件事。
# ---------------------------------------------------------------------------


def _many_works_plan(count: int):
    targets = [
        OaFulltextTarget(work_id=f"w-{index}", arxiv_id=f"2401.{index:05d}")
        for index in range(count)
    ]
    return plan_oa_fulltext(targets)


def test_parallel_acquisition_keeps_plan_order_regardless_of_completion_order() -> None:
    """attempts/documents/failures 的顺序被写进 fulltext_attempt 行，不能随完成序漂移。"""
    import time

    def handler(request: httpx.Request) -> httpx.Response:
        # 让靠后的文献先返回：完成序与计划序刻意相反。
        index = int(str(request.url).split("2401.")[1].split(".")[0].split("/")[0])
        time.sleep(0.02 * (5 - index))
        return httpx.Response(200, content=PDF_BYTES, headers={"content-type": "application/pdf"})

    plan = _many_works_plan(5)
    result = acquire_oa_fulltext(
        plan,
        http_client=_client(handler, resolver=_StubResolver({"arxiv.org": ("151.101.3.42",)})),
        concurrency=5,
    )
    assert [document.work_id for document in result.documents] == [f"w-{i}" for i in range(5)]
    assert [attempt.work_id for attempt in result.attempts] == [f"w-{i}" for i in range(5)]


def test_parallel_acquisition_keeps_per_work_url_fallback_sequential() -> None:
    """每篇内部仍是「按回退顺序逐个试、首个成功即停」——并发发出会换掉留下的那个 URL。"""
    calls: list[str] = []
    lock = __import__("threading").Lock()

    def handler(request: httpx.Request) -> httpx.Response:
        with lock:
            calls.append(str(request.url))
        # 第一个候选（e-print）失败，第二个（pdf）成功。
        if "e-print" in str(request.url):
            return httpx.Response(404, content=b"missing")
        return httpx.Response(200, content=PDF_BYTES, headers={"content-type": "application/pdf"})

    plan = plan_oa_fulltext([OaFulltextTarget(work_id="w-1", arxiv_id="2401.01234")])
    result = acquire_oa_fulltext(
        plan,
        http_client=_client(handler, resolver=_StubResolver({"arxiv.org": ("151.101.3.42",)})),
        concurrency=8,
    )
    assert len(result.documents) == 1
    # 恰好两次：失败一次、成功一次。全部并发发出的话这里会是候选总数。
    assert len(calls) == 2
    assert "e-print" in calls[0]


def test_per_host_concurrency_of_one_keeps_a_single_host_serial() -> None:
    """SafeHttpClient 没有任何限速，所以礼貌度只能靠这个信号量。"""
    import threading
    import time

    inflight = {"now": 0, "peak": 0}
    lock = threading.Lock()

    def handler(request: httpx.Request) -> httpx.Response:
        with lock:
            inflight["now"] += 1
            inflight["peak"] = max(inflight["peak"], inflight["now"])
        time.sleep(0.02)
        with lock:
            inflight["now"] -= 1
        return httpx.Response(200, content=PDF_BYTES, headers={"content-type": "application/pdf"})

    plan = _many_works_plan(6)
    acquire_oa_fulltext(
        plan,
        http_client=_client(handler, resolver=_StubResolver({"arxiv.org": ("151.101.3.42",)})),
        concurrency=6,
        per_host_concurrency=1,
    )
    # 全部候选都在 arxiv.org 上，所以尽管 concurrency=6，同时在飞的也只能是 1。
    assert inflight["peak"] == 1


def test_distinct_hosts_do_run_concurrently() -> None:
    """并行度本来就在 host 之间——否则上面那条限制会让并发化毫无意义。"""
    import threading
    import time

    inflight = {"now": 0, "peak": 0}
    lock = threading.Lock()

    def handler(request: httpx.Request) -> httpx.Response:
        with lock:
            inflight["now"] += 1
            inflight["peak"] = max(inflight["peak"], inflight["now"])
        time.sleep(0.05)
        with lock:
            inflight["now"] -= 1
        return httpx.Response(200, content=PDF_BYTES, headers={"content-type": "application/pdf"})

    targets = [
        OaFulltextTarget(work_id="w-arxiv", arxiv_id="2401.01234"),
        OaFulltextTarget(work_id="w-pmc", pmcid="PMC7654321"),
    ]
    plan = plan_oa_fulltext(targets)
    acquire_oa_fulltext(
        plan,
        http_client=_client(
            handler,
            resolver=_StubResolver(
                {
                    "arxiv.org": ("151.101.3.42",),
                    "europepmc.org": ("193.62.193.80",),
                    "pmc.ncbi.nlm.nih.gov": ("130.14.29.110",),
                }
            ),
        ),
        concurrency=4,
        per_host_concurrency=1,
    )
    assert inflight["peak"] > 1


def test_lazy_client_construction_is_thread_safe() -> None:
    """并发抓取会同时撞上懒初始化；没有锁就会建出两个 client，其中一个永不回收。"""
    import threading

    client = SafeHttpClient(user_agent="PaperForge/0.1")
    seen: list[object] = []
    barrier = threading.Barrier(8)

    def grab() -> None:
        barrier.wait()
        seen.append(client.client)

    threads = [threading.Thread(target=grab) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len({id(item) for item in seen}) == 1
    client.close()
