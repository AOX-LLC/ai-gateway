"""The telemetry schema: what each role can and cannot do, and what a row can hold."""

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
import pytest
from psycopg import errors, sql
from psycopg.conninfo import conninfo_to_dict
from psycopg.types.json import Jsonb

from ai_gateway.telemetry import MIGRATIONS_PACKAGE, SCHEMA
from ai_gateway.telemetry.setup import TelemetryPasswords, grant_telemetry_access, setup_telemetry
from mcp_common.migrate import load_migrations

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
SHA = "a" * 64


def _url(variable: str) -> str:
    url = os.environ.get(variable)
    if not url:
        pytest.skip(f"{variable} is not set")
    return url


@pytest.fixture
def writer_url() -> str:
    return _url("TELEMETRY_WRITER_TEST_DATABASE_URL")


@pytest.fixture
def reader_url() -> str:
    return _url("TELEMETRY_READER_TEST_DATABASE_URL")


@pytest.fixture
def purger_url() -> str:
    return _url("TELEMETRY_PURGER_TEST_DATABASE_URL")


def _password(url: str) -> str:
    return str(conninfo_to_dict(url)["password"])


@pytest.fixture
async def telemetry(
    test_database_url: str, writer_url: str, reader_url: str, purger_url: str
) -> None:
    """A fresh telemetry schema, set up the way `gateway-admin telemetry-setup` sets it up.
    The roles use the passwords the rest of the suite logs in with."""
    async with await psycopg.AsyncConnection.connect(test_database_url, autocommit=True) as conn:
        await conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE".encode())
    await setup_telemetry(
        test_database_url,
        TelemetryPasswords(_password(writer_url), _password(reader_url), _password(purger_url)),
    )


async def _as(url: str) -> psycopg.AsyncConnection:
    return await psycopg.AsyncConnection.connect(url, autocommit=True)


async def _insert_request(url: str, request_id: object | None = None, **overrides: object) -> None:
    values: dict[str, object] = {
        "request_id": request_id or uuid4(),
        "ts": NOW,
        "kind": "tool_call",
        "client_name": "harborline-support-bot",
        "tool": "tickets__get_ticket",
        "namespace": "tickets",
        "effect": "read",
        "outcome": "forwarded",
        "duration_ms": 12.5,
        "args_sha256": SHA,
        **overrides,
    }
    statement = sql.SQL("INSERT INTO telemetry.requests ({}) VALUES ({})").format(
        sql.SQL(", ").join(sql.Identifier(name) for name in values),
        sql.SQL(", ").join(sql.Placeholder(name) for name in values),
    )
    async with await _as(url) as connection:
        await connection.execute(statement, values)


# --- setup -------------------------------------------------------------------------------------


async def test_setup_is_safe_to_repeat(
    telemetry: None, test_database_url: str, writer_url: str, reader_url: str, purger_url: str
) -> None:
    await setup_telemetry(
        test_database_url,
        TelemetryPasswords(_password(writer_url), _password(reader_url), _password(purger_url)),
    )

    async with await _as(test_database_url) as connection:
        cursor = await connection.execute("SELECT version FROM telemetry.schema_migrations")
        versions = [row[0] for row in await cursor.fetchall()]
    assert versions == [migration.version for migration in load_migrations(MIGRATIONS_PACKAGE)]


@pytest.mark.parametrize("empty", ["writer", "reader", "purger"])
async def test_setup_refuses_an_empty_password(test_database_url: str, empty: str) -> None:
    passwords = {"writer": "w", "reader": "r", "purger": "p", **{empty: ""}}

    with pytest.raises(ValueError, match="empty"):
        await setup_telemetry(test_database_url, TelemetryPasswords(**passwords))


# --- the writer: append only -------------------------------------------------------------------


async def test_the_writer_can_append_and_repeat_an_insert_without_reading(
    telemetry: None, writer_url: str
) -> None:
    request_id = uuid4()
    await _insert_request(writer_url, request_id)

    async with await _as(writer_url) as connection:
        # A retried batch repeats its inserts: ON CONFLICT DO NOTHING must need no SELECT.
        await connection.execute(
            "INSERT INTO telemetry.requests (request_id, ts, kind, outcome, duration_ms)"
            " VALUES (%s, %s, 'tool_call', 'forwarded', 1) ON CONFLICT DO NOTHING",
            (request_id, NOW),
        )


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT count(*) FROM telemetry.requests",
        "SELECT count(*) FROM telemetry.dash_requests",
        "UPDATE telemetry.requests SET outcome = 'blocked'",
        "DELETE FROM telemetry.requests",
        "TRUNCATE telemetry.requests",
        "CREATE TABLE telemetry.intruder (id integer)",
        "SELECT count(*) FROM public.clients",
        "SELECT count(*) FROM handbook.documents",
    ],
)
async def test_the_writer_can_do_nothing_but_insert(
    telemetry: None, writer_url: str, statement: str
) -> None:
    async with await _as(writer_url) as connection:
        with pytest.raises((errors.InsufficientPrivilege, errors.UndefinedTable)):
            await connection.execute(statement.encode())


