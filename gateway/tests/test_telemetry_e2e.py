"""The whole gateway storing telemetry: real database, real echo upstream, real HTTP."""

import time
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from uuid import UUID

import anyio
import httpx2
import psycopg
import pytest
from mcp.shared.exceptions import MCPError

from ai_gateway.auth.tokens import IssuedToken
from ai_gateway.registry.repo import AdminRegistry
from ai_gateway.telemetry.setup import TELEMETRY_TABLES
from tests.helpers import RunningGateway, connect, run_gateway

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

MakeClient = Callable[..., Awaitable[tuple[UUID, IssuedToken]]]

MARKER = "dock-7-account-4111-1111-1111-1111"
"""A tool argument, which the echo tool also returns as its result: it must reach no table."""


@pytest.fixture
async def tokens(
    admin_registry: AdminRegistry, make_client: MakeClient, echo_url: str
) -> dict[str, IssuedToken]:
    await admin_registry.upsert_upstream("echo", echo_url, 2000, 5000)
    await admin_registry.upsert_tool_policy("echo", "say", "read", "test policy")
    _, support = await make_client("harborline-support-bot", ["echo__say"])
    return {"support": support}


@pytest.fixture
def gateway(
    test_database_url: str,
    tokens: dict[str, IssuedToken],
    telemetry: None,
    writer_url: str,
    tmp_path: Path,
) -> Iterator[RunningGateway]:
    with run_gateway(test_database_url, tmp_path, writer_url) as running:
        yield running


async def _scalar(url: str, query: str) -> object:
    async with await psycopg.AsyncConnection.connect(url) as connection:
        cursor = await connection.execute(query.encode())
        row = await cursor.fetchone()
    assert row is not None
    return row[0]


async def _rows(url: str, query: str) -> list[tuple[object, ...]]:
    async with await psycopg.AsyncConnection.connect(url) as connection:
        cursor = await connection.execute(query.encode())
        return await cursor.fetchall()


async def _stored(url: str, table: str, at_least: int) -> None:
    """Wait for the background writer to have stored at least `at_least` rows of a table."""
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        count = await _scalar(url, f"SELECT count(*) FROM telemetry.{table}")  # noqa: S608
        if isinstance(count, int) and count >= at_least:
            return
        await anyio.sleep(0.05)
    raise AssertionError(f"telemetry.{table} did not reach {at_least} rows")


