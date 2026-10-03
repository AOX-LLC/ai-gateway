"""The whole gateway writing its audit trail: real policy database, real echo upstream."""

import json
import time
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any
from uuid import UUID

import anyio
import httpx2
import psycopg
import pytest
from aox_agent_core.audit import SQLAuditLog
from aox_agent_core.storage import open_database
from mcp.shared.exceptions import MCPError
from pydantic import SecretStr

from ai_gateway.auth.tokens import IssuedToken
from ai_gateway.policy import policy_url
from ai_gateway.registry.repo import AdminRegistry
from tests.helpers import RunningGateway, connect, run_gateway

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

MakeClient = Callable[..., Awaitable[tuple[UUID, IssuedToken]]]

MARKER = "dock-7-account-4111-1111-1111-1111"
"""A tool argument that the echo tool also returns as its result: it must reach no audit record."""
DEAD_POLICY_DATABASE = "postgresql://policy_gateway:x@127.0.0.1:1/ai_gateway_test"  # nothing on :1
POLICY_BLOCK = "Request blocked by gateway policy."


@pytest.fixture
async def tokens(
    admin_registry: AdminRegistry, make_client: MakeClient, echo_url: str
) -> dict[str, IssuedToken]:
    await admin_registry.upsert_upstream("echo", echo_url, 2000, 5000)
    await admin_registry.upsert_tool_policy("echo", "say", "read", "test policy")  # shout: a write
    _, support = await make_client("harborline-support-bot", ["echo__say"])
    _, ops = await make_client("harborline-ops-bot", ["echo__say", "echo__shout"])
    return {"support": support, "ops": ops}


@pytest.fixture
def audited_gateway(
    test_database_url: str,
    tokens: dict[str, IssuedToken],
    policy: None,
    policy_gateway_url: str,
    tmp_path: Path,
) -> Iterator[RunningGateway]:
    with run_gateway(test_database_url, tmp_path, policy_database_url=policy_gateway_url) as gw:
        yield gw


async def _records(url: str) -> list[tuple[str, str | None, dict[str, Any]]]:
    async with await psycopg.AsyncConnection.connect(policy_url(url)) as connection:
        cursor = await connection.execute(
            "SELECT action, subject_id, payload FROM agent_core_audit ORDER BY seq"
        )
        return [(a, s, json.loads(p)) for a, s, p in await cursor.fetchall()]


async def _wait_for(url: str, count: int) -> list[tuple[str, str | None, dict[str, Any]]]:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        records = await _records(url)
        if len(records) >= count:
            return records
        await anyio.sleep(0.05)
    raise AssertionError(f"the audit log did not reach {count} records")


async def _health_when(
    http: httpx2.AsyncClient, gateway: RunningGateway, status: str
) -> httpx2.Response:
    """The gateway's health once its audit status is `status` (the writer notices it later)."""
    url = gateway.url.removesuffix("/mcp") + "/healthz"
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        health = await http.get(url)
        if health.json()["audit"]["status"] == status:
            return health
        await anyio.sleep(0.1)
    raise AssertionError(f"the audit status never became {status}")


async def test_every_call_is_audited_a_write_before_it_runs_and_the_chain_verifies(
    audited_gateway: RunningGateway,
    tokens: dict[str, IssuedToken],
    policy_auditor_url: str,
    policy_gateway_url: str,
) -> None:
    async with connect(audited_gateway.url, tokens["support"].plaintext) as support:
        await support.call_tool("echo__say", {"text": MARKER})
        with pytest.raises(MCPError):
            await support.call_tool("echo__shout", {"text": MARKER})  # outside its scope
    async with connect(audited_gateway.url, tokens["ops"].plaintext) as ops:
        shouted = await ops.call_tool("echo__shout", {"text": MARKER})
    assert shouted.content[0].text == MARKER.upper()  # type: ignore[union-attr]

    records = await _wait_for(policy_gateway_url, 4)

    actions = [(action, subject) for action, subject, _ in records]
    assert actions.count(("gateway.call_started", "echo__shout")) == 1, (
        "one write ran, one was out of scope"
    )
    assert actions.count(("gateway.tool_call", "echo__say")) == 1
    assert actions.count(("gateway.tool_call", "echo__shout")) == 2
    started = actions.index(("gateway.call_started", "echo__shout"))
    ran = next(
        i
        for i, (a, _, p) in enumerate(records)
        if a == "gateway.tool_call" and p["outcome"] == "forwarded" and p["effect"] == "write"
    )
    assert started < ran, "the record that the write was about to run comes before its result"
    blocked = next(
        p for a, _, p in records if a == "gateway.tool_call" and p["outcome"] == "blocked"
    )
    assert (blocked["blocked_by"], blocked["deny_code"]) == ("scope", "tool_unavailable")
    # Hashes and codes, never the argument, never the result that echoes it.
    assert MARKER not in json.dumps(records)
    assert MARKER.upper() not in json.dumps(records)
    assert "4111" not in json.dumps(records)

    auditor = SQLAuditLog(open_database(SecretStr(policy_url(policy_auditor_url))))
    assert (await auditor.verify()).seq == len(await _records(policy_gateway_url))