# --- the reader: a few views, read only --------------------------------------------------------


async def test_the_reader_sees_the_dashboard_views_and_nothing_else(
    telemetry: None, writer_url: str, reader_url: str
) -> None:
    request_id = uuid4()
    await _insert_request(writer_url, request_id)

    async with await _as(reader_url) as connection:
        cursor = await connection.execute(
            "SELECT request_id, client_name, tool FROM telemetry.dash_requests"
        )
        assert await cursor.fetchall() == [
            (request_id, "harborline-support-bot", "tickets__get_ticket")
        ]
        for view in ("dash_layer_verdicts", "dash_auth_failures", "dash_pipeline_layers"):
            await connection.execute(
                sql.SQL("SELECT * FROM telemetry.{}").format(sql.Identifier(view))
            )


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT * FROM telemetry.requests",
        "SELECT * FROM telemetry.layer_verdicts",
        "SELECT * FROM telemetry.auth_failures",
        "SELECT * FROM telemetry.spans",
        "SELECT * FROM telemetry.pipeline_configs",
        "SELECT * FROM telemetry.schema_migrations",
        "INSERT INTO telemetry.requests (request_id, ts, kind, outcome, duration_ms)"
        " VALUES (gen_random_uuid(), now(), 'tool_call', 'forwarded', 1)",
        "UPDATE telemetry.dash_requests SET outcome = 'forwarded'",
        "DELETE FROM telemetry.dash_requests",
        "DROP VIEW telemetry.dash_requests",
        "CREATE TABLE telemetry.intruder (id integer)",
        "SELECT * FROM public.clients",
        "SELECT * FROM public.client_tokens",
        "SELECT * FROM crm.accounts",
        "SELECT * FROM handbook.documents",
        "SELECT * FROM ticketing.tickets",
    ],
)
async def test_the_reader_cannot_reach_a_table_write_or_another_schema(
    telemetry: None, reader_url: str, statement: str
) -> None:
    async with await _as(reader_url) as connection:
        with pytest.raises(
            (errors.InsufficientPrivilege, errors.UndefinedTable, errors.ReadOnlySqlTransaction)
        ):
            await connection.execute(statement.encode())


async def test_the_readers_sessions_are_read_only_short_and_few(
    telemetry: None, reader_url: str
) -> None:
    async with await _as(reader_url) as connection:
        cursor = await connection.execute(
            "SELECT current_setting('default_transaction_read_only'),"
            " current_setting('statement_timeout'),"
            " current_setting('idle_in_transaction_session_timeout'),"
            " (SELECT rolconnlimit FROM pg_roles WHERE rolname = current_user)"
        )
        assert await cursor.fetchone() == ("on", "5s", "10s", 5)
        with pytest.raises(errors.QueryCanceled):
            await connection.execute("SELECT pg_sleep(6)")


@pytest.mark.parametrize(
    "hidden",
    ["args_sha256", "pipeline_config_sha256", "trace_id", "lookup_id", "client_id", "attrs"],
)
async def test_the_views_leave_out_hashes_trace_ids_and_lookup_ids(
    telemetry: None, test_database_url: str, hidden: str
) -> None:
    async with await _as(test_database_url) as connection:
        cursor = await connection.execute(
            "SELECT table_name FROM information_schema.columns"
            " WHERE table_schema = 'telemetry' AND table_name LIKE 'dash\\_%%'"
            " AND column_name = %s",
            (hidden,),
        )
        assert await cursor.fetchall() == []


async def test_the_pipeline_view_lists_the_newest_configuration_in_order(
    telemetry: None, writer_url: str, reader_url: str
) -> None:
    older = [{"name": "scope", "mode": "enforce"}]
    newer = [{"name": "scope", "mode": "enforce"}, {"name": "allowlist", "mode": "monitor"}]
    async with await _as(writer_url) as connection:
        for sha, layers, seen in (("b" * 64, older, NOW), ("c" * 64, newer, NOW + timedelta(1))):
            await connection.execute(
                "INSERT INTO telemetry.pipeline_configs (sha256, first_seen, layers)"
                " VALUES (%s, %s, %s::jsonb)",
                (sha, seen, Jsonb(layers)),
            )

    async with await _as(reader_url) as connection:
        cursor = await connection.execute(
            "SELECT position, layer, mode FROM telemetry.dash_pipeline_layers ORDER BY position"
        )
        assert await cursor.fetchall() == [(1, "scope", "enforce"), (2, "allowlist", "monitor")]


