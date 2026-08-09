"""Expose a host-local HTTP proxy only to one deployment's private network.

The production host's proxy intentionally listens on loopback.  Workers live in
Docker networks, so they cannot use that loopback address directly.  This small
TCP relay runs with host networking and binds only the current deployment's
private bridge gateway; it never listens on a wildcard or public address.
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import logging
import os

logger = logging.getLogger(__name__)
_DEPLOYMENT_NETWORK = ipaddress.ip_network("10.254.0.0/16")


def _private_listen_host(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("listen host must be an IP address") from error
    if address not in _DEPLOYMENT_NETWORK:
        raise argparse.ArgumentTypeError("listen host must be a deployment gateway address")
    return str(address)


def _loopback_upstream_host(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("upstream host must be an IP address") from error
    if not address.is_loopback:
        raise argparse.ArgumentTypeError("upstream proxy must be bound to loopback")
    return str(address)


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("port must be an integer") from error
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


async def _copy(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while chunk := await reader.read(64 * 1024):
            writer.write(chunk)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        try:
            writer.write_eof()
        except (AttributeError, ConnectionError, RuntimeError):
            pass


async def relay_connection(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    *,
    upstream_host: str,
    upstream_port: int,
    connect_timeout: float = 5.0,
) -> None:
    """Relay one TCP connection and close both sides deterministically."""
    upstream_writer: asyncio.StreamWriter | None = None
    try:
        upstream_reader, upstream_writer = await asyncio.wait_for(
            asyncio.open_connection(upstream_host, upstream_port),
            timeout=connect_timeout,
        )
        await asyncio.gather(
            _copy(client_reader, upstream_writer),
            _copy(upstream_reader, client_writer),
        )
    except (TimeoutError, ConnectionError, OSError) as error:
        logger.warning("proxy upstream unavailable: %s", type(error).__name__)
    finally:
        if upstream_writer is not None:
            upstream_writer.close()
            await upstream_writer.wait_closed()
        client_writer.close()
        await client_writer.wait_closed()


async def serve(
    *,
    listen_host: str,
    listen_port: int,
    upstream_host: str,
    upstream_port: int,
) -> None:
    server = await asyncio.start_server(
        lambda reader, writer: relay_connection(
            reader,
            writer,
            upstream_host=upstream_host,
            upstream_port=upstream_port,
        ),
        listen_host,
        listen_port,
    )
    logger.info(
        "proxy relay listening on %s:%s for loopback upstream %s:%s",
        listen_host,
        listen_port,
        upstream_host,
        upstream_port,
    )
    async with server:
        await server.serve_forever()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--listen-host",
        type=_private_listen_host,
        default=os.getenv("PAPERFORGE_PROXY_RELAY_LISTEN_HOST", ""),
    )
    parser.add_argument(
        "--listen-port",
        type=_port,
        default=os.getenv("PAPERFORGE_PROXY_RELAY_LISTEN_PORT", "38888"),
    )
    parser.add_argument(
        "--upstream-host",
        type=_loopback_upstream_host,
        default=os.getenv("PAPERFORGE_PROXY_RELAY_UPSTREAM_HOST", "127.0.0.1"),
    )
    parser.add_argument(
        "--upstream-port",
        type=_port,
        default=os.getenv("PAPERFORGE_PROXY_RELAY_UPSTREAM_PORT", "38888"),
    )
    return parser


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    args = _parser().parse_args()
    # argparse applies ``type`` only to command-line values, not defaults.
    listen_host = _private_listen_host(str(args.listen_host))
    listen_port = _port(str(args.listen_port))
    upstream_host = _loopback_upstream_host(str(args.upstream_host))
    upstream_port = _port(str(args.upstream_port))
    asyncio.run(
        serve(
            listen_host=listen_host,
            listen_port=listen_port,
            upstream_host=upstream_host,
            upstream_port=upstream_port,
        )
    )


if __name__ == "__main__":
    main()
