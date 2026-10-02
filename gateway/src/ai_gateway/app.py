"""Assemble the gateway: FastAPI app, MCP endpoint, and the tasks behind it."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import anyio
from fastapi import FastAPI
from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp, StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from psycopg_pool import AsyncConnectionPool
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from ai_gateway.auth.middleware import BearerAuthMiddleware
from ai_gateway.auth.verifier import TokenVerifier
from ai_gateway.pipeline.config import load_pipeline_config
from ai_gateway.pipeline.registry import LAYER_ORDER
from ai_gateway.pipeline.runner import Pipeline
from ai_gateway.proxy.catalog import Catalog
from ai_gateway.proxy.http import ProtocolVersionGuard, SessionAdmission, SessionCleanup
from ai_gateway.proxy.server import GatewayServer
from ai_gateway.proxy.sessions import UpstreamSessionPool
from ai_gateway.registry.repo import GatewayRegistry
from ai_gateway.seams.events import EventSink, LogEventSink
from ai_gateway.settings import GatewaySettings

MCP_PATH = "/mcp"


class _McpEndpoint:
    """The MCP endpoint's ASGI app. It only exists while the lifespan runs, because the
    SDK's session manager needs a running task group."""

    def __init__(self) -> None:
        self.app: ASGIApp | None = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self.app is None:
            response = JSONResponse({"error": "starting"}, status_code=503)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def create_app(settings: GatewaySettings, events: EventSink | None = None) -> FastAPI:
    """Build the app. Configuration errors, such as a bad pipeline file, raise here,
    before the gateway accepts a single request."""
    event_sink = events or LogEventSink()
    pipeline_config = load_pipeline_config(settings.pipeline_file, LAYER_ORDER)
    pipeline = Pipeline.build(pipeline_config, event_sink)
    endpoint = _McpEndpoint()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
        async with AsyncConnectionPool(
            settings.database_url.get_secret_value(), min_size=1, max_size=10, open=False
        ) as db_pool:
            await db_pool.open(wait=True, timeout=30)
            registry = GatewayRegistry(db_pool)
            catalog = Catalog(registry, refresh_interval_s=settings.catalog_refresh_s)
            sessions = UpstreamSessionPool(idle_timeout_s=settings.session_idle_timeout_s)
            session_manager = StreamableHTTPSessionManager(
                GatewayServer(catalog, sessions, pipeline).build(),
                json_response=True,
                security_settings=TransportSecuritySettings(allowed_hosts=settings.allowed_hosts),
                session_idle_timeout=settings.session_idle_timeout_s,
                max_sessions=settings.max_sessions,
            )
            endpoint.app = BearerAuthMiddleware(
                AuthContextMiddleware(
                    SessionAdmission(
                        SessionCleanup(
                            ProtocolVersionGuard(StreamableHTTPASGIApp(session_manager)), sessions
                        ),
                        max_sessions_per_client=settings.max_sessions_per_client,
                        idle_timeout_s=settings.session_idle_timeout_s,
                    )
                ),
                TokenVerifier(registry),
                event_sink,
            )

            async with anyio.create_task_group() as task_group:
                await task_group.start(sessions.run)
                await task_group.start(catalog.run)
                async with session_manager.run():
                    try:
                        yield
                    finally:
                        endpoint.app = None
                task_group.cancel_scope.cancel()

    app = FastAPI(
        title="AI Gateway", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        """Healthy only while the MCP endpoint is serving, so Compose notices when it is not."""
        if endpoint.app is None:
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return JSONResponse({"status": "ok"})

    app.router.routes.append(Route(MCP_PATH, endpoint=endpoint, methods=["GET", "POST", "DELETE"]))
    return app