async def test_the_pipeline_view_follows_a_rollback_to_an_older_configuration(
    telemetry: None, writer_url: str, reader_url: str
) -> None:
    older = [{"name": "scope", "mode": "enforce"}]
    newer = [{"name": "scope", "mode": "enforce"}, {"name": "allowlist", "mode": "monitor"}]
    async with await _as(writer_url) as connection:
        for sha, layers, seen in (("b" * 64, older, NOW), ("c" * 64, newer, NOW + timedelta(1))):
            await connection.execute(
                "INSERT INTO telemetry.pipeline_configs (sha256, first_seen, layers)"
                " VALUES (%s, %s, %s::jsonb)",
                (sha, seen, Jsonb(layers)),
            )
    # The newest request ran under the older configuration: it is the one in use.
    await _insert_request(writer_url, ts=NOW + timedelta(2), pipeline_config_sha256="b" * 64)

    async with await _as(reader_url) as connection:
        cursor = await connection.execute("SELECT layer FROM telemetry.dash_pipeline_layers")
        assert await cursor.fetchall() == [("scope",)]


# --- the purger: delete by time ----------------------------------------------------------------


async def test_the_purger_can_delete_old_rows_by_time_and_read_nothing_else(
    telemetry: None, writer_url: str, purger_url: str, test_database_url: str
) -> None:
    await _insert_request(writer_url, ts=NOW - timedelta(days=40))
    await _insert_request(writer_url, ts=NOW)

    async with await _as(purger_url) as connection:
        cursor = await connection.execute(
            "DELETE FROM telemetry.requests WHERE ts < %s", (NOW - timedelta(days=30),)
        )
        assert cursor.rowcount == 1
        for statement in (
            "SELECT request_id FROM telemetry.requests",
            "SELECT * FROM telemetry.requests",
            "INSERT INTO telemetry.requests (request_id, ts, kind, outcome, duration_ms)"
            " VALUES (gen_random_uuid(), now(), 'tool_call', 'forwarded', 1)",
            "UPDATE telemetry.requests SET outcome = 'blocked' WHERE ts < now()",
            "SELECT * FROM telemetry.dash_requests",
            "SELECT * FROM public.clients",
        ):
            with pytest.raises(errors.InsufficientPrivilege):
                await connection.execute(statement.encode())

    async with await _as(test_database_url) as connection:
        cursor = await connection.execute("SELECT count(*) FROM telemetry.requests")
        assert await cursor.fetchone() == (1,)


# --- what a row can hold -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "override",
    [
        {"tool": "t" * 65},
        {"client_name": "c" * 101},
        {"args_sha256": "not a hash, but an argument: {'ticket': 'TKT-000001'}"},
        {"namespace": "Tickets With Spaces"},
        {"blocked_by": "scope; DROP TABLE"},
        {"trace_id": "g" * 32},
        {"kind": "something_else"},
        {"outcome": "approved"},
        {"duration_ms": -1},
    ],
    ids=lambda o: next(iter(o)),
)
async def test_a_request_row_cannot_hold_free_text(
    telemetry: None, writer_url: str, override: dict[str, object]
) -> None:
    with pytest.raises(errors.CheckViolation):
        await _insert_request(writer_url, **override)


async def test_a_span_row_cannot_hold_a_payload(telemetry: None, writer_url: str) -> None:
    async with await _as(writer_url) as connection:
        for name, attrs in (
            ("gateway.tool_call", {"argument": "x" * 3000}),
            ("Tool Call With Spaces", {}),
        ):
            with pytest.raises(errors.CheckViolation):
                await connection.execute(
                    "INSERT INTO telemetry.spans"
                    " (trace_id, span_id, ts, name, duration_us, status, attrs)"
                    " VALUES (%s, %s, %s, %s, 1, 'ok', %s::jsonb)",
                    ("a" * 32, "b" * 16, NOW, name, Jsonb(attrs)),
                )


# --- the grants hold, whatever was done to them by hand ---------------------------------------


async def test_a_grant_widened_by_hand_is_narrowed_again(
    telemetry: None, test_database_url: str, reader_url: str
) -> None:
    async with await _as(test_database_url) as connection:
        await connection.execute("GRANT SELECT ON telemetry.requests TO telemetry_reader")
    async with await _as(reader_url) as connection:
        await connection.execute("SELECT count(*) FROM telemetry.requests")  # the widened grant

    async with await _as(test_database_url) as connection:
        await grant_telemetry_access(connection)

    async with await _as(reader_url) as connection:
        with pytest.raises(errors.InsufficientPrivilege):
            await connection.execute("SELECT count(*) FROM telemetry.requests")
