"""The Starlette app that serves one low-level MCP server over Streamable HTTP.

Mirrors how the gateway serves its own endpoint: JSON responses, DNS-rebinding protection
with an explicit host allowlist, and the session manager's task group tied to the app's
lifespan. The MCP route sits behind the service credential; /healthz is open and says
nothing but "ok", so a container health check needs no secret.
"""

from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp, StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from mcp_common.credentials import ServiceCredentialMiddleware

MCP_PATH = "/mcp"

AppLifespan = Callable[[], AbstractAsyncContextManager[None]]


def build_mcp_app(
    server: Server[Any],
    *,
    allowed_hosts: list[str],
    credential: str,
    lifespan: AppLifespan | None = None,
) -> Starlette:
    """Serve `server` at /mcp, requiring the service credential. `lifespan`, if given,
    wraps the app's run, for resources such as a database pool."""
    session_manager = StreamableHTTPSessionManager(
        server,
        json_response=True,
        security_settings=TransportSecuritySettings(allowed_hosts=allowed_hosts),
    )

    @asynccontextmanager
    async def app_lifespan(_: Starlette) -> AsyncGenerator[None]:
        async with session_manager.run():
            if lifespan is None:
                yield
                return
            async with lifespan():
                yield

    async def healthz(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    mcp_endpoint = ServiceCredentialMiddleware(StreamableHTTPASGIApp(session_manager), credential)
    return Starlette(
        routes=[
            Route("/healthz", healthz, methods=["GET"]),
            Route(MCP_PATH, endpoint=mcp_endpoint, methods=["GET", "POST", "DELETE"]),
        ],
        lifespan=app_lifespan,
    )
