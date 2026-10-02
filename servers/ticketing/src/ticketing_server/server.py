"""Assemble the ticketing server: strict tools over the ticketing_app database role."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from importlib.metadata import version

from psycopg_pool import AsyncConnectionPool
from starlette.applications import Starlette

from mcp_common.http_app import build_mcp_app
from ticketing_server import CONNECTION_KWARGS
from ticketing_server.repo import TicketRepo
from ticketing_server.settings import TicketingSettings
from ticketing_server.tools import INSTRUCTIONS, build_toolset

SERVER_NAME = "ticketing"


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
    )
