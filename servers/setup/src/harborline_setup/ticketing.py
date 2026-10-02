"""Set up the ticketing schema: role, schema, migrations, seed."""

import logging
from pathlib import Path

from psycopg import AsyncConnection, sql

from mcp_common.migrate import apply_migrations
from ticketing_server import MIGRATIONS_PACKAGE, ROLE, SCHEMA
from ticketing_server.seed import build_dataset, insert_dataset, is_seeded, load_extra_records

logger = logging.getLogger(__name__)

_CREATE_ROLE_IF_MISSING = f"""
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{ROLE}') THEN
        CREATE ROLE {ROLE} LOGIN;
    END IF;
END
$$
"""


async def setup_ticketing(owner_url: str, password: str, extra_records: Path | None) -> None:
    if not password:
        raise ValueError("TICKETING_DB_PASSWORD is empty")
    async with await AsyncConnection.connect(owner_url, autocommit=True) as connection:
        await _ensure_role(connection, password)
        await _ensure_schema(connection)
    applied = await apply_migrations(owner_url, MIGRATIONS_PACKAGE, SCHEMA)
    logger.info("ticketing migrations applied: %s", applied or "none")
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


async def _ensure_role(connection: AsyncConnection, password: str) -> None:
    await connection.execute(_CREATE_ROLE_IF_MISSING.encode())
    # DDL cannot take bind parameters; Literal quotes the value safely on the client.
    await connection.execute(
        sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}").format(
            sql.Identifier(ROLE), sql.Literal(password)
        )
    )
    await connection.execute(
        sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
            sql.Identifier(await _database(connection)),
            sql.Identifier(ROLE),
        )
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
