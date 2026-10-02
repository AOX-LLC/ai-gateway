"""The gateway's database role may read the registry and record token use, nothing more."""

from collections.abc import AsyncIterator, Awaitable, Callable
from uuid import UUID

import psycopg
import pytest
from psycopg import AsyncConnection

from ai_gateway.auth.tokens import IssuedToken
from ai_gateway.registry.repo import AdminRegistry

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

MakeClient = Callable[..., Awaitable[tuple[UUID, IssuedToken]]]

READABLE_TABLES = [
    "clients",
    "client_tokens",
    "client_scopes",
    "upstream_servers",
    "schema_migrations",
]

FORBIDDEN_WRITES = {
    "insert client": "INSERT INTO clients (name) VALUES ('intruder')",
    "insert token": (
        "INSERT INTO client_tokens (client_id, lookup_id, token_sha256)"
        " SELECT id, 'abcdefgh', sha256('x') FROM clients LIMIT 1"
    ),
    "insert scope": (
        "INSERT INTO client_scopes (client_id, tool) SELECT id, 'echo__shout' FROM clients LIMIT 1"
    ),
    "insert upstream": "INSERT INTO upstream_servers (namespace, url) VALUES ('evil', 'http://x')",
    "insert migration": "INSERT INTO schema_migrations (version) VALUES (999)",
    "update client status": "UPDATE clients SET status = 'active'",
    "update scope": "UPDATE client_scopes SET tool = 'echo__shout'",
    "update upstream url": "UPDATE upstream_servers SET url = 'http://attacker.test/mcp'",
    "update token revoked_at": "UPDATE client_tokens SET revoked_at = NULL",
    "update token expires_at": "UPDATE client_tokens SET expires_at = NULL",
    "update token hash": "UPDATE client_tokens SET token_sha256 = sha256('x')",
    "update migration": "UPDATE schema_migrations SET version = version",
    "delete client": "DELETE FROM clients",
    "delete token": "DELETE FROM client_tokens",
    "delete scope": "DELETE FROM client_scopes",
    "delete upstream": "DELETE FROM upstream_servers",
    "delete migration": "DELETE FROM schema_migrations",
    **{f"truncate {table}": f"TRUNCATE {table} CASCADE" for table in READABLE_TABLES},
    "create table": "CREATE TABLE intruder (id integer)",
}


@pytest.fixture
async def seeded_lookup_id(make_client: MakeClient, admin_registry: AdminRegistry) -> str:
    await admin_registry.upsert_upstream("echo", "http://echo.test/mcp", 5000, 30000)
    _, token = await make_client("harborline-support-bot", ["echo__say"])
    return token.lookup_id


@pytest.fixture
async def as_gateway(
    app_database_url: str, seeded_lookup_id: str
) -> AsyncIterator[AsyncConnection]:
    connection = await AsyncConnection.connect(app_database_url, autocommit=True)
    try:
        yield connection
    finally:
        await connection.close()


@pytest.mark.parametrize("table", READABLE_TABLES)
async def test_the_gateway_role_reads_the_registry(as_gateway: AsyncConnection, table: str) -> None:
    query = f"SELECT count(*) FROM {table}"  # noqa: S608 - table names come from the list above
    cursor = await as_gateway.execute(query.encode())

    assert await cursor.fetchone() is not None


@pytest.mark.parametrize("statement", FORBIDDEN_WRITES.values(), ids=FORBIDDEN_WRITES.keys())
async def test_the_gateway_role_cannot_change_the_registry(
    as_gateway: AsyncConnection, statement: str
) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        await as_gateway.execute(statement.encode())


async def test_the_gateway_role_may_record_token_use(
    as_gateway: AsyncConnection, seeded_lookup_id: str
) -> None:
    cursor = await as_gateway.execute(
        "UPDATE client_tokens SET last_used_at = now() WHERE lookup_id = %s", (seeded_lookup_id,)
    )

    assert cursor.rowcount == 1