async def test_requests_layers_spans_and_auth_failures_are_stored_without_a_trace_of_the_arguments(
    gateway: RunningGateway,
    tokens: dict[str, IssuedToken],
    test_database_url: str,
    reader_url: str,
) -> None:
    async with connect(gateway.url, tokens["support"].plaintext) as client:
        await client.list_tools()
        said = await client.call_tool("echo__say", {"text": MARKER})
        with pytest.raises(MCPError):
            await client.call_tool("echo__shout", {"text": MARKER})  # out of scope
        with pytest.raises(MCPError):
            await client.call_tool("echo__nothing", {"text": MARKER})  # unknown
    async with httpx2.AsyncClient() as http:
        refused = await http.post(
            gateway.url, headers={"Authorization": "Bearer aig_abcd2345_wrong"}, json={}
        )
    assert said.content[0].text == MARKER  # type: ignore[union-attr]
    assert refused.status_code == 401

    await _stored(test_database_url, "requests", 4)
    await _stored(test_database_url, "auth_failures", 1)

    requests = await _rows(
        test_database_url,
        "SELECT kind, tool, namespace, outcome, blocked_by, client_name FROM telemetry.requests"
        " ORDER BY ts",
    )
    assert requests == [
        ("tools_list", None, None, "listed", None, "harborline-support-bot"),
        ("tool_call", "echo__say", "echo", "forwarded", None, "harborline-support-bot"),
        ("tool_call", "echo__shout", "echo", "blocked", "scope", "harborline-support-bot"),
        ("tool_call", "echo__nothing", "echo", "blocked", "catalog", "harborline-support-bot"),
    ]
    assert await _rows(test_database_url, "SELECT reason FROM telemetry.auth_failures") == [
        ("malformed",)
    ]
    blocked = await _rows(
        test_database_url,
        "SELECT layer, hook, verdict, code FROM telemetry.layer_verdicts v"
        " JOIN telemetry.requests r USING (request_id) WHERE r.tool = 'echo__shout'",
    )
    assert blocked == [("scope", "before_call", "deny", "tool_unavailable")]

    # Every request that has a trace id has its spans, in a trace of its own.
    await _stored(test_database_url, "spans", 4)  # at least a root span per request
    trace_ids = await _rows(
        test_database_url,
        "SELECT DISTINCT trace_id FROM telemetry.requests WHERE trace_id IS NOT NULL",
    )
    stored_traces = {
        row[0] for row in await _rows(test_database_url, "SELECT trace_id FROM telemetry.spans")
    }
    assert {row[0] for row in trace_ids} <= stored_traces

    # Nothing of the argument, or of the result that echoes it, is in any column of any table.
    for table in TELEMETRY_TABLES:
        dump = await _rows(test_database_url, f"SELECT t::text FROM telemetry.{table} t")  # noqa: S608
        assert MARKER not in repr(dump), table
        assert "4111" not in repr(dump), table

    # The dashboard's role sees the same through its views.
    async with await psycopg.AsyncConnection.connect(reader_url) as connection:
        cursor = await connection.execute("SELECT count(*) FROM telemetry.dash_requests")
        assert await cursor.fetchone() == (4,)
        cursor = await connection.execute("SELECT layer FROM telemetry.dash_pipeline_layers")
        assert await cursor.fetchall() == [("scope",)]


async def test_health_reports_the_telemetry_writer(
    gateway: RunningGateway, tokens: dict[str, IssuedToken], test_database_url: str
) -> None:
    async with connect(gateway.url, tokens["support"].plaintext) as client:
        await client.call_tool("echo__say", {"text": "hello"})
    await _stored(test_database_url, "requests", 1)

    async with httpx2.AsyncClient() as http:
        health = await http.get(gateway.url.removesuffix("/mcp") + "/healthz")

    body = health.json()
    assert health.status_code == 200
    assert body["telemetry"]["status"] == "ok"
    assert body["telemetry"]["written_total"] >= 2  # the pipeline config and the request
    assert body["telemetry"]["dropped_total"] == 0


async def test_a_dead_telemetry_database_changes_nothing_a_client_can_see(
    test_database_url: str, tokens: dict[str, IssuedToken], tmp_path: Path
) -> None:
    dead = "postgresql://telemetry_writer:x@127.0.0.1:1/ai_gateway_test"  # nothing listens on :1

    with run_gateway(test_database_url, tmp_path, dead) as gateway:
        async with connect(gateway.url, tokens["support"].plaintext) as client:
            listed = await client.list_tools()
            said = await client.call_tool("echo__say", {"text": "still works"})
            with pytest.raises(MCPError) as refused:
                await client.call_tool("echo__shout", {"text": "x"})
        async with httpx2.AsyncClient() as http:
            await eventually_degraded(http, gateway.url.removesuffix("/mcp") + "/healthz")
            health = await http.get(gateway.url.removesuffix("/mcp") + "/healthz")

    assert [tool.name for tool in listed.tools] == ["echo__say"]
    assert said.content[0].text == "still works"  # type: ignore[union-attr]
    assert refused.value.message == "Tool 'echo__shout' is not available to this client."
    assert health.status_code == 200, "a telemetry outage must not make the gateway unhealthy"
    assert health.json()["telemetry"]["status"] == "degraded"
    assert health.json()["status"] == "ok"


async def eventually_degraded(http: httpx2.AsyncClient, url: str) -> None:
    async def check() -> bool:
        return bool((await http.get(url)).json()["telemetry"]["status"] == "degraded")

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if await check():
            return
        await anyio.sleep(0.05)
    raise AssertionError("the gateway never reported degraded telemetry")
