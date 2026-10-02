"""harborline-setup: idempotent role, schema, migration and seed, run as the owner."""

from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from crm_server.seed import ACCOUNT_COUNT, CONTACT_COUNT, DEAL_COUNT, NOTE_COUNT
from harborline_setup.crm import setup_crm
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


async def _privileges(url: str, role: str) -> dict[str, bool]:
    async with await psycopg.AsyncConnection.connect(url) as connection:
        cursor = await connection.execute(
            "SELECT has_schema_privilege(%(r)s, 'ticketing', 'USAGE'),"
            " has_table_privilege(%(r)s, 'ticketing.tickets', 'SELECT'),"
            " has_column_privilege(%(r)s, 'ticketing.comments', 'body', 'INSERT'),"
            " has_column_privilege(%(r)s, 'ticketing.comments', 'visibility', 'INSERT'),"
            " has_column_privilege(%(r)s, 'ticketing.tickets', 'internal_notes', 'INSERT'),"
            " has_column_privilege(%(r)s, 'ticketing.tickets', 'status', 'UPDATE'),"
            " has_table_privilege(%(r)s, 'ticketing.tickets', 'DELETE'),"
            " has_sequence_privilege(%(r)s, 'ticketing.ticket_number', 'USAGE')",
            {"r": role},
        )
        row = await cursor.fetchone()
    assert row is not None
    names = [
        "usage",
        "select",
        "insert_comment_body",
        "insert_visibility",
        "insert_internal_notes",
        "update_status",
        "delete",
        "sequence",
    ]
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
        "insert_comment_body": True,
        "insert_visibility": False,
        "insert_internal_notes": False,
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


async def test_setup_stores_a_scram_verifier_and_the_role_can_log_in(
    scratch_database: tuple[str, str],
) -> None:
    url, role = scratch_database
    password = f"scratch-{uuid4().hex}"

    await setup_ticketing(url, password, None, role)

    async with await psycopg.AsyncConnection.connect(url) as owner:
        cursor = await owner.execute(
            "SELECT rolpassword FROM pg_authid WHERE rolname = %s", (role,)
        )
        row = await cursor.fetchone()
    assert row is not None
    assert row[0].startswith("SCRAM-SHA-256$")
    assert password not in row[0]
    as_role = make_conninfo(url, user=role, password=password)
    async with await psycopg.AsyncConnection.connect(as_role) as connection:
        cursor = await connection.execute("SELECT current_user")
        assert await cursor.fetchone() == (role,)
    with pytest.raises(psycopg.OperationalError):
        await psycopg.AsyncConnection.connect(
            make_conninfo(url, user=role, password=f"not-{password}")
        )


async def test_setup_takes_the_default_database_access_away_from_public(
    scratch_database: tuple[str, str],
) -> None:
    url, role = scratch_database

    await setup_ticketing(url, f"scratch-{uuid4().hex}", None, role)

    async with await psycopg.AsyncConnection.connect(url) as connection:
        cursor = await connection.execute(
            "SELECT has_database_privilege(0::oid, current_database(), 'CONNECT'),"
            " has_database_privilege(0::oid, current_database(), 'TEMPORARY'),"
            " has_schema_privilege(0::oid, 'public', 'USAGE'),"
            " has_database_privilege(%(r)s, current_database(), 'CONNECT'),"
            " has_schema_privilege(%(r)s, 'public', 'USAGE')",
            {"r": role},
        )
        assert await cursor.fetchone() == (False, False, False, True, False)


# --- the CRM step ---------------------------------------------------------------------------


async def _crm_counts(url: str) -> tuple[int, int, int, int]:
    async with await psycopg.AsyncConnection.connect(url) as connection:
        counts = []
        for table in ("accounts", "contacts", "deals", "activity_notes"):
            cursor = await connection.execute(f"SELECT count(*) FROM crm.{table}".encode())  # noqa: S608
            row = await cursor.fetchone()
            assert row is not None
            counts.append(int(row[0]))
    return counts[0], counts[1], counts[2], counts[3]


async def _crm_privileges(url: str, role: str) -> dict[str, bool]:
    async with await psycopg.AsyncConnection.connect(url) as connection:
        cursor = await connection.execute(
            "SELECT has_schema_privilege(%(r)s, 'crm', 'USAGE'),"
            " has_column_privilege(%(r)s, 'crm.accounts', 'name', 'SELECT'),"
            " has_column_privilege(%(r)s, 'crm.accounts', 'internal_notes', 'SELECT'),"
            " has_column_privilege(%(r)s, 'crm.accounts', 'credit_limit_internal', 'SELECT'),"
            " has_column_privilege(%(r)s, 'crm.deals', 'floor_price_cents', 'SELECT'),"
            " has_column_privilege(%(r)s, 'crm.deals', 'amount_cents', 'SELECT'),"
            " has_table_privilege(%(r)s, 'crm.accounts', 'INSERT'),"
            " has_table_privilege(%(r)s, 'crm.activity_notes', 'UPDATE')",
            {"r": role},
        )
        row = await cursor.fetchone()
    assert row is not None
    names = [
        "usage", "select_name", "select_internal_notes", "select_credit_limit",
        "select_floor_price", "select_amount", "insert", "update",
    ]  # fmt: skip
    return dict(zip(names, row, strict=True))


async def test_crm_setup_builds_everything_once_and_is_safe_to_repeat(
    scratch_database: tuple[str, str],
) -> None:
    url, role = scratch_database
    password = f"scratch-{uuid4().hex}"

    await setup_crm(url, password, None, role)
    first_counts = await _crm_counts(url)
    first_privileges = await _crm_privileges(url, role)
    await setup_crm(url, password, None, role)

    assert first_counts == (ACCOUNT_COUNT, CONTACT_COUNT, DEAL_COUNT, NOTE_COUNT)
    assert await _crm_counts(url) == first_counts
    expected = {
        "usage": True,
        "select_name": True,
        "select_internal_notes": False,
        "select_credit_limit": False,
        "select_floor_price": False,
        "select_amount": True,
        "insert": False,
        "update": False,
    }
    assert first_privileges == expected
    assert await _crm_privileges(url, role) == expected


async def test_crm_setup_narrows_a_grant_that_was_widened(
    scratch_database: tuple[str, str],
) -> None:
    url, role = scratch_database
    password = f"scratch-{uuid4().hex}"
    await setup_crm(url, password, None, role)
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as connection:
        await connection.execute(
            sql.SQL("GRANT SELECT, INSERT ON crm.accounts TO {}").format(sql.Identifier(role))
        )
    assert (await _crm_privileges(url, role))["select_internal_notes"] is True

    await setup_crm(url, password, None, role)

    privileges = await _crm_privileges(url, role)
    assert privileges["select_internal_notes"] is False
    assert privileges["insert"] is False
    assert privileges["select_name"] is True


async def test_crm_setup_refuses_an_empty_password(test_database_url: str) -> None:
    with pytest.raises(ValueError, match="CRM_DB_PASSWORD"):
        await setup_crm(test_database_url, "", None)
