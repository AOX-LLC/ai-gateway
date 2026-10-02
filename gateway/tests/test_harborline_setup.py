"""harborline-setup: idempotent role, schema, migration and seed, run as the owner."""

from collections.abc import AsyncIterator
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from harborline_setup.ticketing import setup_ticketing
from ticketing_server.seed import COMMENT_COUNT, TICKET_COUNT

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


async def _counts(url: str) -> tuple[int, int, int]:
    async with await psycopg.AsyncConnection.connect(url) as connection:
        counts = []
        for table in ("staff", "tickets", "comments"):
            cursor = await connection.execute(f"SELECT count(*) FROM ticketing.{table}".encode())  # noqa: S608
            row = await cursor.fetchone()
            assert row is not None
            counts.append(int(row[0]))
    return counts[0], counts[1], counts[2]


async def test_setup_seeds_an_empty_schema_once_and_is_safe_to_repeat(
    test_database_url: str, ticketing_app_url: str, ticketing_schema: None
) -> None:
    # The role's password is the one the rest of the suite logs in with, so reuse it.
    password = str(conninfo_to_dict(ticketing_app_url)["password"])
    async with await psycopg.AsyncConnection.connect(test_database_url) as connection:
        await connection.execute("TRUNCATE ticketing.comments, ticketing.tickets, ticketing.staff")

    await setup_ticketing(test_database_url, password, None)
    first = await _counts(test_database_url)
    await setup_ticketing(test_database_url, password, None)
    second = await _counts(test_database_url)

    assert first == (12, TICKET_COUNT, COMMENT_COUNT)
    assert second == first


async def test_setup_refuses_an_empty_password(test_database_url: str) -> None:
    with pytest.raises(ValueError, match="TICKETING_DB_PASSWORD"):
        await setup_ticketing(test_database_url, "", None)


@pytest.fixture
async def scratch_database(test_database_url: str) -> AsyncIterator[tuple[str, str]]:
    """An empty database and a role name that exist nowhere yet, removed afterwards."""
    suffix = uuid4().hex[:8]
    database, role = f"setup_scratch_{suffix}", f"ticketing_scratch_{suffix}"
    async with await psycopg.AsyncConnection.connect(test_database_url, autocommit=True) as conn:
        await conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    try:
        yield make_conninfo(test_database_url, dbname=database), role
    finally:
        async with await psycopg.AsyncConnection.connect(
            test_database_url, autocommit=True
        ) as conn:
            await conn.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database))
            )
            await conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


async def _privileges(url: str, role: str) -> dict[str, bool]:
    async with await psycopg.AsyncConnection.connect(url) as connection:
        cursor = await connection.execute(
            "SELECT has_schema_privilege(%(r)s, 'ticketing', 'USAGE'),"
            " has_table_privilege(%(r)s, 'ticketing.tickets', 'SELECT'),"
            " has_table_privilege(%(r)s, 'ticketing.comments', 'INSERT'),"
            " has_column_privilege(%(r)s, 'ticketing.tickets', 'status', 'UPDATE'),"
            " has_table_privilege(%(r)s, 'ticketing.tickets', 'DELETE'),"
            " has_sequence_privilege(%(r)s, 'ticketing.ticket_number', 'USAGE')",
            {"r": role},
        )
        row = await cursor.fetchone()
    assert row is not None
    names = ["usage", "select", "insert_comments", "update_status", "delete", "sequence"]
    return dict(zip(names, row, strict=True))


async def test_setup_builds_everything_from_a_database_without_the_role_or_schema(
    scratch_database: tuple[str, str], test_database_url: str
) -> None:
    url, role = scratch_database
    password = f"scratch-{uuid4().hex}"

    await setup_ticketing(url, password, None, role)
    first = await _privileges(url, role)
    first_counts = await _counts(url)
    await setup_ticketing(url, password, None, role)

    expected = {
        "usage": True,
        "select": True,
        "insert_comments": True,
        "update_status": True,
        "delete": False,
        "sequence": True,
    }
    assert first == expected
    assert await _privileges(url, role) == expected
    assert first_counts == (12, TICKET_COUNT, COMMENT_COUNT)
    assert await _counts(url) == first_counts


async def test_setup_restores_grants_that_were_revoked(
    scratch_database: tuple[str, str],
) -> None:
    url, role = scratch_database
    password = f"scratch-{uuid4().hex}"
    await setup_ticketing(url, password, None, role)
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as connection:
        await connection.execute(
            sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA ticketing FROM {}").format(
                sql.Identifier(role)
            )
        )

    await setup_ticketing(url, password, None, role)

    assert (await _privileges(url, role))["select"] is True
