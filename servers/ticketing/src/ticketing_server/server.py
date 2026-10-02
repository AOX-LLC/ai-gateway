"""Assemble the ticketing server: strict tools over the ticketing_app database role."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from importlib.metadata import version

from psycopg_pool import AsyncConnectionPool
from starlette.applications import Starlette
from starlette.responses import JSONResponse

from mcp_common.health import BuildIdentity, SchemaVersionCache, read_schema_version
from mcp_common.http_app import build_mcp_app
from ticketing_server import CONNECTION_KWARGS, SCHEMA
from ticketing_server.repo import TicketRepo
from ticketing_server.settings import TicketingSettings
from ticketing_server.tools import INSTRUCTIONS, build_toolset

SERVER_NAME = "ticketing"


class _SchemaVersionSource:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def schema_version(self) -> int:
        return await read_schema_version(self._pool, SCHEMA)


def build_app(settings: TicketingSettings) -> Starlette:
    """The ticketing app. A missing credential raises here, before any request is accepted."""
    pool = AsyncConnectionPool(
        settings.database_url.get_secret_value(),
        kwargs=CONNECTION_KWARGS,
        min_size=1,
        max_size=5,
        open=False,
    )
    toolset = build_toolset(TicketRepo(pool))
    server = toolset.build_server(SERVER_NAME, version("ticketing-server"), INSTRUCTIONS)

    identity = BuildIdentity(
        commit=settings.git_commit,
        branch=settings.git_branch,
        version=version("ticketing-server"),
    )
    schema_versions = SchemaVersionCache(_SchemaVersionSource(pool))

    async def health() -> JSONResponse:
        """Healthy while the database answers; nothing here is secret."""
        schema_version = await schema_versions.current()
        if schema_version is None:
            return JSONResponse(identity.payload("unavailable", None), status_code=503)
        return JSONResponse(identity.payload("ok", schema_version))

    @asynccontextmanager
    async def lifespan() -> AsyncGenerator[None]:
        # Raises when the database is unreachable, so the server refuses to start.
        await pool.open(wait=True, timeout=30)
        try:
            yield
        finally:
            await pool.close()

    return build_mcp_app(
        server,
        allowed_hosts=settings.allowed_hosts,
        credential=settings.service_token.get_secret_value(),
        lifespan=lifespan,
        health=health,
    )
