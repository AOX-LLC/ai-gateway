"""The allowlist and the rate limit through the whole gateway: what a client sees, what is kept."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from mcp.shared.exceptions import MCPError

from ai_gateway.auth.tokens import IssuedToken
from ai_gateway.registry.repo import AdminRegistry
from tests.helpers import RunningGateway, connect, run_gateway
from tests.test_audit_e2e import MakeClient

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

ALLOWLIST = """
[[rule]]
name = "say-lowercase-only"
client = "harborline-support-bot"
tool = "echo__say"
argument = "text"
pattern = "[a-z ]{1,20}"
"""
LIMITS = """
[reads]
burst = 3
per = 3600
"""


@pytest.fixture
async def token(
    admin_registry: AdminRegistry, make_client: MakeClient, echo_url: str
) -> IssuedToken:
    await admin_registry.upsert_upstream("echo", echo_url, 2000, 5000)
    await admin_registry.upsert_tool_policy("echo", "say", "read", "test policy")
    _, issued = await make_client("harborline-support-bot", ["echo__say"])
    return issued


@pytest.fixture
def gateway(test_database_url: str, token: IssuedToken, tmp_path: Path) -> Iterator[RunningGateway]:
    with run_gateway(
        test_database_url, tmp_path, allowlist=ALLOWLIST, rate_limits=LIMITS
    ) as running:
        yield running


async def test_a_call_outside_the_allowlist_is_refused_generically_and_recorded_by_layer(
    gateway: RunningGateway, token: IssuedToken
) -> None:
    async with connect(gateway.url, token.plaintext) as client:
        ok = await client.call_tool("echo__say", {"text": "all fine"})
        with pytest.raises(MCPError) as refused:
            await client.call_tool("echo__say", {"text": "SHOUTING!"})

    assert ok.is_error is False
    assert refused.value.message == "Request blocked by gateway policy."
    assert "SHOUTING" not in str(refused.value)
    blocked = [e for e in gateway.events.events if e.payload.get("outcome") == "blocked"][-1]
    assert (blocked.payload["blocked_by"], blocked.payload["deny_code"]) == (
        "allowlist",
        "allowlist_violation",
    )


async def test_the_rate_limit_stops_a_client_after_its_burst_and_says_when_to_retry(
    gateway: RunningGateway, token: IssuedToken
) -> None:
    async with connect(gateway.url, token.plaintext) as client:
        for _ in range(3):
            await client.call_tool("echo__say", {"text": "ok"})
        with pytest.raises(MCPError) as refused:
            await client.call_tool("echo__say", {"text": "ok"})

    assert refused.value.message.startswith("Too many requests. Retry in ")
    blocked = [e for e in gateway.events.events if e.payload.get("outcome") == "blocked"][-1]
    assert (blocked.payload["blocked_by"], blocked.payload["deny_code"]) == (
        "rate_limit",
        "rate_limited",
    )


async def test_a_call_the_allowlist_refuses_takes_no_token(
    gateway: RunningGateway, token: IssuedToken
) -> None:
    async with connect(gateway.url, token.plaintext) as client:
        for _ in range(10):
            with pytest.raises(MCPError):
                await client.call_tool("echo__say", {"text": "NOT ALLOWED"})
        for _ in range(3):  # the whole burst is still there
            await client.call_tool("echo__say", {"text": "fine"})
