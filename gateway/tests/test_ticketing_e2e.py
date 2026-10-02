"""The ticketing server behind the gateway, over real HTTP, with the credential and
client attribution as they run in the stack."""

from collections.abc import AsyncGenerator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import httpx2
import psycopg
import pytest
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp.types import INVALID_PARAMS
from pydantic import SecretStr

from ai_gateway.auth.tokens import IssuedToken
from ai_gateway.registry.repo import AdminRegistry
from ai_gateway.registry.tool_policies import load_tool_policies
from mcp_common.attribution import CLIENT_META_KEY
from mcp_common.schema_contract import check_input_schema
from tests.helpers import RunningGateway, run_gateway, serve_in_thread
from ticketing_server.seed import Dataset
from ticketing_server.server import build_app
from ticketing_server.settings import TicketingSettings

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

MakeClient = Callable[..., Awaitable[tuple[UUID, IssuedToken]]]

SERVICE_TOKEN = "ticketing-service-token-for-tests-only"  # noqa: S105 - a fixture value
TOKEN_ENV = "TICKETING_SERVICE_TOKEN"  # noqa: S105 - the variable's name, not a secret
REPO_ROOT = Path(__file__).resolve().parents[2]
SUPPORT_SCOPES = [
    "tickets__get_ticket",
    "tickets__list_tickets",
    "tickets__create_ticket",
    "tickets__add_comment",
]
OPS_SCOPES = [*SUPPORT_SCOPES, "tickets__change_status", "tickets__assign"]


@pytest.fixture
def ticketing_url(ticketing_app_url: str, ticketing_data: Dataset) -> Iterator[str]:
    settings = TicketingSettings(
        database_url=SecretStr(ticketing_app_url),
        service_token=SecretStr(SERVICE_TOKEN),
        allowed_hosts=["127.0.0.1:*"],
    )
    with serve_in_thread(build_app(settings)) as base_url:
        yield f"{base_url}/mcp"


@pytest.fixture
async def tokens(
    admin_registry: AdminRegistry,
    make_client: MakeClient,
    ticketing_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, IssuedToken]:
    monkeypatch.setenv(TOKEN_ENV, SERVICE_TOKEN)
    await admin_registry.upsert_upstream("tickets", ticketing_url, 2000, 5000, TOKEN_ENV)
    for policy in load_tool_policies(REPO_ROOT / "config" / "tool_policies.toml"):
        await admin_registry.upsert_tool_policy(
            policy.namespace, policy.tool, policy.effect, policy.notes
        )
    _, support = await make_client("harborline-support-bot", SUPPORT_SCOPES)
    _, ops = await make_client("harborline-ops-bot", OPS_SCOPES)
    return {"support": support, "ops": ops}


@pytest.fixture
def gateway(
    test_database_url: str, tokens: dict[str, IssuedToken], tmp_path: Path
) -> Iterator[RunningGateway]:
    with run_gateway(test_database_url, tmp_path) as running:
        yield running


@asynccontextmanager
async def connect(url: str, token: str) -> AsyncGenerator[Client]:
    headers = {"Authorization": f"Bearer {token}"}
    async with (
        httpx2.AsyncClient(headers=headers) as http_client,
        Client(streamable_http_client(url, http_client=http_client), mode="legacy") as client,
    ):
        yield client


async def _requested_by(url: str, ticket_id: str) -> str:
    async with await psycopg.AsyncConnection.connect(url) as connection:
        await connection.execute("SET search_path TO ticketing")
        cursor = await connection.execute(
            "SELECT requested_by FROM tickets WHERE id = %s", (ticket_id,)
        )
        row = await cursor.fetchone()
    assert row is not None
    return str(row[0])


_NEW_TICKET = {
    "subject": "Dock gate stuck",
    "description": "The gate will not latch.",
    "priority": "normal",
    "account_id": "ACC-00003",
}


async def test_the_gateway_not_the_client_decides_who_is_recorded_as_asking(
    gateway: RunningGateway, tokens: dict[str, IssuedToken], ticketing_app_url: str
) -> None:
    spoof = cast(Any, {CLIENT_META_KEY: "harborline-ops-bot"})

    async with connect(gateway.url, tokens["support"].plaintext) as client:
        spoofed = await client.call_tool("tickets__create_ticket", _NEW_TICKET, meta=spoof)
        honest = await client.call_tool("tickets__create_ticket", _NEW_TICKET)

    for result in (spoofed, honest):
        assert result.structured_content is not None
        ticket_id = result.structured_content["ticket_id"]
        assert await _requested_by(ticketing_app_url, ticket_id) == "harborline-support-bot"


async def test_a_client_cannot_reach_a_tool_outside_its_scope_even_for_a_write(
    gateway: RunningGateway, tokens: dict[str, IssuedToken]
) -> None:
    async with connect(gateway.url, tokens["support"].plaintext) as client:
        with pytest.raises(MCPError) as refused:
            await client.call_tool(
                "tickets__change_status", {"ticket_id": "TKT-000001", "status": "closed"}
            )

    assert refused.value.code == INVALID_PARAMS


async def test_the_gateways_catalog_of_the_tickets_tools_meets_the_schema_contract(
    gateway: RunningGateway, tokens: dict[str, IssuedToken]
) -> None:
    async with connect(gateway.url, tokens["ops"].plaintext) as client:
        tools = (await client.list_tools()).tools

    assert sorted(tool.name for tool in tools) == sorted(OPS_SCOPES)
    for tool in tools:
        assert check_input_schema(tool.input_schema) == [], tool.name
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is (
            tool.name in {"tickets__get_ticket", "tickets__list_tickets"}
        )


async def test_the_ops_client_runs_every_tool_through_the_gateway(
    gateway: RunningGateway, tokens: dict[str, IssuedToken], ticketing_data: Dataset
) -> None:
    handle = ticketing_data.staff[0].handle
    async with connect(gateway.url, tokens["ops"].plaintext) as client:
        listed = await client.call_tool("tickets__list_tickets", {"limit": 3})
        got = await client.call_tool("tickets__get_ticket", {"ticket_id": "TKT-000001"})
        commented = await client.call_tool(
            "tickets__add_comment", {"ticket_id": "TKT-000001", "body": "On its way."}
        )
        changed = await client.call_tool(
            "tickets__change_status", {"ticket_id": "TKT-000002", "status": "pending"}
        )
        assigned = await client.call_tool(
            "tickets__assign", {"ticket_id": "TKT-000003", "assignee": handle}
        )

    assert not any(r.is_error for r in (listed, got, commented, changed, assigned))
    assert changed.structured_content == {"ticket_id": "TKT-000002", "status": "pending"}


async def test_the_ticketing_server_refuses_calls_without_its_credential(
    ticketing_url: str,
) -> None:
    headers = {"Accept": "application/json, text/event-stream"}
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "raw-test", "version": "0"},
        },
    }
    async with httpx2.AsyncClient() as http:
        missing = await http.post(ticketing_url, json=body, headers=headers)
        wrong = await http.post(
            ticketing_url, json=body, headers={**headers, "Authorization": "Bearer nope"}
        )
        right = await http.post(
            ticketing_url,
            json=body,
            headers={**headers, "Authorization": f"Bearer {SERVICE_TOKEN}"},
        )
        health = await http.get(ticketing_url.removesuffix("/mcp") + "/healthz")

    assert (missing.status_code, wrong.status_code, right.status_code) == (401, 401, 200)
    assert health.json() == {"status": "ok"}
