"""harborline-setup: idempotent role, schema, migration and seed, run as the owner."""

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict

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
