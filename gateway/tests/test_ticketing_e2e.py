"""The ticketing server behind the gateway, over real HTTP, with the credential and
client attribution as they run in the stack."""

import json
from collections.abc import Awaitable, Callable, Iterator
from importlib.metadata import version
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import httpx2
import psycopg
import pytest
from mcp.shared.exceptions import MCPError
from mcp.types import INVALID_PARAMS
from pydantic import SecretStr
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ai_gateway.auth.tokens import IssuedToken
from ai_gateway.registry.repo import AdminRegistry
from ai_gateway.registry.tool_policies import load_tool_policies
from mcp_common.attribution import CLIENT_META_KEY
from mcp_common.migrate import load_migrations
from mcp_common.schema_contract import check_input_schema
from tests.helpers import RunningGateway, connect, run_gateway, serve_in_thread
from ticketing_server import MIGRATIONS_PACKAGE as TICKETING_MIGRATIONS_PACKAGE
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


class _RecordingApp:
    """Wraps an ASGI app and keeps the JSON body of every POST it receives, so a test can
    see exactly what reached the server."""

    def __init__(self, app: ASGIApp, bodies: list[dict[str, Any]]) -> None:
        self._app = app
        self._bodies = bodies

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "POST":
            await self._app(scope, receive, send)
            return
        chunks: list[bytes] = []

        async def recording_receive() -> Message:
            message = await receive()
            if message["type"] == "http.request":
                chunks.append(message.get("body", b""))
            return message

        try:
            await self._app(scope, recording_receive, send)
        finally:
            body = json.loads(b"".join(chunks) or b"null")
            if isinstance(body, dict):
                self._bodies.append(body)


@pytest.fixture
def upstream_requests() -> list[dict[str, Any]]:
    """Every JSON-RPC request body the ticketing server received."""
    return []


@pytest.fixture
def ticketing_url(
    ticketing_app_url: str, ticketing_data: Dataset, upstream_requests: list[dict[str, Any]]
) -> Iterator[str]:
    settings = TicketingSettings(
        database_url=SecretStr(ticketing_app_url),
        service_token=SecretStr(SERVICE_TOKEN),
        allowed_hosts=["127.0.0.1:*"],
    )
    with serve_in_thread(_RecordingApp(build_app(settings), upstream_requests)) as base_url:
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


async def test_nothing_of_the_clients_own_meta_reaches_the_ticketing_server(
    gateway: RunningGateway,
    tokens: dict[str, IssuedToken],
    upstream_requests: list[dict[str, Any]],
) -> None:
    hostile = cast(
        Any,
        {
            CLIENT_META_KEY: "harborline-ops-bot",
            "progressToken": "client-chosen-token",
            "io.example/custom": {"anything": "else"},
            "io.example/progress": 1,
            "x": None,
        },
    )

    async with connect(gateway.url, tokens["support"].plaintext) as client:
        await client.call_tool("tickets__list_tickets", {"limit": 1}, meta=hostile)

    calls = [r for r in upstream_requests if r.get("method") == "tools/call"]
    assert len(calls) == 1
    assert calls[0]["params"]["_meta"] == {CLIENT_META_KEY: "harborline-support-bot"}
    assert "client-chosen-token" not in json.dumps(calls)


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
    assert health.status_code == 200


def _health_settings(ticketing_app_url: str) -> TicketingSettings:
    return TicketingSettings(
        database_url=SecretStr(ticketing_app_url),
        service_token=SecretStr(SERVICE_TOKEN),
        allowed_hosts=["127.0.0.1:*"],
        git_commit="0123456789abcdef0123456789abcdef01234567",
        git_branch="phase-2-mcp-servers",
    )


async def test_healthz_reports_the_build_and_the_schema_in_the_house_format(
    ticketing_app_url: str, ticketing_data: Dataset
) -> None:
    with serve_in_thread(build_app(_health_settings(ticketing_app_url))) as base_url:
        async with httpx2.AsyncClient() as http:
            response = await http.get(f"{base_url}/healthz")  # no credential needed

    health = response.json()
    newest = max(m.version for m in load_migrations(TICKETING_MIGRATIONS_PACKAGE))
    assert response.status_code == 200
    assert health["status"] == "ok"
    assert health["commit"] == "0123456789abcdef0123456789abcdef01234567"
    assert health["branch"] == "phase-2-mcp-servers"
    assert health["commit_source"] == "process_start"
    assert health["version"] == version("ticketing-server")
    assert health["schema_version"] == f"{newest:04d}"
    assert health["uptime_s"] >= 0
    assert set(health) == {
        "status",
        "commit",
        "commit_source",
        "branch",
        "version",
        "schema_version",
        "uptime_s",
    }


async def test_healthz_without_a_build_reports_null_commit_and_branch(
    ticketing_app_url: str, ticketing_data: Dataset
) -> None:
    settings = _health_settings(ticketing_app_url).model_copy(
        update={"git_commit": None, "git_branch": None}
    )
    with serve_in_thread(build_app(settings)) as base_url:
        async with httpx2.AsyncClient() as http:
            health = (await http.get(f"{base_url}/healthz")).json()

    assert (health["commit"], health["branch"]) == (None, None)


async def test_healthz_is_unavailable_when_the_database_cannot_be_read(
    ticketing_app_url: str, test_database_url: str, ticketing_data: Dataset
) -> None:
    owner = await psycopg.AsyncConnection.connect(test_database_url, autocommit=True)
    try:
        with serve_in_thread(build_app(_health_settings(ticketing_app_url))) as base_url:
            await owner.execute("REVOKE SELECT ON ticketing.schema_migrations FROM ticketing_app")
            async with httpx2.AsyncClient() as http:
                response = await http.get(f"{base_url}/healthz")
    finally:
        await owner.execute("GRANT SELECT ON ticketing.schema_migrations TO ticketing_app")
        await owner.close()

    assert response.status_code == 503
    assert response.json()["status"] == "unavailable"
    assert response.json()["schema_version"] is None
