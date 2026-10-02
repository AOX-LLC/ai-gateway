"""Steps the setup of every schema shares: its role, its schema, and the database's defaults.

Used by harborline-setup for the servers and by gateway-admin for the telemetry schema."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from psycopg import AsyncConnection, sql

GATEWAY_ROLE = "gateway_app"

_DATABASE_ACCESS_LOCK = 7_165_201_001
"""The advisory lock that serialises changes to the database-level access list."""


@asynccontextmanager
async def _database_access_lock(connection: AsyncConnection) -> AsyncGenerator[None]:
    """Hold a session advisory lock while changing who may connect to the database or use the
    public schema.

    Compose runs several setups side by side (the servers' and the telemetry's), and each
    grants CONNECT on the same database and revokes defaults from PUBLIC. Postgres keeps those
    privileges in one catalog row per object, and two sessions updating it at once fail with
    "tuple concurrently updated". The connection must not be inside a transaction that ends
    before the lock is released: callers use autocommit."""
    await connection.execute("SELECT pg_advisory_lock(%s)", (_DATABASE_ACCESS_LOCK,))
    try:
        yield
    finally:
        await connection.execute("SELECT pg_advisory_unlock(%s)", (_DATABASE_ACCESS_LOCK,))


async def ensure_role(connection: AsyncConnection, password: str, role: str) -> None:
    cursor = await connection.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
    if await cursor.fetchone() is None:
        await connection.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(role)))
    # Send a SCRAM verifier, never the password: the plaintext would otherwise appear in
    # the server's logs and statistics if statements are logged. DDL cannot take bind
    # parameters; Literal quotes the value safely on the client.
    verifier = connection.pgconn.encrypt_password(
        password.encode(), role.encode(), b"scram-sha-256"
    ).decode()
    await connection.execute(
        sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}").format(
            sql.Identifier(role), sql.Literal(verifier)
        )
    )
    database = sql.Identifier(await database_name(connection))
    async with _database_access_lock(connection):
        await connection.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(database, sql.Identifier(role))
        )


async def restrict_database_access(connection: AsyncConnection, roles: list[str]) -> None:
    """Take the default access away from PUBLIC and grant it, by name, to the roles that
    need it: connecting to this database, and the public schema (the gateway's registry).

    Without this, every role could connect, create temporary tables and look inside the
    public schema. A listed role that does not exist (yet) is skipped; setup runs again
    when it does."""
    database = sql.Identifier(await database_name(connection))
    async with _database_access_lock(connection):
        await connection.execute(
            sql.SQL("REVOKE TEMPORARY, CONNECT ON DATABASE {} FROM PUBLIC").format(database)
        )
        await connection.execute("REVOKE USAGE ON SCHEMA public FROM PUBLIC")
        existing = await existing_roles(connection, [*roles, GATEWAY_ROLE])
        for name in existing:
            await connection.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(database, sql.Identifier(name))
            )
        if GATEWAY_ROLE in existing:
            await connection.execute(
                sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(GATEWAY_ROLE))
            )


async def existing_roles(connection: AsyncConnection, roles: list[str]) -> list[str]:
    cursor = await connection.execute(
        "SELECT rolname FROM pg_roles WHERE rolname = ANY(%s) ORDER BY rolname", (roles,)
    )
    return [str(name) for (name,) in await cursor.fetchall()]


async def database_name(connection: AsyncConnection) -> str:
    cursor = await connection.execute("SELECT current_database()")
    row = await cursor.fetchone()
    if row is None:
        raise RuntimeError("current_database() returned no row")
    return str(row[0])


async def ensure_schema(connection: AsyncConnection, schema: str) -> None:
    await connection.execute(
        sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema))
    )
    await connection.execute(
        sql.SQL("REVOKE ALL ON SCHEMA {} FROM PUBLIC").format(sql.Identifier(schema))
    )


async def revoke_role_access(connection: AsyncConnection, schema: str, role: str) -> None:
    """Take back everything `role` holds in `schema`: its table, view and column privileges,
    its sequence privileges, and the schema's own USAGE and CREATE. The grant step runs this
    first and then grants exactly what the role needs, so anything widened by hand, at any
    of those levels, is narrowed again."""
    schema_name, role_name = sql.Identifier(schema), sql.Identifier(role)
    for statement in (
        "REVOKE ALL ON ALL TABLES IN SCHEMA {} FROM {}",
        "REVOKE ALL ON ALL SEQUENCES IN SCHEMA {} FROM {}",
        "REVOKE ALL ON SCHEMA {} FROM {}",
    ):
        await connection.execute(sql.SQL(statement).format(schema_name, role_name))
