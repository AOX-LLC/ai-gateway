"""The whole gateway over HTTP: real database, real echo upstream, the SDK's own client."""

import logging
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from importlib.metadata import version
from pathlib import Path
from typing import Any
from uuid import UUID

import anyio
import httpx2
import pytest
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp.types import INVALID_PARAMS, TextContent

from ai_gateway.auth.tokens import IssuedToken
from ai_gateway.registry import MIGRATIONS_PACKAGE
from ai_gateway.registry.repo import AdminRegistry
from echo_server.server import cancellations
from mcp_common.migrate import load_migrations
from tests.helpers import RunningGateway, run_gateway
from tests.test_upstreams import eventually

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

MakeClient = Callable[..., Awaitable[tuple[UUID, IssuedToken]]]

_INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-11-25",
        "capabilities": {},
        "clientInfo": {"name": "raw-test", "version": "0"},
    },
}
_MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


@pytest.fixture
async def tokens(
    admin_registry: AdminRegistry, make_client: MakeClient, echo_url: str
) -> dict[str, IssuedToken]:
    await admin_registry.upsert_upstream("echo", echo_url, 2000, 5000)
    await admin_registry.upsert_tool_policy("echo", "say", "read", "test policy")
    _, support = await make_client("harborline-support-bot", ["echo__say"])
    _, ops = await make_client("harborline-ops-bot", ["echo__say", "echo__shout"])
    return {"support": support, "ops": ops}


@pytest.fixture
def gateway(
    test_database_url: str, tokens: dict[str, IssuedToken], tmp_path: Path
) -> Iterator[RunningGateway]:
    with run_gateway(test_database_url, tmp_path) as running:
        yield running


def _tampered(token: str) -> str:
    """The same token with its last character changed: well-formed, wrong secret."""
    return token[:-1] + ("A" if token[-1] != "A" else "B")


@asynccontextmanager
async def connect(url: str, token: str) -> AsyncGenerator[Client]:
    headers = {"Authorization": f"Bearer {token}"}
    async with (
        httpx2.AsyncClient(headers=headers) as http_client,
        Client(streamable_http_client(url, http_client=http_client), mode="legacy") as client,
    ):
        yield client


async def test_a_client_sees_and_calls_only_its_scoped_tools(
    gateway: RunningGateway, tokens: dict[str, IssuedToken]
) -> None:
    async with connect(gateway.url, tokens["support"].plaintext) as client:
        listed = await client.list_tools()
        said = await client.call_tool("echo__say", {"text": "dock 7 is clear"})
        with pytest.raises(MCPError) as refused:
            await client.call_tool("echo__shout", {"text": "dock 7 is clear"})
        with pytest.raises(MCPError) as unknown:
            await client.call_tool("echo__nothing", {})

    assert [tool.name for tool in listed.tools] == ["echo__say"]
    assert said.content == [TextContent(type="text", text="dock 7 is clear")]
    assert refused.value.code == INVALID_PARAMS
    assert refused.value.message == "Tool 'echo__shout' is not available to this client."
    assert unknown.value.message == "Tool 'echo__nothing' is not available to this client."


async def test_a_wider_scope_sees_more_tools(
    gateway: RunningGateway, tokens: dict[str, IssuedToken]
) -> None:
    async with connect(gateway.url, tokens["ops"].plaintext) as client:
        listed = await client.list_tools()
        shouted = await client.call_tool("echo__shout", {"text": "all hands"})

    assert sorted(tool.name for tool in listed.tools) == ["echo__say", "echo__shout"]
    assert shouted.content == [TextContent(type="text", text="ALL HANDS")]


async def test_requests_without_a_valid_token_are_refused(
    gateway: RunningGateway, tokens: dict[str, IssuedToken], admin_registry: AdminRegistry
) -> None:
    await admin_registry.revoke_token(tokens["ops"].lookup_id)
    attempts: list[dict[str, str]] = [
        {},
        {"Authorization": "Bearer not-a-token"},
        {"Authorization": f"Bearer {_tampered(tokens['support'].plaintext)}"},
        {"Authorization": f"Bearer {tokens['ops'].plaintext}"},
    ]

    async with httpx2.AsyncClient() as http_client:
        responses = [
            await http_client.post(
                gateway.url, json=_INITIALIZE, headers={**_MCP_HEADERS, **headers}
            )
            for headers in attempts
        ]

    assert [response.status_code for response in responses] == [401, 401, 401, 401]
    failures = [e.payload["reason"] for e in gateway.events.events if e.action.endswith("failure")]
    assert failures == ["missing", "malformed", "wrong_secret", "revoked"]


async def test_a_session_cannot_be_used_with_another_clients_token(
    gateway: RunningGateway, tokens: dict[str, IssuedToken]
) -> None:
    async with httpx2.AsyncClient() as http_client:
        opened = await http_client.post(
            gateway.url,
            json=_INITIALIZE,
            headers={**_MCP_HEADERS, "Authorization": f"Bearer {tokens['ops'].plaintext}"},
        )
        session_id = opened.headers["mcp-session-id"]
        hijack = await http_client.post(
            gateway.url,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            headers={
                **_MCP_HEADERS,
                "Authorization": f"Bearer {tokens['support'].plaintext}",
                "Mcp-Session-Id": session_id,
                "MCP-Protocol-Version": "2025-11-25",
            },
        )

    assert opened.status_code == 200
    assert hijack.status_code == 404