async def test_a_dead_audit_log_refuses_a_write_and_never_a_read(
    test_database_url: str, tokens: dict[str, IssuedToken], tmp_path: Path
) -> None:
    with run_gateway(
        test_database_url, tmp_path, policy_database_url=DEAD_POLICY_DATABASE
    ) as gateway:
        async with connect(gateway.url, tokens["ops"].plaintext) as client:
            said = await client.call_tool("echo__say", {"text": "a read"})
            with pytest.raises(MCPError) as refused:
                await client.call_tool("echo__shout", {"text": "a write"})
        async with httpx2.AsyncClient() as http:
            health = await _health_when(http, gateway, "degraded")

    assert said.content[0].text == "a read"  # type: ignore[union-attr]
    assert refused.value.message == POLICY_BLOCK
    assert health.status_code == 200, "a failing audit log must not make the gateway unhealthy"
    assert health.json()["audit"]["status"] == "degraded"
    assert health.json()["audit"]["queue_depth"] >= 1, "the read's record is waiting, not lost"
    blocked = [e for e in gateway.events.events if e.action == "gateway.tool_call"][-1].payload
    assert (blocked["blocked_by"], blocked["deny_code"]) == ("audit", "audit_unavailable")


async def test_without_an_audit_database_writes_are_refused_unless_the_configuration_allows(
    test_database_url: str, tokens: dict[str, IssuedToken], tmp_path: Path
) -> None:
    with run_gateway(test_database_url, tmp_path, unaudited_writes=False) as gateway:
        async with connect(gateway.url, tokens["ops"].plaintext) as client:
            await client.call_tool("echo__say", {"text": "a read"})
            with pytest.raises(MCPError) as refused:
                await client.call_tool("echo__shout", {"text": "a write"})
        async with httpx2.AsyncClient() as http:
            health = await http.get(gateway.url.removesuffix("/mcp") + "/healthz")

    assert refused.value.message == POLICY_BLOCK
    assert health.json()["audit"] == {
        "status": "disabled",
        "queue_depth": 0,
        "dropped_total": 0,
        "rejected_total": 0,
        "written_total": 0,
    }


async def test_a_dead_telemetry_database_does_not_stop_the_audit_trail(
    test_database_url: str,
    tokens: dict[str, IssuedToken],
    policy: None,
    policy_gateway_url: str,
    tmp_path: Path,
) -> None:
    dead_telemetry = "postgresql://telemetry_writer:x@127.0.0.1:1/ai_gateway_test"
    with run_gateway(
        test_database_url,
        tmp_path,
        telemetry_database_url=dead_telemetry,
        policy_database_url=policy_gateway_url,
    ) as gateway:
        async with connect(gateway.url, tokens["ops"].plaintext) as client:
            await client.call_tool("echo__shout", {"text": "a write"})

    records = await _wait_for(policy_gateway_url, 2)
    assert [a for a, _, _ in records] == ["gateway.call_started", "gateway.tool_call"]


async def test_health_reports_the_audit_log(
    audited_gateway: RunningGateway, tokens: dict[str, IssuedToken], policy_gateway_url: str
) -> None:
    async with connect(audited_gateway.url, tokens["ops"].plaintext) as client:
        await client.call_tool("echo__say", {"text": "x"})
    await _wait_for(policy_gateway_url, 1)

    async with httpx2.AsyncClient() as http:
        health = await http.get(audited_gateway.url.removesuffix("/mcp") + "/healthz")

    audit = health.json()["audit"]
    assert audit["status"] == "ok"
    assert audit["written_total"] >= 1
    assert audit["dropped_total"] == 0
