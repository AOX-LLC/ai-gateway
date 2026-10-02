"""Assemble the ticketing server: strict tools over the ticketing_app database role."""

from psycopg_pool import AsyncConnectionPool
from starlette.applications import Starlette

from mcp_common.server import build_server_app
from mcp_common.toolset import StrictToolset
from ticketing_server import CONNECTION_KWARGS, SCHEMA
from ticketing_server.repo import TicketRepo
from ticketing_server.settings import TicketingSettings
from ticketing_server.tools import INSTRUCTIONS, build_toolset

SERVER_NAME = "ticketing"


def build_app(settings: TicketingSettings) -> Starlette:
    """The ticketing app. A missing credential raises here, before any request is accepted."""

    def toolset(pool: AsyncConnectionPool) -> StrictToolset:
        return build_toolset(TicketRepo(pool))

    return build_server_app(
        settings,
        name=SERVER_NAME,
        distribution="ticketing-server",
        schema=SCHEMA,
        connection_kwargs=CONNECTION_KWARGS,
        instructions=INSTRUCTIONS,
        build_toolset=toolset,
    )
