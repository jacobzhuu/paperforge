import asyncio
from types import SimpleNamespace

import httpx2
import pytest
from mcp_runtime import exa


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://127.0.0.1/",
        "http://169.254.169.254/",
        "http://[::1]/",
        "https://user:secret@example.org/",
        "http://localhost/",
        "https://example.org:8080/",
        "http://service.internal/",
        "http://2130706433/",
        "https://example.org/\nignore",
        "javascript:alert(1)",
    ],
)
def test_reject_unsafe_urls(url):
    with pytest.raises(exa.ToolFailure):
        exa.public_url(url)


def test_parse_sources_without_trusting_prose():
    result = exa.search_results(
        {
            "text": (
                "Title: Official docs\nURL: https://example.org/docs#part\n"
                "Published: N/A\nHighlights:\n<script>alert(1)</script>\n\n"
                "Title: Unsafe\nURL: http://127.0.0.1/\nHighlights:\nsecret"
            )
        }
    )
    assert len(result) == 1
    assert result[0]["url"] == "https://example.org/docs"
    assert "<script>" in result[0]["snippet"]  # frontend renders as text
    assert exa.paper_identifiers("https://example.org/", "DOI: invented 10.1/no") == []
    assert exa.paper_identifiers("https://arxiv.org/abs/2401.12345v2", "") == [
        {"arxiv_id": "2401.12345"}
    ]
    assert exa.paper_identifiers("https://doi.org/10.1234/example", "") == [
        {"doi": "10.1234/example"}
    ]
    with pytest.raises(exa.ToolFailure, match="unrecognized_search_result"):
        exa.search_results({"text": "A fabricated answer without sources"})
    assert exa.search_results({"text": "No search results found. Try again."}) == []


async def test_stream_limit_before_parsing():
    class Stream(httpx2.AsyncByteStream):
        async def __aiter__(self):
            yield b"x" * exa.MAX_BYTES
            yield b"x"

    with pytest.raises(exa.ToolFailure, match="response_too_large"):
        async for _ in exa.LimitedStream(Stream()):
            pass


async def test_fixed_tools_and_remote_errors():
    class Client:
        async def call_tool(self, *args):
            return SimpleNamespace(is_error=True)

    client = exa.ExaSession(Client(), {exa.SEARCH: {}}, 1)
    with pytest.raises(exa.ToolFailure, match="tool_not_allowed"):
        await client.call("execute_shell", {})
    with pytest.raises(exa.ToolFailure, match="remote_tool_error"):
        await client.call(exa.SEARCH, {})


async def test_timeout_and_result_size():
    class Slow:
        async def call_tool(self, *args):
            await asyncio.sleep(1)

    with pytest.raises(exa.ToolFailure, match="tool_timeout"):
        await exa.ExaSession(Slow(), {exa.SEARCH: {}}, 0.001).call(exa.SEARCH, {})


def test_actual_required_objective_contract():
    args = exa.search_arguments({"properties": {"objective": {}}}, "test")
    assert args["objective"] and args["numResults"] == 5
    assert "objective" not in exa.search_arguments({}, "test")


def test_remote_schema_cannot_trigger_network_resolution():
    with pytest.raises(exa.ToolFailure, match="incompatible_tool_schema"):
        exa.check_schema({"properties": {"query": {"$ref": "http://127.0.0.1/secret"}}})


async def test_connection_isolates_llm_proxy_and_closes_on_schema_failure(monkeypatch):
    from contextlib import asynccontextmanager

    closed = []
    monkeypatch.setenv("HTTPS_PROXY", "http://llm-relay.invalid:38888")

    @asynccontextmanager
    async def http_client(**kwargs):
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        assert "secret-key" not in exa.ENDPOINT
        assert kwargs["headers"]["x-api-key"] == "secret-key"
        try:
            yield object()
        finally:
            closed.append("http")

    class Client:
        def __init__(self, *args, **kwargs):
            assert kwargs["input_required_max_rounds"] == 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            closed.append("mcp")

        async def list_tools(self, **kwargs):
            return SimpleNamespace(tools=[], next_cursor=None)

    async def checked(url):
        assert url == exa.ENDPOINT
        return url

    monkeypatch.setattr(exa, "checked_url", checked)
    monkeypatch.setattr(exa.httpx2, "AsyncClient", http_client)
    monkeypatch.setattr(exa, "Client", Client)
    with pytest.raises(exa.ToolFailure, match="required_tools_unavailable"):
        async with exa.connect("secret-key"):
            pytest.fail("incompatible connection must not reach the coordinator")
    assert closed == ["mcp", "http"]