async def test_the_stateless_2026_revision_is_refused_clearly(
    gateway: RunningGateway, tokens: dict[str, IssuedToken]
) -> None:
    async with httpx2.AsyncClient() as http_client:
        response = await http_client.post(
            gateway.url,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={
                **_MCP_HEADERS,
                "Authorization": f"Bearer {tokens['support'].plaintext}",
                "MCP-Protocol-Version": "2026-07-28",
            },
        )

    assert response.status_code == 400
    assert "2025-11-25" in response.json()["error"]["message"]


async def test_every_decision_is_recorded_with_the_negotiated_version(
    gateway: RunningGateway, tokens: dict[str, IssuedToken]
) -> None:
    async with connect(gateway.url, tokens["support"].plaintext) as client:
        await client.list_tools()
        with pytest.raises(MCPError):
            await client.call_tool("echo__shout", {"text": "x"})

    calls = [e for e in gateway.events.events if e.action == "gateway.tool_call"]
    listings = [e for e in gateway.events.events if e.action == "gateway.tools_list"]
    assert listings[-1].payload["tools_returned"] == 1
    assert listings[-1].payload["protocol_version"] == "2025-11-25"
    blocked: dict[str, Any] = calls[-1].payload
    assert (blocked["outcome"], blocked["blocked_by"]) == ("blocked", "scope")


async def test_decision_records_carry_the_tool_effect_and_where_it_came_from(
    gateway: RunningGateway, tokens: dict[str, IssuedToken]
) -> None:
    async with connect(gateway.url, tokens["ops"].plaintext) as client:
        await client.call_tool("echo__say", {"text": "x"})
        await client.call_tool("echo__shout", {"text": "x"})

    calls = [e.payload for e in gateway.events.events if e.action == "gateway.tool_call"]
    assert [(c["effect"], c["effect_source"]) for c in calls] == [
        ("read", "policy"),
        ("write", "default"),
    ]


async def test_raw_tokens_never_appear_in_logs(
    gateway: RunningGateway,
    tokens: dict[str, IssuedToken],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)

    async with connect(gateway.url, tokens["support"].plaintext) as client:
        await client.list_tools()
        await client.call_tool("echo__say", {"text": "x"})
    async with httpx2.AsyncClient() as http_client:
        await http_client.post(
            gateway.url,
            json=_INITIALIZE,
            headers={
                **_MCP_HEADERS,
                "Authorization": f"Bearer {_tampered(tokens['ops'].plaintext)}",
            },
        )

    for token in tokens.values():
        assert token.plaintext.split("_", 2)[2] not in caplog.text


async def test_healthz_reports_the_running_build(gateway: RunningGateway) -> None:
    async with httpx2.AsyncClient() as http_client:
        response = await http_client.get(gateway.url.removesuffix("/mcp") + "/healthz")

    health = response.json()
    newest_migration = max(migration.version for migration in load_migrations(MIGRATIONS_PACKAGE))
    assert response.status_code == 200
    assert health["status"] == "ok"
    assert (health["commit"], health["branch"]) == (None, None)
    assert health["commit_source"] == "process_start"
    assert health["version"] == version("ai-gateway")
    assert health["schema_version"] == f"{newest_migration:04d}"
    assert health["uptime_s"] >= 0


async def test_a_client_that_cancels_mid_call_cancels_the_upstream_call(
    gateway: RunningGateway, make_client: MakeClient
) -> None:
    """Cancellation travels client -> gateway -> upstream over real HTTP at both hops.

    The client is raw HTTP, not the SDK's: the SDK's streamable-HTTP client does not guard
    writing a late JSON response to its session when the session has closed, so a test that
    leaves its cancelled call unanswered and exits races the SDK's own teardown (see the SDK
    note in the pull request that rewrote this test). Here the test holds the cancelled POST
    itself and waits for it to finish, so nothing is left in flight.
    """
    _, token = await make_client("cancel-bot", ["echo__wait"])
    cancelled_before = cancellations.count
    started_before = cancellations.started
    headers = {**_MCP_HEADERS, "Authorization": f"Bearer {token.plaintext}"}
    answers: list[httpx2.Response] = []

    async with httpx2.AsyncClient(timeout=10) as http:
        opened = await http.post(gateway.url, json=_INITIALIZE, headers=headers)
        session = {
            **headers,
            "Mcp-Session-Id": opened.headers["mcp-session-id"],
            "MCP-Protocol-Version": "2025-11-25",
        }
        await http.post(
            gateway.url,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers=session,
        )

        async def call_and_wait() -> None:
            answers.append(
                await http.post(
                    gateway.url,
                    json={
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {"name": "echo__wait", "arguments": {"seconds": 30}},
                    },
                    headers=session,
                )
            )

        with anyio.fail_after(10):
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(call_and_wait)
                await eventually(lambda: cancellations.started > started_before, timeout_s=5.0)
                cancel = await http.post(
                    gateway.url,
                    json={
                        "jsonrpc": "2.0",
                        "method": "notifications/cancelled",
                        "params": {"requestId": 2, "reason": "the client gave up"},
                    },
                    headers=session,
                )
                assert cancel.status_code == 202
                # The upstream call timeout is 5 s, so a prompt cancel can only be propagation.
                await eventually(lambda: cancellations.count > cancelled_before, timeout_s=2.0)
            # Leaving the task group means the cancelled POST was answered: nothing is in flight.

    assert len(answers) == 1
    assert cancellations.started == started_before + 1
    assert cancellations.count == cancelled_before + 1
