"""Set up the handbook schema: role, schema, vector extension, migrations, grants, seed."""

import logging
from pathlib import Path

from psycopg import AsyncConnection, sql

from handbook_server import MIGRATIONS_PACKAGE, ROLE, SCHEMA
from handbook_server.embedding import Embedder
from harborline_setup.handbook_seed import (
    build_dataset,
    insert_dataset,
    is_seeded,
    sync_superseded,
)
from mcp_common.migrate import apply_migrations
from mcp_common.roles import (
    ensure_role,
    ensure_schema,
    restrict_database_access,
    revoke_role_access,
)

logger = logging.getLogger(__name__)

# The role may read two views and the migration list, nothing else: no base table, so no
# restricted document or chunk. The views leave restricted documents out.
_GRANTS = [
    "GRANT USAGE ON SCHEMA {schema} TO {role}",
    "GRANT SELECT ON {schema}.published_documents, {schema}.searchable_chunks TO {role}",
    # So /healthz can report which migration is applied; read only.
    "GRANT SELECT ON {schema}.schema_migrations TO {role}",
]


async def setup_handbook(
    owner_url: str, password: str, model_path: Path, documents_path: Path, role: str = ROLE
) -> None:
    """Create the role, schema and extension, migrate, grant, and seed when empty.

    Every step is safe to repeat; the grants are applied on every run. The embedding model
    is read only when there is something to seed; the documents folder is read on every run,
    to keep which documents are superseded in step with the files."""
    if not password:
        raise ValueError("HANDBOOK_DB_PASSWORD is empty")
    async with await AsyncConnection.connect(owner_url, autocommit=True) as connection:
        await ensure_role(connection, password, role)
        await prepare_schema(connection)
    applied = await apply_migrations(owner_url, MIGRATIONS_PACKAGE, SCHEMA)
    logger.info("handbook migrations applied: %s", applied or "none")
    async with await AsyncConnection.connect(owner_url, autocommit=True) as connection:
        await grant_handbook_access(connection, role)
        await restrict_database_access(connection, [role])
    async with await AsyncConnection.connect(owner_url) as connection:
        await connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(SCHEMA)))
        if await is_seeded(connection):
            changed = await sync_superseded(connection, documents_path)
            logger.info("handbook is already seeded; %d superseded markers updated", changed)
            return
        dataset = build_dataset(Embedder(model_path), documents_path)
        await insert_dataset(connection, dataset)
        logger.info("seeded %d documents, %d chunks", len(dataset.documents), len(dataset.chunks))


async def prepare_schema(connection: AsyncConnection) -> None:
    """The schema, closed to PUBLIC, with the pgvector extension in it.

    The extension is created here, by the owner, in the schema itself: PUBLIC no longer has
    USAGE on `public`, so the server's role could not see it anywhere else."""
    await ensure_schema(connection, SCHEMA)
    await connection.execute(
        sql.SQL("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA {}").format(
            sql.Identifier(SCHEMA)
        )
    )


async def grant_handbook_access(connection: AsyncConnection, role: str = ROLE) -> None:
    """Make the handbook role's grants exactly these. The role, schema and views must exist.

    Everything the role holds in the schema is revoked first, so a grant that was widened by
    hand, for example onto a base table, is narrowed again."""
    schema, name = sql.Identifier(SCHEMA), sql.Identifier(role)
    await revoke_role_access(connection, SCHEMA, role)
    for template in _GRANTS:
        await connection.execute(sql.SQL(template).format(schema=schema, role=name))
