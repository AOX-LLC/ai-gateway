"""Open an MCP client connection to one upstream server."""

import os
from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

import httpx2
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import Implementation

from ai_gateway.registry.models import UpstreamServer

UpstreamClientFactory = Callable[[UpstreamServer], AbstractAsyncContextManager[Client]]

_GATEWAY_IDENTITY = Implementation(name="ai-gateway", version="0.1.0")


class UpstreamCredentialError(RuntimeError):
    """The upstream needs a service credential that this process does not have.

    The message names the environment variable, never its value.
    """


def upstream_headers(upstream: UpstreamServer) -> dict[str, str]:
    """The headers every connection to this upstream carries: its service credential, if any.

    The credential is read from the environment at connect time, so rotating it needs a
    restart of the gateway but no registry change. A missing or empty variable makes the
    upstream unavailable rather than letting the gateway call it without credentials.
    """
    if upstream.credential_env is None:
        return {}
    value = os.environ.get(upstream.credential_env, "")
    if not value:
        raise UpstreamCredentialError(
            f"upstream {upstream.namespace!r} needs the service credential in the environment"
            f" variable {upstream.credential_env}, which is not set"
        )
    return {"Authorization": f"Bearer {value}"}


@asynccontextmanager
async def open_upstream_client(upstream: UpstreamServer) -> AsyncGenerator[Client]:
    """Connect over Streamable HTTP, pinned to the handshake protocol (2025-11-25).

    The SDK's response cache is off: the catalog must always see the upstream's
    current tool list, never a cached one.
    """
    timeout = httpx2.Timeout(upstream.call_timeout_s, connect=upstream.connect_timeout_s)
    async with (
        httpx2.AsyncClient(
            timeout=timeout, follow_redirects=False, headers=upstream_headers(upstream)
        ) as http_client,
        Client(
            streamable_http_client(upstream.url, http_client=http_client),
            read_timeout_seconds=upstream.call_timeout_s,
            mode="legacy",
            cache=None,
            client_info=_GATEWAY_IDENTITY,
        ) as client,
    ):
        yield client
