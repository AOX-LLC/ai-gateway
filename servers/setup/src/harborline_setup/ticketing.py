"""Set up the ticketing schema: role, schema, migrations, seed."""

import logging
from pathlib import Path

from psycopg import AsyncConnection, sql

from mcp_common.migrate import apply_migrations
from mcp_common.roles import (
    ensure_role,
    ensure_schema,
    restrict_database_access,
    revoke_role_access,
)
from ticketing_server import MIGRATIONS_PACKAGE, ROLE, SCHEMA
from ticketing_server.seed import build_dataset, insert_dataset, is_seeded, load_extra_records

logger = logging.getLogger(__name__)

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
        await ensure_role(connection, password, role)
        await ensure_schema(connection, SCHEMA)
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


async def grant_ticketing_access(connection: AsyncConnection, role: str = ROLE) -> None:
    """Make the ticketing role's grants exactly these. The role, schema and tables must exist.

    Everything the role holds in the schema is revoked first, so a grant that was widened by
    hand, at table, column, sequence or schema level, is narrowed again."""
    await revoke_role_access(connection, SCHEMA, role)
    for template in _GRANTS:
        await connection.execute(
            sql.SQL(template).format(schema=sql.Identifier(SCHEMA), role=sql.Identifier(role))
        )
