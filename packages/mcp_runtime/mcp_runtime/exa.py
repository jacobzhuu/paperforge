"""Exa MCP boundary: fixed tools, bounded transport and untrusted result parsing."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import re
import socket
from contextlib import asynccontextmanager
from urllib.parse import unquote, urlsplit, urlunsplit

import httpx2
from jsonschema import validate
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp_types import REQUEST_TIMEOUT

ENDPOINT = "https://mcp.exa.ai/mcp"
SEARCH = "web_search_exa"
FETCH = "web_fetch_exa"
TOOLS = {SEARCH: {"query", "numResults"}, FETCH: {"urls", "maxCharacters"}}
MAX_BYTES = 512 * 1024
VERSION = "exa-web-v1"


def search_arguments(schema: dict, query: str) -> dict:
    args = {"query": query, "numResults": 5}
    if "objective" in schema.get("properties", {}):
        args["objective"] = (
            "Find primary project websites, official technical documentation and dataset "
            "descriptions relevant to this research question. Prefer original sources; "
            "exclude advertisements and content farms. Preserve source URLs and paper links."
        )
    return args


class ToolFailure(Exception):
    def __init__(self, code: str, *, retryable: bool = False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


def digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def check_schema(schema):
    """JSON Schema validation must never fetch remote references on the host."""
    stack = [schema]
    visited = 0
    while stack:
        value = stack.pop()
        visited += 1
        if visited > 2000:
            raise ToolFailure("incompatible_tool_schema")
        if isinstance(value, dict):
            if any(key in value for key in ("$ref", "$dynamicRef", "$recursiveRef")):
                # The two Exa tools have flat schemas; no references are needed.
                raise ToolFailure("incompatible_tool_schema")
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)


def public_url(value: str) -> str:
    """Syntactic policy, also used when rendering stored source links."""
    try:
        url = urlsplit(value.strip())
        host = (url.hostname or "").lower().rstrip(".")
        if (
            len(value) > 4096
            or any(ord(c) < 33 for c in value)
            or "\\" in value
            or url.scheme not in {"https", "http"}
            or not host
            or url.username is not None
            or url.password is not None
            or url.port not in {None, 80, 443}
            or host in {"localhost", "metadata", "metadata.google.internal"}
            or host.endswith((".localhost", ".local", ".internal"))
        ):
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if "." not in host:
                raise ValueError from None
        else:
            if not address.is_global:
                raise ValueError
        return urlunsplit((url.scheme, url.netloc.lower(), url.path or "/", url.query, ""))
    except (TypeError, ValueError) as error:
        raise ToolFailure("unsafe_url") from error


async def checked_url(value: str) -> str:
    url = public_url(value)
    parts = urlsplit(url)
    try:
        async with asyncio.timeout(5):
            records = await asyncio.get_running_loop().getaddrinfo(
                parts.hostname,
                parts.port or (443 if parts.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        if not records or any(not ipaddress.ip_address(r[4][0]).is_global for r in records):
            raise ToolFailure("unsafe_url")
    except (OSError, TimeoutError) as error:
        raise ToolFailure("url_unresolvable") from error
    return url


class LimitedStream(httpx2.AsyncByteStream):
    def __init__(self, stream):
        self.stream = stream

    async def __aiter__(self):
        size = 0
        async for chunk in self.stream:
            size += len(chunk)
            if size > MAX_BYTES:
                raise ToolFailure("response_too_large")
            yield chunk

    async def aclose(self):
        await self.stream.aclose()


async def _bound_response(response):
    if response.is_redirect:
        raise ToolFailure("endpoint_redirect_rejected")
    if response.headers.get("content-encoding", "identity") not in {"identity", ""}:
        raise ToolFailure("compressed_response_rejected")
    response.stream = LimitedStream(response.stream)


@asynccontextmanager
async def connect(api_key: str = "", timeout: float = 30):
    # No auth discovery, token forwarding, callbacks, roots, sampling or subprocesses.
    await checked_url(ENDPOINT)
    headers = {"Accept-Encoding": "identity"}
    if api_key:
        headers["x-api-key"] = api_key
    async with httpx2.AsyncClient(
        headers=headers,
        timeout=timeout,
        follow_redirects=False,
        # The deployment HTTPS_PROXY is a dedicated LLM relay. MCP needs its own
        # direct TLS connection, including concurrent notification/POST streams.
        trust_env=False,
        event_hooks={"response": [_bound_response]},
    ) as http:
        async with Client(
            streamable_http_client(ENDPOINT, http_client=http),
            read_timeout_seconds=timeout,
            input_required_max_rounds=0,
        ) as client:
            schemas = {}
            cursor = None
            for _ in range(4):
                result = await client.list_tools(cursor=cursor)
                for tool in result.tools:
                    if tool.name in TOOLS:
                        schema = tool.input_schema
                        check_schema(schema)
                        if not TOOLS[tool.name] <= set(schema.get("properties", {})):
                            raise ToolFailure("incompatible_tool_schema")
                        try:
                            validate(
                                search_arguments(schema, "research")
                                if tool.name == SEARCH
                                else {"urls": ["https://example.org/"], "maxCharacters": 6000},
                                schema,
                            )
                        except Exception as error:
                            raise ToolFailure("incompatible_tool_schema") from error
                        schemas[tool.name] = schema
                cursor = result.next_cursor
                if not cursor:
                    break
            if set(schemas) != set(TOOLS):
                raise ToolFailure("required_tools_unavailable")
            yield ExaSession(client, schemas, timeout)


class ExaSession:
    def __init__(self, client, schemas, timeout):
        self.client, self.schemas, self.timeout = client, schemas, timeout

    async def call(self, tool: str, arguments: dict) -> dict:
        if tool not in TOOLS:
            raise ToolFailure("tool_not_allowed")
        validate(arguments, self.schemas[tool])
        try:
            async with asyncio.timeout(self.timeout):
                result = await self.client.call_tool(tool, arguments)
        except TimeoutError as error:
            raise ToolFailure("tool_timeout", retryable=True) from error
        except MCPError as error:
            raise ToolFailure(
                "tool_timeout" if error.code == REQUEST_TIMEOUT else "protocol_error",
                retryable=error.code == REQUEST_TIMEOUT,
            ) from error
        except httpx2.HTTPStatusError as error:
            code = error.response.status_code
            raise ToolFailure(f"http_{code}", retryable=code in {429, 502, 503, 504}) from error
        except httpx2.TransportError as error:
            raise ToolFailure("transport_error", retryable=True) from error
        if result.is_error:
            # Server error prose is untrusted and may contain secrets; never log it.
            raise ToolFailure("remote_tool_error")
        text = "\n".join(
            item.text for item in result.content if getattr(item, "type", None) == "text"
        )
        payload = {"text": text, "structured": result.structured_content}
        if len(json.dumps(payload).encode()) > MAX_BYTES:
            raise ToolFailure("response_too_large")
        return payload


def search_results(payload: dict) -> list[dict]:
    structured = payload.get("structured")
    if isinstance(structured, dict) and isinstance(structured.get("results"), list):
        rows = structured["results"]
    else:
        text = payload.get("text", "")
        if text.strip().startswith("No search results found"):
            return []
        rows = []
        for match in re.finditer(
            r"(?:^|\n)Title: ([^\n]*)\nURL: (https?://[^\s]+)(.*?)(?=\nTitle: |\Z)",
            text,
            re.S,
        ):
            rows.append({"title": match[1], "url": match[2], "text": match[3].strip()})
        if not rows:
            raise ToolFailure("unrecognized_search_result")
    result = []
    for row in rows[:5]:
        if not isinstance(row, dict):
            continue
        try:
            url = public_url(row.get("url", ""))
        except ToolFailure:
            continue
        result.append(
            {
                "url": url,
                "title": str(row.get("title") or url)[:500],
                "snippet": str(row.get("text") or row.get("highlights") or "")[:2000],
            }
        )
    return result


def fetched_text(payload: dict, url: str) -> str:
    structured = payload.get("structured")
    if isinstance(structured, dict) and isinstance(structured.get("results"), list):
        for row in structured["results"]:
            if isinstance(row, dict) and row.get("url") == url and row.get("text"):
                return str(row["text"])[:6000]
        raise ToolFailure("empty_page")
    text = payload.get("text", "")
    if url not in text or not text.strip() or text.startswith(("Error", "No content")):
        raise ToolFailure("unrecognized_fetch_result")
    return text[:6000]


def paper_identifiers(url: str, body: str) -> list[dict]:
    """Only explicit identifier links; never infer bibliographic metadata from prose."""
    links = [url, *re.findall(r"https?://[^\s<>\"\]]+", body)]
    found = []
    for link in links:
        try:
            parts = urlsplit(public_url(link.rstrip(".,);")))
        except ToolFailure:
            continue
        host, path = parts.hostname, unquote(parts.path)
        item = None
        if host in {"doi.org", "dx.doi.org"} and re.fullmatch(r"/10\.\d{4,9}/\S+", path):
            item = {"doi": path[1:]}
        elif host in {"arxiv.org", "www.arxiv.org"}:
            match = re.fullmatch(r"/(?:abs|pdf)/(\d{4}\.\d{4,5})(?:v\d+)?(?:\.pdf)?", path)
            if match:
                item = {"arxiv_id": match[1]}
        if item and item not in found:
            found.append(item)
    return found[:10]
