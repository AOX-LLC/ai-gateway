"""Set up the crm schema: role, schema, migrations, grants, seed."""

import logging
from pathlib import Path

from psycopg import AsyncConnection, sql

from crm_server import MIGRATIONS_PACKAGE, ROLE, SCHEMA
from crm_server.seed import (
    build_dataset,
    insert_dataset,
    is_seeded,
    load_extra_records,
    sync_canary,
)
from mcp_common.migrate import apply_migrations
from mcp_common.roles import (
    ensure_role,
    ensure_schema,
    restrict_database_access,
    revoke_role_access,
)

logger = logging.getLogger(__name__)

# The role may only read, and only the public columns: the internal ones (credit limit,
# risk rating, internal notes, a deal's floor price) are left out of the column lists, so
# even a query bug or a `SELECT *` cannot return them.
_GRANTS = [
    "GRANT USAGE ON SCHEMA {schema} TO {role}",
    "GRANT SELECT (id, name, industry, region, tier, about, account_manager, created_at)"
    " ON {schema}.accounts TO {role}",
    "GRANT SELECT ON {schema}.contacts, {schema}.activity_notes TO {role}",
    "GRANT SELECT (id, account_id, name, stage, amount_cents, close_date, owner)"
    " ON {schema}.deals TO {role}",
    # So /healthz can report which migration is applied; read only.
    "GRANT SELECT ON {schema}.schema_migrations TO {role}",
]


async def setup_crm(
    owner_url: str, password: str, extra_records: Path | None, role: str = ROLE
) -> None:
    """Create the role and schema, migrate, grant the role its access, and seed when empty.

    Every step is safe to repeat; the grants are applied on every run, after the role, the
    schema and the migrated tables all exist."""
    if not password:
        raise ValueError("CRM_DB_PASSWORD is empty")
    async with await AsyncConnection.connect(owner_url, autocommit=True) as connection:
        await ensure_role(connection, password, role)
        await ensure_schema(connection, SCHEMA)
    applied = await apply_migrations(owner_url, MIGRATIONS_PACKAGE, SCHEMA)
    logger.info("crm migrations applied: %s", applied or "none")
    async with await AsyncConnection.connect(owner_url, autocommit=True) as connection:
        await grant_crm_access(connection, role)
        await restrict_database_access(connection, [role])
    async with await AsyncConnection.connect(owner_url) as connection:
        await connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(SCHEMA)))
        if await is_seeded(connection):
            if await sync_canary(connection):
                logger.info("crm is already seeded; the canary was added")
            else:
                logger.info("crm is already seeded")
            return
        dataset = build_dataset()
        if extra_records is not None:
            load_extra_records(dataset, extra_records)
        await insert_dataset(connection, dataset)
        logger.info(
            "seeded %d accounts, %d contacts, %d deals, %d notes",
            len(dataset.accounts),
            len(dataset.contacts),
            len(dataset.deals),
            len(dataset.notes),
        )


async def grant_crm_access(connection: AsyncConnection, role: str = ROLE) -> None:
    """Make the crm role's grants exactly these. The role, schema and tables must exist.

    Everything the role holds in the schema is revoked first, so a grant that was widened
    by hand is narrowed again on the next run."""
    schema, name = sql.Identifier(SCHEMA), sql.Identifier(role)
    await revoke_role_access(connection, SCHEMA, role)
    for template in _GRANTS:
        await connection.execute(sql.SQL(template).format(schema=schema, role=name))
