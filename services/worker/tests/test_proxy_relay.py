from __future__ import annotations

import argparse
import asyncio

import pytest
from paperforge_worker.proxy_relay import (
    _loopback_upstream_host,
    _private_listen_host,
    relay_connection,
)


def test_relay_address_validation_prevents_public_proxy_exposure() -> None:
    assert _private_listen_host("10.254.20.1") == "10.254.20.1"
    assert _loopback_upstream_host("127.0.0.1") == "127.0.0.1"
    with pytest.raises(argparse.ArgumentTypeError):
        _private_listen_host("0.0.0.0")
    with pytest.raises(argparse.ArgumentTypeError):
        _private_listen_host("10.122.230.165")
    with pytest.raises(argparse.ArgumentTypeError):
        _loopback_upstream_host("10.254.20.1")


async def test_relay_copies_bytes_in_both_directions() -> None:
    async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(await reader.read(1024))
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    upstream = await asyncio.start_server(echo, "127.0.0.1", 0)
    upstream_port = upstream.sockets[0].getsockname()[1]
    relay = await asyncio.start_server(
        lambda reader, writer: relay_connection(
            reader,
            writer,
            upstream_host="127.0.0.1",
            upstream_port=upstream_port,
        ),
        "127.0.0.1",
        0,
    )
    relay_port = relay.sockets[0].getsockname()[1]

    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", relay_port)
        writer.write(b"paperforge-proxy-check")
        await writer.drain()
        writer.write_eof()
        assert await reader.read() == b"paperforge-proxy-check"
        writer.close()
        await writer.wait_closed()
    finally:
        relay.close()
        upstream.close()
        await relay.wait_closed()
        await upstream.wait_closed()
