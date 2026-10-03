"""A write through the whole gateway waits for a person, and the guarantees around that."""

import argparse
import json
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID

import anyio
import psycopg
import pytest
from aox_agent_core.approvals import Decision, Principal, PrincipalKind
from aox_agent_core.errors import ConfigError, NotAuthorizedToResolveError
from mcp.shared.exceptions import MCPError
from psycopg import errors
from pydantic import SecretStr

from ai_gateway.admin.cli import _approver_add
from ai_gateway.app import create_app
from ai_gateway.approver.cli import Approvals
from ai_gateway.auth.tokens import IssuedToken
from ai_gateway.policy import policy_url
from ai_gateway.registry.repo import AdminRegistry
from ai_gateway.settings import GatewaySettings
from tests.helpers import RunningGateway, connect, run_gateway
from tests.test_approval_gate import HUMAN, MARKER, _approver, _call, _context, _gate
from tests.test_audit_e2e import MakeClient, _records, _wait_for

pytestmark = [pytest.mark.integration, pytest.mark.anyio]
SHOUT_ROLES = {"echo__shout": "approver"}


@pytest.fixture
async def ops_token(
    admin_registry: AdminRegistry, make_client: MakeClient, echo_url: str
) -> IssuedToken:
    await admin_registry.upsert_upstream("echo", echo_url, 2000, 5000)
    await admin_registry.upsert_tool_policy("echo", "say", "read", "test policy")
    _, ops = await make_client("harborline-ops-bot", ["echo__say", "echo__shout"])
    return ops


@pytest.fixture
def gateway(
    test_database_url: str,
    ops_token: IssuedToken,
    policy: None,
    policy_gateway_url: str,
    tmp_path: Path,
) -> Iterator[RunningGateway]:
    with run_gateway(
        test_database_url,
        tmp_path,
        policy_database_url=policy_gateway_url,
        approvals={"hold_s": 0.3, "poll_s": 0.05},
    ) as running:
        yield running


@pytest.fixture
async def person(test_database_url: str, policy_approver_url: str) -> Approvals:
    await _approver_add(test_database_url, argparse.Namespace(id="aiden", name="Aiden", role=None))
    return Approvals(policy_approver_url, SHOUT_ROLES)


async def test_a_write_waits_for_a_person_runs_once_when_approved_and_leaves_no_arguments_behind(
    gateway: RunningGateway,
    ops_token: IssuedToken,
    person: Approvals,
    policy_gateway_url: str,
    policy_approver_url: str,
) -> None:
    async with connect(gateway.url, ops_token.plaintext) as ops:
        read = await ops.call_tool("echo__say", {"text": "a read needs nobody"})
        waiting = await ops.call_tool("echo__shout", {"text": MARKER})
        assert read.is_error is False
        assert waiting.is_error is True, "a pending call must not look like a success"
        assert waiting.structured_content is not None
        assert waiting.structured_content["status"] == "approval_pending"
        request_id = UUID(waiting.structured_content["approval_id"])
        assert MARKER.upper() not in json.dumps(waiting.model_dump(mode="json"))
        assert "call_started" not in json.dumps(await _records(policy_gateway_url)), (
            "nothing was about to run"
        )

        await person.decide(request_id, "aiden", Decision.APPROVE, None)
        done = await ops.call_tool("echo__shout", {"text": MARKER})
        again = await ops.call_tool("echo__shout", {"text": MARKER})

    assert done.is_error is False
    assert done.content[0].text == MARKER.upper()  # type: ignore[union-attr]
    assert again.is_error is True, "one approval authorises one run"
    assert again.structured_content is not None
    assert again.structured_content["approval_id"] != str(request_id)

    # Nine: the calls' own records, and the queue's (requested, resolved, consumed, requested).
    records = await _wait_for(policy_gateway_url, 9)
    ran = [p for a, _, p in records if a == "gateway.tool_call" and p["outcome"] == "forwarded"]
    shout = next(p for p in ran if p["effect"] == "write")
    assert shout["approval_id"] == str(request_id)
    blocked = [
        p for a, _, p in records if a == "gateway.tool_call" and p.get("blocked_by") == "approval"
    ]
    assert [(p["deny_code"]) for p in blocked] == ["approval_pending", "approval_pending"]
    assert MARKER not in json.dumps(records)
    assert MARKER.upper() not in json.dumps(records)
    assert MARKER not in json.dumps([e.payload for e in gateway.events.events])
    # The one place the arguments are kept: for the approver.
    async with await psycopg.AsyncConnection.connect(policy_url(policy_approver_url)) as connection:
        cursor = await connection.execute("SELECT arguments_json FROM approval_arguments")
        assert MARKER in json.dumps(await cursor.fetchall())


async def test_a_rejected_write_never_runs_and_the_client_is_told_so(
    gateway: RunningGateway, ops_token: IssuedToken, person: Approvals
) -> None:
    async with connect(gateway.url, ops_token.plaintext) as ops:
        waiting = await ops.call_tool("echo__shout", {"text": "no"})
        assert waiting.structured_content is not None
        await person.decide(
            UUID(waiting.structured_content["approval_id"]), "aiden", Decision.REJECT, "no"
        )

        with pytest.raises(MCPError) as refused:
            await ops.call_tool("echo__shout", {"text": "no"})

    assert refused.value.message == "An approver rejected this call."


