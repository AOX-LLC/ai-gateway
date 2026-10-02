"""credential_env may only name a *_SERVICE_TOKEN variable: in the database and at runtime."""

import psycopg
import pytest

from ai_gateway.registry import MIGRATIONS_PACKAGE
from ai_gateway.registry.repo import AdminRegistry
from mcp_common.migrate import load_migrations

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

BAD_NAMES = ["GATEWAY_DATABASE_URL", "TICKETING_DB_PASSWORD", "TICKETING_SERVICE_TOKENS", "lower"]


@pytest.mark.parametrize("name", BAD_NAMES)
async def test_the_registry_refuses_a_credential_env_that_is_not_a_service_token(
    admin_registry: AdminRegistry, name: str
) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        await admin_registry.upsert_upstream("tickets", "http://t.test/mcp", 5000, 5000, name)


async def test_the_registry_accepts_a_service_token_name(admin_registry: AdminRegistry) -> None:
    await admin_registry.upsert_upstream(
        "tickets", "http://t.test/mcp", 5000, 5000, "TICKETING_SERVICE_TOKEN"
    )


async def test_the_migration_disables_an_existing_upstream_with_another_credential_name(
    scratch_database: tuple[str, str],
) -> None:
    url, _ = scratch_database
    migrations = load_migrations(MIGRATIONS_PACKAGE)
    before = [m for m in migrations if m.version < 4]
    suffix_migration = next(m for m in migrations if m.version == 4)
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as connection:
        # The runner creates this table first; migration 0002 grants on it.
        await connection.execute("CREATE TABLE schema_migrations (version integer PRIMARY KEY)")
        for migration in before:
            await connection.execute(migration.sql.encode())
        for namespace, env in [("good", "GOOD_SERVICE_TOKEN"), ("bad", "GATEWAY_DATABASE_URL")]:
            await connection.execute(
                "INSERT INTO upstream_servers (namespace, url, credential_env)"
                " VALUES (%s, 'http://x.test/mcp', %s)",
                (namespace, env),
            )

        await connection.execute(suffix_migration.sql.encode())

        cursor = await connection.execute(
            "SELECT namespace, enabled, credential_env FROM upstream_servers ORDER BY namespace"
        )
        assert await cursor.fetchall() == [
            ("bad", False, None),
            ("good", True, "GOOD_SERVICE_TOKEN"),
        ]
