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
    "tool_policies",
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
    "insert tool policy": (
        "INSERT INTO tool_policies (namespace, tool, effect) VALUES ('echo', 'shout', 'read')"
    ),
    "update tool policy": "UPDATE tool_policies SET effect = 'read'",
    "delete tool policy": "DELETE FROM tool_policies",
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


# --- the ticketing server's role and schema -------------------------------------------------

TICKETING_READABLE = ["staff", "tickets", "comments"]

TICKETING_FORBIDDEN = {
    "update ticket subject": "UPDATE ticketing.tickets SET subject = 'rewritten'",
    "update ticket description": "UPDATE ticketing.tickets SET description = 'rewritten'",
    "update ticket internal notes": "UPDATE ticketing.tickets SET internal_notes = NULL",
    "update ticket requested_by": "UPDATE ticketing.tickets SET requested_by = 'someone-else'",
    "update ticket account": "UPDATE ticketing.tickets SET account_id = 'ACC-00001'",
    "update ticket id": "UPDATE ticketing.tickets SET id = 'TKT-999999'",
    "update ticket created_at": "UPDATE ticketing.tickets SET created_at = now()",
    "update comment body": "UPDATE ticketing.comments SET body = 'rewritten'",
    "update comment visibility": "UPDATE ticketing.comments SET visibility = 'public'",
    "update staff": "UPDATE ticketing.staff SET team = 'nowhere'",
    "insert staff": (
        "INSERT INTO ticketing.staff (handle, display_name, team) VALUES ('a.b', 'A', 't')"
    ),
    "delete ticket": "DELETE FROM ticketing.tickets",
    "delete comment": "DELETE FROM ticketing.comments",
    "delete staff": "DELETE FROM ticketing.staff",
    "truncate tickets": "TRUNCATE ticketing.tickets CASCADE",
    "truncate comments": "TRUNCATE ticketing.comments",
    "create table in ticketing": "CREATE TABLE ticketing.intruder (id integer)",
    "create table in public": "CREATE TABLE public.intruder (id integer)",
    "drop table": "DROP TABLE ticketing.comments",
    "read migrations": "SELECT * FROM ticketing.schema_migrations",
    **{
        f"read registry {table}": f"SELECT count(*) FROM public.{table}"  # noqa: S608 - names come from the list above
        for table in READABLE_TABLES
    },
}


@pytest.fixture
async def as_ticketing(
    ticketing_app_url: str, ticketing_data: object
) -> AsyncIterator[AsyncConnection]:
    connection = await AsyncConnection.connect(ticketing_app_url, autocommit=True)
    try:
        yield connection
    finally:
        await connection.close()


@pytest.mark.parametrize("table", TICKETING_READABLE)
async def test_the_ticketing_role_reads_its_tables(
    as_ticketing: AsyncConnection, table: str
) -> None:
    query = f"SELECT count(*) FROM ticketing.{table}"  # noqa: S608 - names come from the list above
    cursor = await as_ticketing.execute(query.encode())

    assert await cursor.fetchone() is not None


@pytest.mark.parametrize("statement", TICKETING_FORBIDDEN.values(), ids=TICKETING_FORBIDDEN.keys())
async def test_the_ticketing_role_is_confined_to_narrow_writes(
    as_ticketing: AsyncConnection, statement: str
) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        await as_ticketing.execute(statement.encode())


async def test_the_ticketing_role_may_change_status_assignee_and_update_time(
    as_ticketing: AsyncConnection,
) -> None:
    cursor = await as_ticketing.execute(
        "UPDATE ticketing.tickets SET status = 'closed', assignee = NULL, updated_at = now()"
        " WHERE id = 'TKT-000001'"
    )

    assert cursor.rowcount == 1


@pytest.mark.parametrize("table", TICKETING_READABLE)
async def test_the_gateway_role_cannot_see_the_ticketing_schema(
    as_gateway: AsyncConnection, ticketing_data: object, table: str
) -> None:
    query = f"SELECT count(*) FROM ticketing.{table}"  # noqa: S608 - names come from the list above
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        await as_gateway.execute(query.encode())


async def test_the_gateway_role_cannot_write_the_ticketing_schema(
    as_gateway: AsyncConnection, ticketing_data: object
) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        await as_gateway.execute(
            b"INSERT INTO ticketing.tickets (account_id, subject, description, requested_by)"
            b" VALUES ('ACC-00001', 'abc', 'x', 'y')"
        )
