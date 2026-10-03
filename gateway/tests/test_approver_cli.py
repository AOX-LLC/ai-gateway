"""The approver's tool: what is shown, what is refused, and who may decide."""

import argparse
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import anyio
import psycopg
import pytest
from aox_agent_core.approvals import ApprovalRequest, Decision, approval_payload_hash
from aox_agent_core.errors import NotAuthorizedToResolveError

from ai_gateway.admin.cli import _approver_add, _approver_deactivate
from ai_gateway.approver.cli import Approvals, ApproverError, main
from ai_gateway.approver.display import ApprovalNotShowableError, render
from ai_gateway.policy import policy_url
from ai_gateway.seams.approvals import ApprovalOutcome
from tests.test_approval_gate import _call, _context, _gate

ACTION = "tickets__change_status"
ESCAPES = "\x1b[2J\x1b[1;31mAPPROVED BY SECURITY\x1b[0m\x1b]0;owned\x07‮\r"


def _request(arguments: Mapping[str, Any], **changes: object) -> ApprovalRequest:
    now = datetime.now(UTC)
    values: dict[str, object] = {
        "id": uuid4(),
        "action": ACTION,
        "summary": "tickets__change_status for client harborline-ops-bot",
        "payload_sha256": approval_payload_hash(ACTION, arguments),
        "requested_by": f"client:{uuid4()}",
        "required_role": "approver",
        "created_at": now,
        "expires_at": now + timedelta(minutes=30),
        **changes,
    }
    return ApprovalRequest.model_validate(values)


# --- what is shown ---------------------------------------------------------------------------


def test_what_is_shown_is_what_was_asked_for_and_says_it_was_checked() -> None:
    arguments = {"ticket_id": "TKT-000001", "status": "closed"}
    request = _request(arguments)

    shown = render(request, '{"status": "closed", "ticket_id": "TKT-000001"}')

    assert "TKT-000001" in shown.text
    assert "verified" in shown.text
    assert shown.suspicious == []


def test_control_characters_and_escape_sequences_in_the_arguments_are_shown_escaped() -> None:
    arguments = {"ticket_id": "TKT-000001", "note": ESCAPES}
    shown = render(_request(arguments), json.dumps(arguments))

    assert not {"\x1b", "\x07", "\r"} & set(shown.text)
    assert "‮" not in shown.text
    assert "\\u001b" in shown.text, "escaped, so the person sees there was something"
    assert shown.suspicious == ["note"]
    assert "non-ASCII or control characters" in shown.text


def test_free_text_from_outside_is_cleaned_of_escape_sequences() -> None:
    arguments = {"a": 1}
    request = _request(arguments, summary="ok\x1b[2J" + "x" * 10, requested_by=f"client:{uuid4()}")
    shown = render(request, json.dumps(arguments))

    assert "\x1b" not in shown.text


@pytest.mark.parametrize(
    ("stored", "why"),
    [
        (None, "not stored"),
        ('{"ticket_id": "TKT-000002", "status": "closed"}', "do not match"),
        ('{"status": "closed"}', "do not match"),
        ("not json", "not JSON"),
        ("[1, 2]", "not an object"),
    ],
)
def test_a_request_whose_arguments_are_missing_or_different_is_never_shown_as_approvable(
    stored: str | None, why: str
) -> None:
    request = _request({"ticket_id": "TKT-000001", "status": "closed"})

    with pytest.raises(ApprovalNotShowableError, match=why):
        render(request, stored)


# --- against the database -------------------------------------------------------------------


@pytest.fixture
async def people(policy: None, test_database_url: str) -> None:
    for approver_id in ("aiden", "tyler"):
        await _approver_add(
            test_database_url,
            argparse.Namespace(id=approver_id, name=approver_id.title(), role=None),
        )


async def _ask(url: str, **arguments: object) -> UUID:
    decision = await _gate(url).decide(_context(), _call(**arguments))
    assert decision.outcome is ApprovalOutcome.PENDING
    assert decision.approval_id
    return UUID(decision.approval_id)


async def _owner(url: str, statement: str, *params: object) -> None:
    async with await psycopg.AsyncConnection.connect(policy_url(url), autocommit=True) as db:
        await db.execute(statement, params)


