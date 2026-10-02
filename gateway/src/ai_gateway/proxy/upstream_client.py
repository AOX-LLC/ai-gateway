"""Open an MCP client connection to one upstream server."""

from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

import httpx2
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import Implementation

from ai_gateway.registry.models import UpstreamServer

UpstreamClientFactory = Callable[[UpstreamServer], AbstractAsyncContextManager[Client]]

_GATEWAY_IDENTITY = Implementation(name="ai-gateway", version="0.1.0")


@asynccontextmanager
async def open_upstream_client(upstream: UpstreamServer) -> AsyncGenerator[Client]:
    """Connect over Streamable HTTP, pinned to the handshake protocol (2025-11-25).

    The SDK's response cache is off: the catalog must always see the upstream's
    current tool list, never a cached one.
    """
    timeout = httpx2.Timeout(upstream.call_timeout_s, connect=upstream.connect_timeout_s)
    async with (
        httpx2.AsyncClient(timeout=timeout, follow_redirects=False) as http_client,
        Client(
            streamable_http_client(upstream.url, http_client=http_client),
            read_timeout_seconds=upstream.call_timeout_s,
            mode="legacy",
            cache=None,
            client_info=_GATEWAY_IDENTITY,
        ) as client,
    ):
        yield client
