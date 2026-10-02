"""Shared fixtures: the Postgres test database, registry helpers and the echo MCP server."""

import os
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from datetime import datetime
from uuid import UUID

import psycopg
import pytest
import uvicorn
from psycopg_pool import AsyncConnectionPool

from ai_gateway.auth.tokens import IssuedToken, generate_token
from ai_gateway.registry.migrate import apply_migrations
from ai_gateway.registry.repo import AdminRegistry
from echo_server.server import build_app

MakeClient = Callable[..., Awaitable[tuple[UUID, IssuedToken]]]

_SERVER_START_TIMEOUT_S = 10.0


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
    await apply_migrations(url)
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
    config = uvicorn.Config(
        build_app(["127.0.0.1:*"]), host="127.0.0.1", port=0, log_level="warning"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + _SERVER_START_TIMEOUT_S
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            server.should_exit = True
            raise RuntimeError("echo server did not start")
        time.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        thread.join(timeout=_SERVER_START_TIMEOUT_S)
