"""Assemble the CRM server: strict tools over the crm_app database role."""

from psycopg_pool import AsyncConnectionPool
from starlette.applications import Starlette

from crm_server import CONNECTION_KWARGS, SCHEMA
from crm_server.repo import CrmRepo
from crm_server.settings import CrmSettings
from crm_server.tools import INSTRUCTIONS, build_toolset
from mcp_common.server import build_server_app
from mcp_common.toolset import StrictToolset

SERVER_NAME = "crm"


def build_app(settings: CrmSettings) -> Starlette:
    """The CRM app. A missing credential raises here, before any request is accepted."""

    def toolset(pool: AsyncConnectionPool) -> StrictToolset:
        return build_toolset(CrmRepo(pool))

    return build_server_app(
        settings,
        name=SERVER_NAME,
        distribution="crm-server",
        schema=SCHEMA,
        connection_kwargs=CONNECTION_KWARGS,
        instructions=INSTRUCTIONS,
        build_toolset=toolset,
    )