async def test_changed_arguments_are_a_new_request_the_old_approval_does_not_cover(
    gateway: RunningGateway, ops_token: IssuedToken, person: Approvals
) -> None:
    async with connect(gateway.url, ops_token.plaintext) as ops:
        first = await ops.call_tool("echo__shout", {"text": "close TKT-000001"})
        assert first.structured_content is not None
        await person.decide(
            UUID(first.structured_content["approval_id"]), "aiden", Decision.APPROVE, None
        )

        other = await ops.call_tool("echo__shout", {"text": "close TKT-000002"})

    assert other.is_error is True
    assert other.structured_content is not None
    assert other.structured_content["approval_id"] != first.structured_content["approval_id"]


async def test_with_no_policy_database_a_write_is_refused_not_waved_through(
    test_database_url: str, ops_token: IssuedToken, tmp_path: Path
) -> None:
    with run_gateway(test_database_url, tmp_path, approvals={}) as gateway:
        async with connect(gateway.url, ops_token.plaintext) as ops:
            with pytest.raises(MCPError) as refused:
                await ops.call_tool("echo__shout", {"text": "x"})
            read = await ops.call_tool("echo__say", {"text": "still works"})

    assert refused.value.message == "Request blocked by gateway policy."
    assert read.is_error is False


# --- the guarantees ---------------------------------------------------------------------------


async def test_no_client_can_list_or_call_anything_that_approves(
    gateway: RunningGateway, ops_token: IssuedToken
) -> None:
    async with connect(gateway.url, ops_token.plaintext) as ops:
        names = [tool.name for tool in (await ops.list_tools()).tools]
        for guess in ("approve", "approvals__approve", "gateway__approve", "policy__resolve"):
            with pytest.raises(MCPError):
                await ops.call_tool(guess, {"id": "1"})

    assert not [name for name in names if "approv" in name or "resolve" in name]


def test_the_gateways_http_surface_has_no_route_that_approves(
    test_database_url: str, tmp_path: Path
) -> None:
    app = create_app(_settings(test_database_url, tmp_path))

    paths = [getattr(route, "path", "") for route in app.routes]

    assert not [path for path in paths if "approv" in path or "resolve" in path]


def _settings(url: str, tmp_path: Path) -> GatewaySettings:
    pipeline = tmp_path / "pipeline.toml"
    pipeline.write_text('[layers]\napproval = "enforce"\n')
    roles = tmp_path / "approval_roles.toml"
    roles.write_text("[roles_by_action]\n")
    return GatewaySettings(
        database_url=SecretStr(url), pipeline_file=pipeline, approval_roles_file=roles
    )


async def test_the_gateways_own_role_cannot_approve_even_with_a_human_principal(
    policy: None, policy_gateway_url: str, policy_approver_url: str
) -> None:
    gate = _gate(policy_gateway_url)
    pending = await gate.decide(_context(), _call())

    # The library refuses on the requester's connection, before the database is asked ...
    with pytest.raises(ConfigError, match="cannot decide requests"):
        await gate._queue.resolve(
            UUID(pending.approval_id or ""), decision=Decision.APPROVE, principal=HUMAN
        )
    # ... and the database refuses the same change in plain SQL.
    async with await psycopg.AsyncConnection.connect(policy_url(policy_gateway_url)) as connection:
        with pytest.raises((errors.RaiseException, errors.InsufficientPrivilege)):
            await connection.execute(
                b"UPDATE agent_core_approvals SET status = 'approved', decision = 'approve',"
                b" resolved_by = 'human:x', resolved_at = to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"') WHERE id = %s",  # noqa: E501 - a3's canonical timestamp text
                (pending.approval_id,),
            )
    assert (
        await _approver(policy_approver_url).get(UUID(pending.approval_id or ""))
    ).status.value == "pending"


async def test_the_one_who_asked_cannot_approve_and_neither_can_an_agent_or_a_service(
    policy: None, policy_gateway_url: str, policy_approver_url: str
) -> None:
    ctx = _context()
    pending = await _gate(policy_gateway_url).decide(ctx, _call())
    approver = _approver(policy_approver_url)
    request_id = UUID(pending.approval_id or "")
    roles = frozenset({"approver"})

    for who in (
        Principal(id=ctx.client.actor_id, kind=PrincipalKind.HUMAN, roles=roles),  # asked itself
        Principal(id=ctx.client.actor_id, kind=PrincipalKind.AGENT, roles=roles),
        Principal(id="service:gateway", kind=PrincipalKind.SERVICE, roles=roles),
        Principal(id="human:no-role", kind=PrincipalKind.HUMAN),
    ):
        with pytest.raises(NotAuthorizedToResolveError):
            await approver.resolve(request_id, decision=Decision.APPROVE, principal=who)

    assert (await approver.get(request_id)).status.value == "pending"


async def test_nothing_waits_in_a_gateway_that_was_never_asked(
    gateway: RunningGateway, ops_token: IssuedToken
) -> None:
    async with connect(gateway.url, ops_token.plaintext) as ops:
        with anyio.fail_after(5):
            await ops.call_tool("echo__say", {"text": "read"})
