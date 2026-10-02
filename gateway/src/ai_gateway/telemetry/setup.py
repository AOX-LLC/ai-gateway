"""Set up the telemetry schema: its roles, the schema, the migrations and the grants.

Runs as the database owner (`gateway-admin telemetry-setup`) and is safe to repeat. The
grants are applied on every run, after revoking everything the roles hold, so a grant that
was widened by hand is narrowed again.
"""

import logging
from dataclasses import dataclass

from psycopg import AsyncConnection, sql

from ai_gateway.telemetry import (
    MIGRATIONS_PACKAGE,
    PURGER_ROLE,
    READER_ROLE,
    SCHEMA,
    WRITER_ROLE,
)
from mcp_common.migrate import apply_migrations
from mcp_common.roles import (
    ensure_role,
    ensure_schema,
    restrict_database_access,
    revoke_role_access,
)

logger = logging.getLogger(__name__)

TABLES = ["requests", "layer_verdicts", "auth_failures", "spans"]
"""The tables whose old rows the purge deletes."""
APPEND_ONLY_TABLES = [*TABLES, "pipeline_configs"]
DASHBOARD_VIEWS = [
    "dash_requests",
    "dash_layer_verdicts",
    "dash_auth_failures",
    "dash_pipeline_layers",
]

READER_CONNECTION_LIMIT = 5


@dataclass(frozen=True)
class TelemetryPasswords:
    writer: str
    reader: str
    purger: str


async def setup_telemetry(owner_url: str, passwords: TelemetryPasswords) -> None:
    """Create the roles and the schema, migrate, and grant, in that order."""
    for name, password in (
        (WRITER_ROLE, passwords.writer),
        (READER_ROLE, passwords.reader),
        (PURGER_ROLE, passwords.purger),
    ):
        if not password:
            raise ValueError(f"the password of {name} is empty")
    async with await AsyncConnection.connect(owner_url, autocommit=True) as connection:
        for role, password in (
            (WRITER_ROLE, passwords.writer),
            (READER_ROLE, passwords.reader),
            (PURGER_ROLE, passwords.purger),
        ):
            await ensure_role(connection, password, role)
        await ensure_schema(connection, SCHEMA)
    applied = await apply_migrations(owner_url, MIGRATIONS_PACKAGE, SCHEMA)
    logger.info("telemetry migrations applied: %s", applied or "none")
    async with await AsyncConnection.connect(owner_url, autocommit=True) as connection:
        await grant_telemetry_access(connection)
        await restrict_database_access(connection, [WRITER_ROLE, READER_ROLE, PURGER_ROLE])


async def grant_telemetry_access(connection: AsyncConnection) -> None:
    """Make the three roles' grants exactly these. The roles, schema and tables must exist.

    - writer: INSERT on the tables, and nothing else (it cannot read what it wrote).
    - reader: SELECT on the dashboard's views, and nothing else; its sessions are read-only,
      short and few.
    - purger: DELETE on the purgeable tables, and SELECT on their `ts` column only, which a
      DELETE ... WHERE ts < ... needs.
    """
    schema = sql.Identifier(SCHEMA)
    for role in (WRITER_ROLE, READER_ROLE, PURGER_ROLE):
        await revoke_role_access(connection, SCHEMA, role)
        await connection.execute(
            sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(schema, sql.Identifier(role))
        )

    def tables(names: list[str]) -> sql.Composable:
        return sql.SQL(", ").join(sql.Identifier(SCHEMA, name) for name in names)

    await connection.execute(
        sql.SQL("GRANT INSERT ON {} TO {}").format(
            tables(APPEND_ONLY_TABLES), sql.Identifier(WRITER_ROLE)
        )
    )
    await connection.execute(
        sql.SQL("GRANT SELECT ON {} TO {}").format(
            tables(DASHBOARD_VIEWS), sql.Identifier(READER_ROLE)
        )
    )
    await connection.execute(
        sql.SQL("GRANT DELETE, SELECT (ts) ON {} TO {}").format(
            tables(TABLES), sql.Identifier(PURGER_ROLE)
        )
    )
    reader = sql.Identifier(READER_ROLE)
    await connection.execute(
        sql.SQL("ALTER ROLE {} CONNECTION LIMIT {}").format(
            reader, sql.Literal(READER_CONNECTION_LIMIT)
        )
    )
    for setting, value in (
        ("default_transaction_read_only", "on"),
        ("statement_timeout", "5s"),
        ("idle_in_transaction_session_timeout", "10s"),
    ):
        await connection.execute(
            sql.SQL("ALTER ROLE {} SET {} = {}").format(
                reader, sql.Identifier(setting), sql.Literal(value)
            )
        )
    await connection.execute(
        sql.SQL("ALTER ROLE {} SET statement_timeout = {}").format(
            sql.Identifier(PURGER_ROLE), sql.Literal("60s")
        )
    )
