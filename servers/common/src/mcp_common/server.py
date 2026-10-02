"""Assemble and run one MCP server over its own database role.

The ticketing, CRM and handbook servers differ only in their name, schema, tools and
settings; everything else, from the connection pool to the health answer, is here.
"""

import logging
import sys
from collections.abc import AsyncGenerator, Callable, Mapping
from contextlib import asynccontextmanager
from importlib.metadata import version

import uvicorn
from psycopg_pool import AsyncConnectionPool
from starlette.applications import Starlette
from starlette.responses import JSONResponse

from mcp_common.credentials import MissingCredentialError, WeakCredentialError
from mcp_common.health import BuildIdentity, SchemaVersionCache, read_schema_version
from mcp_common.http_app import build_mcp_app
from mcp_common.settings import ServerSettings
from mcp_common.toolset import StrictToolset


class _SchemaVersionSource:
    def __init__(self, pool: AsyncConnectionPool, schema: str) -> None:
        self._pool = pool
        self._schema = schema

    async def schema_version(self) -> int:
        return await read_schema_version(self._pool, self._schema)


def build_server_app(
    settings: ServerSettings,
    *,
    name: str,
    distribution: str,
    schema: str,
    connection_kwargs: Mapping[str, str],
    instructions: str,
    build_toolset: Callable[[AsyncConnectionPool], StrictToolset],
) -> Starlette:
    """The server's app. A missing credential, or whatever `build_toolset` raises (the
    handbook's missing model, say), raises here, before any request is accepted.

    `distribution` is the package name that `/healthz` and the MCP handshake report as the
    server's version."""
    pool = AsyncConnectionPool(
        settings.database_url.get_secret_value(),
        kwargs=dict(connection_kwargs),
        min_size=1,
        max_size=5,
        open=False,
    )
    server_version = version(distribution)
    server = build_toolset(pool).build_server(name, server_version, instructions)

    identity = BuildIdentity(
        commit=settings.git_commit, branch=settings.git_branch, version=server_version
    )
    schema_versions = SchemaVersionCache(_SchemaVersionSource(pool, schema))

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


def run_server[S: ServerSettings](
    command: str,
    settings_class: type[S],
    build_app: Callable[[S], Starlette],
    startup_errors: tuple[type[Exception], ...] = (),
) -> None:
    """The body of a server's `main`: read its settings, build the app and serve it.

    `command` names the server in a startup message. A weak or missing credential, and any
    of `startup_errors`, end the process with one line and no traceback."""
    settings = settings_class()
    logging.basicConfig(level=settings.log_level)
    expected_errors: tuple[type[Exception], ...] = (
        MissingCredentialError,
        WeakCredentialError,
        *startup_errors,
    )
    try:
        app = build_app(settings)
    except expected_errors as error:
        sys.exit(f"{command}: {error}")
    uvicorn.run(app, host=settings.bind, port=settings.port)