@pytest.mark.integration
@pytest.mark.anyio
class TestAgainstTheDatabase:
    async def test_a_registered_approver_approves_and_the_call_then_goes_through(
        self, people: None, policy_gateway_url: str, policy_approver_url: str
    ) -> None:
        gate, ctx, call = _gate(policy_gateway_url), _context(), _call()
        pending = await gate.decide(ctx, call)

        request = await Approvals(policy_approver_url).decide(
            UUID(pending.approval_id or ""), "aiden", Decision.APPROVE, None
        )

        assert request.resolved_by == "human:aiden"
        assert (await gate.decide(ctx, call)).outcome is ApprovalOutcome.APPROVED

    async def test_nobody_unregistered_or_deactivated_can_decide(
        self,
        people: None,
        policy_gateway_url: str,
        policy_approver_url: str,
        test_database_url: str,
    ) -> None:
        request_id = await _ask(policy_gateway_url)
        approvals = Approvals(policy_approver_url)
        await _approver_deactivate(test_database_url, argparse.Namespace(id="tyler"))

        for who in ("stranger", "tyler"):
            with pytest.raises(ApproverError, match="not a registered, active approver"):
                await approvals.decide(request_id, who, Decision.APPROVE, None)
        assert (await approvals.queue.get(request_id)).status.value == "pending"

    async def test_an_approver_without_the_role_is_refused_by_the_queue(
        self,
        people: None,
        policy_gateway_url: str,
        policy_approver_url: str,
        test_database_url: str,
    ) -> None:
        await _approver_add(
            test_database_url, argparse.Namespace(id="intern", name="Intern", role=["reader"])
        )
        request_id = await _ask(policy_gateway_url)

        with pytest.raises(NotAuthorizedToResolveError):
            await Approvals(policy_approver_url).decide(
                request_id, "intern", Decision.APPROVE, None
            )

    async def test_arguments_changed_after_the_request_are_not_approvable_but_can_be_rejected(
        self,
        people: None,
        policy_gateway_url: str,
        policy_approver_url: str,
        test_database_url: str,
    ) -> None:
        request_id = await _ask(policy_gateway_url)
        await _owner(
            test_database_url,
            "UPDATE approval_arguments SET arguments_json = %s WHERE request_id = %s",
            json.dumps({"ticket_id": "TKT-999999", "status": "closed"}),
            str(request_id),
        )
        approvals = Approvals(policy_approver_url)

        with pytest.raises(ApprovalNotShowableError, match="do not match"):
            await approvals.decide(request_id, "aiden", Decision.APPROVE, None)
        assert (await approvals.queue.get(request_id)).status.value == "pending"
        rejected = await approvals.decide(request_id, "aiden", Decision.REJECT, "does not match")
        assert rejected.status.value == "rejected"

    async def test_a_request_whose_arguments_were_purged_is_not_approvable(
        self,
        people: None,
        policy_gateway_url: str,
        policy_approver_url: str,
        test_database_url: str,
    ) -> None:
        request_id = await _ask(policy_gateway_url)
        await _owner(test_database_url, "DELETE FROM approval_arguments")

        with pytest.raises(ApprovalNotShowableError, match="not stored"):
            await Approvals(policy_approver_url).decide(request_id, "aiden", Decision.APPROVE, None)

    async def test_the_command_lists_shows_and_approves(
        self,
        people: None,
        policy_gateway_url: str,
        policy_approver_url: str,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        request_id = await _ask(policy_gateway_url)
        monkeypatch.setenv("POLICY_APPROVER_DATABASE_URL", policy_approver_url)

        for argv in (
            ["--as", "aiden", "list"],
            ["--as", "aiden", "show", str(request_id)],
            ["--as", "aiden", "approve", "--yes", str(request_id)],
        ):
            await anyio.to_thread.run_sync(main, argv)

        output = capsys.readouterr().out
        assert str(request_id) in output
        assert "TKT-000001" in output
        assert f"approved: {request_id}" in output

    async def test_the_command_exits_with_one_line_for_an_unknown_approver(
        self,
        people: None,
        policy_approver_url: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("POLICY_APPROVER_DATABASE_URL", policy_approver_url)

        with pytest.raises(SystemExit) as exit_info:
            await anyio.to_thread.run_sync(main, ["--as", "nobody\x1b[2J", "list"])

        assert "\x1b" not in str(exit_info.value)
        assert "not a registered, active approver" in str(exit_info.value)
