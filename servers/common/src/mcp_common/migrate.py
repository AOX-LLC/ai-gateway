"""Apply the numbered SQL migrations shipped inside a Python package, each exactly once.

The gateway migrates its registry in the public schema; each MCP server migrates its own
schema. Every schema keeps its own schema_migrations table, so the histories never mix.
"""

import logging
import re
import zlib
from dataclasses import dataclass
from importlib.resources import files

from psycopg import AsyncConnection, sql

logger = logging.getLogger(__name__)

_MIGRATION_FILE = re.compile(r"^(?P<version>\d{4})_[a-z0-9_]+\.sql$")
_SCHEMA_NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")

# The public schema keeps the key it has always used; other schemas derive theirs from it.
_MIGRATION_LOCK_KEY = 4_401_0001


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


def load_migrations(package: str) -> list[Migration]:
    """Return the migrations packaged under `<package>/migrations`, in version order."""
    migration_dir = files(package).joinpath("migrations")
    migrations = []
    for entry in migration_dir.iterdir():
        match = _MIGRATION_FILE.match(entry.name)
        if match is None:
            continue
        migrations.append(
            Migration(int(match["version"]), entry.name, entry.read_text(encoding="utf-8"))
        )

    migrations.sort(key=lambda migration: migration.version)
    versions = [migration.version for migration in migrations]
    if len(versions) != len(set(versions)):
        raise RuntimeError(f"duplicate migration versions in {versions}")
    return migrations


def _lock_key(schema: str) -> int:
    """One advisory lock per schema, so two schemas can migrate at the same time."""
    if schema == "public":
        return _MIGRATION_LOCK_KEY
    return _MIGRATION_LOCK_KEY + 1 + zlib.crc32(schema.encode()) % 1_000_000


async def apply_migrations(conninfo: str, package: str, schema: str = "public") -> list[int]:
    """Apply every migration not yet recorded in the schema's schema_migrations table;
    return the versions applied.

    A transaction-scoped advisory lock serialises concurrent runs, and each migration
    commits together with its schema_migrations row, so a failed migration leaves no trace.
    Migrations run with the schema first on the search path, so they use bare table names.
    The schema must already exist.
    """
    if _SCHEMA_NAME.match(schema) is None:
        raise ValueError(f"not a valid schema name: {schema!r}")
    schema_name = sql.Identifier(schema)
    applied_now = []
    async with await AsyncConnection.connect(conninfo) as connection:
        await connection.execute(
            sql.SQL(
                "CREATE TABLE IF NOT EXISTS {}.schema_migrations ("
                " version integer PRIMARY KEY,"
                " applied_at timestamptz NOT NULL DEFAULT now())"
            ).format(schema_name)
        )
        await connection.commit()

        for migration in load_migrations(package):
            async with connection.transaction():
                await connection.execute("SELECT pg_advisory_xact_lock(%s)", (_lock_key(schema),))
                cursor = await connection.execute(
                    sql.SQL("SELECT 1 FROM {}.schema_migrations WHERE version = %s").format(
                        schema_name
                    ),
                    (migration.version,),
                )
                if await cursor.fetchone() is not None:
                    continue

                logger.info("applying migration %s to schema %s", migration.name, schema)
                await connection.execute(sql.SQL("SET LOCAL search_path TO {}").format(schema_name))
                # Migrations are trusted files shipped in the package, never user input.
                await connection.execute(migration.sql.encode())
                await connection.execute(
                    sql.SQL("INSERT INTO {}.schema_migrations (version) VALUES (%s)").format(
                        schema_name
                    ),
                    (migration.version,),
                )
                applied_now.append(migration.version)
    return applied_now
