"""Assemble the handbook server: strict tools over the handbook_app database role."""

from psycopg_pool import AsyncConnectionPool
from starlette.applications import Starlette

from handbook_server import CONNECTION_KWARGS, SCHEMA
from handbook_server.embedding import Embedder
from handbook_server.repo import HandbookRepo
from handbook_server.settings import HandbookSettings
from handbook_server.tools import INSTRUCTIONS, build_toolset
from mcp_common.server import build_server_app
from mcp_common.toolset import StrictToolset

SERVER_NAME = "handbook"


def build_app(settings: HandbookSettings) -> Starlette:
    """The handbook app. A missing credential or model raises here, before any request is
    accepted."""

    def toolset(pool: AsyncConnectionPool) -> StrictToolset:
        return build_toolset(HandbookRepo(pool, Embedder(settings.model_path)))

    return build_server_app(
        settings,
        name=SERVER_NAME,
        distribution="handbook-server",
        schema=SCHEMA,
        connection_kwargs=CONNECTION_KWARGS,
        instructions=INSTRUCTIONS,
        build_toolset=toolset,
    )
