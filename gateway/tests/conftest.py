"""Shared fixtures: the Postgres test database, registry helpers and the echo MCP server."""

import os
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from datetime import datetime
from uuid import UUID

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from ai_gateway.auth.tokens import IssuedToken, generate_token
from ai_gateway.registry import MIGRATIONS_PACKAGE
from ai_gateway.registry.repo import AdminRegistry
from echo_server.server import build_app
from mcp_common.migrate import apply_migrations
from tests.helpers import serve_in_thread
from ticketing_server import CONNECTION_KWARGS as TICKETING_CONNECTION_KWARGS
from ticketing_server import MIGRATIONS_PACKAGE as TICKETING_MIGRATIONS_PACKAGE
from ticketing_server import SCHEMA as TICKETING_SCHEMA
from ticketing_server.seed import Dataset, build_dataset, insert_dataset

MakeClient = Callable[..., Awaitable[tuple[UUID, IssuedToken]]]


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="session")
async def test_database_url(anyio_backend: str) -> str:
    url = os.environ.get("GATEWAY_TEST_DATABASE_URL")
    if not url:
        pytest.skip("GATEWAY_TEST_DATABASE_URL is not set")
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as connection:
        await connection.execute("DROP SCHEMA public CASCADE")
        await connection.execute("CREATE SCHEMA public")
        # A recreated schema loses the default grant; the gateway's role needs it.
        await connection.execute("GRANT USAGE ON SCHEMA public TO PUBLIC")
    await apply_migrations(url, MIGRATIONS_PACKAGE)
    return url


@pytest.fixture
def app_database_url(test_database_url: str) -> str:
    """The test database as the gateway's own least-privilege role, gateway_app."""
    url = os.environ.get("GATEWAY_TEST_APP_DATABASE_URL")
    if not url:
        pytest.skip("GATEWAY_TEST_APP_DATABASE_URL is not set")
    return url


@pytest.fixture
async def clean_database(test_database_url: str) -> None:
    async with await psycopg.AsyncConnection.connect(test_database_url) as connection:
        await connection.execute(
            "TRUNCATE clients, client_tokens, client_scopes, upstream_servers"
            " RESTART IDENTITY CASCADE"
        )


@pytest.fixture
async def db_pool(
    test_database_url: str, clean_database: None
) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(test_database_url, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture
async def admin_registry(
    test_database_url: str, clean_database: None
) -> AsyncIterator[AdminRegistry]:
    connection = await psycopg.AsyncConnection.connect(test_database_url)
    try:
        yield AdminRegistry(connection)
    finally:
        await connection.close()


@pytest.fixture
def make_client(admin_registry: AdminRegistry) -> MakeClient:
    async def make(
        name: str, scopes: list[str], *, expires_at: datetime | None = None
    ) -> tuple[UUID, IssuedToken]:
        client_id = await admin_registry.upsert_client(name)
        await admin_registry.grant_scopes(client_id, scopes)
        token = generate_token()
        await admin_registry.insert_token(
            client_id, token.lookup_id, token.token_sha256, "test", expires_at
        )
        return client_id, token

    return make


@pytest.fixture(scope="session")
def echo_url() -> Iterator[str]:
    with serve_in_thread(build_app(["127.0.0.1:*"])) as base_url:
        yield f"{base_url}/mcp"


@pytest.fixture
def ticketing_app_url(test_database_url: str) -> str:
    """The test database as the ticketing server's own role, ticketing_app."""
    url = os.environ.get("TICKETING_TEST_APP_DATABASE_URL")
    if not url:
        pytest.skip("TICKETING_TEST_APP_DATABASE_URL is not set")
    return url


@pytest.fixture(scope="session")
async def ticketing_schema(test_database_url: str) -> None:
    """A fresh ticketing schema, built the way harborline-setup builds it."""
    async with await psycopg.AsyncConnection.connect(test_database_url, autocommit=True) as conn:
        await conn.execute("DROP SCHEMA IF EXISTS ticketing CASCADE")
        await conn.execute("CREATE SCHEMA ticketing")
        await conn.execute("REVOKE ALL ON SCHEMA ticketing FROM PUBLIC")
    await apply_migrations(test_database_url, TICKETING_MIGRATIONS_PACKAGE, TICKETING_SCHEMA)


@pytest.fixture
async def ticketing_data(test_database_url: str, ticketing_schema: None) -> Dataset:
    """The deterministic seed, reloaded before every test so writes never leak across tests."""
    dataset = build_dataset()
    async with await psycopg.AsyncConnection.connect(test_database_url) as conn:
        await conn.execute("SET search_path TO ticketing")
        await conn.execute("TRUNCATE comments, tickets, staff RESTART IDENTITY CASCADE")
        await conn.execute("ALTER SEQUENCE ticket_number RESTART")
        await insert_dataset(conn, dataset)
    return dataset


@pytest.fixture
async def ticketing_pool(
    ticketing_app_url: str, ticketing_data: Dataset
) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(ticketing_app_url, kwargs=TICKETING_CONNECTION_KWARGS, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()
