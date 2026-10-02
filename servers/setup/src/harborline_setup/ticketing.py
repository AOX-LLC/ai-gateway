"""Set up the ticketing schema: role, schema, migrations, seed."""

import logging
from pathlib import Path

from psycopg import AsyncConnection, sql

from mcp_common.migrate import apply_migrations
from ticketing_server import MIGRATIONS_PACKAGE, ROLE, SCHEMA
from ticketing_server.seed import build_dataset, insert_dataset, is_seeded, load_extra_records

logger = logging.getLogger(__name__)

GATEWAY_ROLE = "gateway_app"

# The role may read everything, create tickets and comments, and change only a ticket's
# status, assignee and update time. It cannot delete, truncate or create.
# Inserts are granted per column: exactly the columns create_ticket and add_comment set.
# That leaves internal_notes unwritable, and a comment's visibility at its 'public' default.
_GRANTS = [
    "GRANT USAGE ON SCHEMA {schema} TO {role}",
    "GRANT SELECT ON {schema}.staff, {schema}.tickets, {schema}.comments TO {role}",
    # So /healthz can report which migration is applied; read only.
    "GRANT SELECT ON {schema}.schema_migrations TO {role}",
    "GRANT INSERT (subject, description, priority, account_id, requested_by)"
    " ON {schema}.tickets TO {role}",
    "GRANT INSERT (ticket_id, author, body, requested_by) ON {schema}.comments TO {role}",
    "GRANT UPDATE (status, assignee, updated_at) ON {schema}.tickets TO {role}",
    "GRANT USAGE ON SEQUENCE {schema}.ticket_number, {schema}.comments_id_seq TO {role}",
]


async def setup_ticketing(
    owner_url: str, password: str, extra_records: Path | None, role: str = ROLE
) -> None:
    """Create the role and schema, migrate, grant the role its access, and seed when empty.

    Every step is safe to repeat; the grants are applied on every run, after the role, the
    schema and the migrated tables all exist."""
    if not password:
        raise ValueError("TICKETING_DB_PASSWORD is empty")
    async with await AsyncConnection.connect(owner_url, autocommit=True) as connection:
        await _ensure_role(connection, password, role)
        await _ensure_schema(connection)
    applied = await apply_migrations(owner_url, MIGRATIONS_PACKAGE, SCHEMA)
    logger.info("ticketing migrations applied: %s", applied or "none")
    async with await AsyncConnection.connect(owner_url, autocommit=True) as connection:
        await grant_ticketing_access(connection, role)
        await restrict_database_access(connection, [role])
    async with await AsyncConnection.connect(owner_url) as connection:
        await connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(SCHEMA)))
        if await is_seeded(connection):
            logger.info("ticketing is already seeded")
            return
        dataset = build_dataset()
        if extra_records is not None:
            load_extra_records(dataset, extra_records)
        await insert_dataset(connection, dataset)
        logger.info(
            "seeded %d staff, %d tickets, %d comments",
            len(dataset.staff),
            len(dataset.tickets),
            len(dataset.comments),
        )


async def _ensure_role(connection: AsyncConnection, password: str, role: str) -> None:
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
    await connection.execute(
        sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
            sql.Identifier(await _database(connection)), sql.Identifier(role)
        )
    )


async def restrict_database_access(connection: AsyncConnection, roles: list[str]) -> None:
    """Take the default access away from PUBLIC and grant it, by name, to the roles that
    need it: connecting to this database, and the public schema (the gateway's registry).

    Without this, every role could connect, create temporary tables and look inside the
    public schema. A listed role that does not exist (yet) is skipped; setup runs again
    when it does."""
    database = sql.Identifier(await _database(connection))
    await connection.execute(
        sql.SQL("REVOKE TEMPORARY, CONNECT ON DATABASE {} FROM PUBLIC").format(database)
    )
    await connection.execute("REVOKE USAGE ON SCHEMA public FROM PUBLIC")
    existing = await _existing_roles(connection, [*roles, GATEWAY_ROLE])
    for name in existing:
        await connection.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(database, sql.Identifier(name))
        )
    if GATEWAY_ROLE in existing:
        await connection.execute(
            sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(GATEWAY_ROLE))
        )


async def _existing_roles(connection: AsyncConnection, roles: list[str]) -> list[str]:
    cursor = await connection.execute(
        "SELECT rolname FROM pg_roles WHERE rolname = ANY(%s) ORDER BY rolname", (roles,)
    )
    return [str(name) for (name,) in await cursor.fetchall()]


async def grant_ticketing_access(connection: AsyncConnection, role: str = ROLE) -> None:
    """(Re)apply the ticketing role's grants. The role, schema and tables must exist."""
    for template in _GRANTS:
        await connection.execute(
            sql.SQL(template).format(schema=sql.Identifier(SCHEMA), role=sql.Identifier(role))
        )


async def _database(connection: AsyncConnection) -> str:
    cursor = await connection.execute("SELECT current_database()")
    row = await cursor.fetchone()
    if row is None:
        raise RuntimeError("current_database() returned no row")
    return str(row[0])


async def _ensure_schema(connection: AsyncConnection) -> None:
    await connection.execute(
        sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(SCHEMA))
    )
    await connection.execute(
        sql.SQL("REVOKE ALL ON SCHEMA {} FROM PUBLIC").format(sql.Identifier(SCHEMA))
    )
