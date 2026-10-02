"""Apply the numbered SQL migrations shipped inside this package, each exactly once."""

import logging
import re
from dataclasses import dataclass
from importlib.resources import files

from psycopg import AsyncConnection

logger = logging.getLogger(__name__)

_MIGRATION_FILE = re.compile(r"^(?P<version>\d{4})_[a-z0-9_]+\.sql$")

# Any constant works; it only has to be the same for every process that migrates.
_MIGRATION_LOCK_KEY = 4_401_0001


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


def load_migrations() -> list[Migration]:
    """Return the packaged migrations in version order."""
    migration_dir = files("ai_gateway.registry").joinpath("migrations")
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


async def apply_migrations(conninfo: str) -> list[int]:
    """Apply every migration not yet recorded in schema_migrations; return the versions applied.

    A transaction-scoped advisory lock serialises concurrent runs, and each migration
    commits together with its schema_migrations row, so a failed migration leaves no trace.
    """
    applied_now = []
    async with await AsyncConnection.connect(conninfo) as connection:
        await connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version integer PRIMARY KEY,"
            " applied_at timestamptz NOT NULL DEFAULT now())"
        )
        await connection.commit()

        for migration in load_migrations():
            async with connection.transaction():
                await connection.execute("SELECT pg_advisory_xact_lock(%s)", (_MIGRATION_LOCK_KEY,))
                cursor = await connection.execute(
                    "SELECT 1 FROM schema_migrations WHERE version = %s", (migration.version,)
                )
                if await cursor.fetchone() is not None:
                    continue

                logger.info("applying migration %s", migration.name)
                # Migrations are trusted files shipped in the package, never user input.
                await connection.execute(migration.sql.encode())
                await connection.execute(
                    "INSERT INTO schema_migrations (version) VALUES (%s)", (migration.version,)
                )
                applied_now.append(migration.version)
    return applied_now
