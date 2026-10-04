"""The approver's tool: what is shown, what is refused, and who may decide."""

import inspect
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit
from uuid import UUID, uuid4

import anyio
import psycopg
import pytest
from aox_agent_core.approvals import ApprovalRequest, Decision, approval_payload_hash
from aox_agent_core.errors import NotAuthorizedToResolveError

from ai_gateway.approver.cli import Approvals, ApproverError, _sign_in, main
from ai_gateway.approver.display import ApprovalNotShowableError, render
from ai_gateway.policy import policy_url
from ai_gateway.seams.approvals import ApprovalOutcome
from tests.conftest import MakeApprover
from tests.test_approval_gate import ROLES, _call, _context, _gate

ROLES_FILE = Path(__file__).resolve().parents[2] / "config" / "approval_roles.toml"
ACTION = "tickets__change_status"
ESCAPES = "\x1b[2J\x1b[1;31mAPPROVED BY SECURITY\x1b[0m\x1b]0;owned\x07‮\r"


UPSTREAM = "0123456789abcdef0123456789abcdef"


def _stored(arguments: Mapping[str, Any], upstream: str = UPSTREAM) -> str:
    """What the gateway stores for the approver: the arguments and the upstream they go to."""
    return json.dumps({"arguments": arguments, "upstream": upstream})


def _request(arguments: Mapping[str, Any], **changes: object) -> ApprovalRequest:
    now = datetime.now(UTC)
    values: dict[str, object] = {
        "id": uuid4(),
        "action": ACTION,
        "summary": "tickets__change_status for client harborline-ops-bot",
        "payload_sha256": approval_payload_hash(
            ACTION, {"arguments": dict(arguments), "upstream": UPSTREAM}
        ),
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

    shown = render(request, _stored(arguments))

    assert "TKT-000001" in shown.text
    assert UPSTREAM in shown.text, "the person sees which upstream it goes to"
    assert "verified" in shown.text
    assert shown.suspicious == []


def test_control_characters_and_escape_sequences_in_the_arguments_are_shown_escaped() -> None:
    arguments = {"ticket_id": "TKT-000001", "note": ESCAPES}
    shown = render(_request(arguments), _stored(arguments))

    assert not {"\x1b", "\x07", "\r"} & set(shown.text)
    assert "‮" not in shown.text
    assert "\\u001b" in shown.text, "escaped, so the person sees there was something"
    assert shown.suspicious == ["note"]
    assert "non-ASCII or control characters" in shown.text


def test_free_text_from_outside_is_cleaned_of_escape_sequences() -> None:
    arguments = {"a": 1}
    # agent-core a6 refuses such text on the way in; a row written around it is still cleaned here.
    request = _request(arguments, requested_by=f"client:{uuid4()}").model_copy(
        update={"summary": "ok\x1b[2J" + "x" * 10}
    )
    shown = render(request, _stored(arguments))

    assert "\x1b" not in shown.text


@pytest.mark.parametrize(
    ("stored", "why"),
    [
        (None, "not stored"),
        (_stored({"ticket_id": "TKT-000002", "status": "closed"}), "do not match"),
        (_stored({"status": "closed"}), "do not match"),
        (
            _stored({"ticket_id": "TKT-000001", "status": "closed"}, "another-upstream"),
            "do not match",
        ),
        (
            json.dumps({"arguments": {"ticket_id": "TKT-000001", "status": "closed"}}),
            "not arguments",
        ),
        (json.dumps({"ticket_id": "TKT-000001", "status": "closed"}), "not arguments"),
        ("not json", "not JSON"),
        ("[1, 2]", "not an object"),
        (
            '{"arguments": {"ticket_id": "TKT-000001", "status": NaN}, "upstream": "'
            + UPSTREAM
            + '"}',
            "cannot be shown",
        ),
        (
            '{"upstream": "x", "arguments": {"a": ' + "[" * 5000 + "]" * 5000 + "}}",
            "not JSON|cannot be shown",
        ),
    ],
    ids=[
        "not-stored",
        "other-ticket",
        "fewer-keys",
        "other-upstream",
        "no-upstream",
        "bare-arguments",
        "not-json",
        "a-list",
        "nan",
        "deep-nesting",
    ],
)
def test_a_request_whose_arguments_are_missing_or_different_is_never_shown_as_approvable(
    stored: str | None, why: str
) -> None:
    request = _request({"ticket_id": "TKT-000001", "status": "closed"})

    with pytest.raises(ApprovalNotShowableError, match=why):
        render(request, stored)


# --- against the database -------------------------------------------------------------------


async def _ask(url: str, **arguments: Any) -> UUID:
    decision = await _gate(url).decide(_context(), _call(**arguments))
    assert decision.outcome is ApprovalOutcome.PENDING
    assert decision.approval_id
    return UUID(decision.approval_id)


async def _owner(url: str, statement: str, *params: object) -> None:
    async with await psycopg.AsyncConnection.connect(policy_url(url), autocommit=True) as db:
        await db.execute(statement, params)


async def _audit_roles(url: str) -> list[tuple[str, str, str]]:
    """(action, actor, database role that wrote it) of every approval decision."""
    async with await psycopg.AsyncConnection.connect(policy_url(url), autocommit=True) as db:
        cursor = await db.execute(
            "SELECT action, actor_id, db_role FROM agent_core_audit"
            " WHERE action = 'approval.resolved' ORDER BY seq"
        )
        return [(str(a), str(b), str(c)) for a, b, c in await cursor.fetchall()]


@pytest.mark.integration
@pytest.mark.anyio
class TestAgainstTheDatabase:
    async def test_an_approver_approves_and_the_call_then_goes_through(
        self, make_approver: MakeApprover, policy_gateway_url: str
    ) -> None:
        gate, ctx, call = _gate(policy_gateway_url), _context(), _call()
        pending = await gate.decide(ctx, call)

        request = await Approvals(await make_approver("aiden"), ROLES).decide(
            UUID(pending.approval_id or ""), Decision.APPROVE, None
        )

        assert request.resolved_by == "human:aiden"
        assert (await gate.decide(ctx, call)).outcome is ApprovalOutcome.APPROVED

    async def test_who_decided_is_the_login_that_decided_and_the_log_says_so(
        self, make_approver: MakeApprover, policy_gateway_url: str, test_database_url: str
    ) -> None:
        aiden, tyler = await make_approver("aiden"), await make_approver("tyler")
        first = await _ask(policy_gateway_url)
        second = await _ask(policy_gateway_url, ticket_id="TKT-000002", status="closed")

        await Approvals(aiden, ROLES).decide(first, Decision.APPROVE, None)
        await Approvals(tyler, ROLES).decide(second, Decision.REJECT, "no")

        assert [
            (a, who, role)
            for _, who, role in [await _audit_roles(test_database_url)][0]
            for a in ("x",)
        ] == [
            ("x", "human:aiden", "policy_approver_aiden"),
            ("x", "human:tyler", "policy_approver_tyler"),
        ]

    async def test_nobody_can_decide_as_someone_else_through_the_tool(
        self, make_approver: MakeApprover, policy_gateway_url: str
    ) -> None:
        """The tool takes no name: its principal is whoever the database says is signed in."""
        tyler = Approvals(await make_approver("tyler"), ROLES)
        await make_approver("aiden")

        assert (await tyler.principal()).id == "human:tyler"
        assert "approver" not in inspect.signature(tyler.decide).parameters

    async def test_a_login_that_is_not_an_active_approver_cannot_decide(
        self, make_approver: MakeApprover, policy_gateway_url: str, test_database_url: str
    ) -> None:
        tyler = Approvals(await make_approver("tyler"), ROLES)
        request_id = await _ask(policy_gateway_url)
        await _owner(test_database_url, "UPDATE approvers SET active = false WHERE id = 'tyler'")

        with pytest.raises(ApproverError, match="not a registered, active approver"):
            await tyler.decide(request_id, Decision.APPROVE, None)
        assert (await tyler.queue.get(request_id)).status.value == "pending"

    async def test_an_approver_without_the_role_is_refused_by_the_queue(
        self, make_approver: MakeApprover, policy_gateway_url: str
    ) -> None:
        intern = Approvals(await make_approver("intern", ["reader"]), ROLES)
        request_id = await _ask(policy_gateway_url)

        with pytest.raises(NotAuthorizedToResolveError):
            await intern.decide(request_id, Decision.APPROVE, None)

    async def test_arguments_changed_after_the_request_are_not_approvable_but_can_be_rejected(
        self, make_approver: MakeApprover, policy_gateway_url: str, test_database_url: str
    ) -> None:
        approvals = Approvals(await make_approver("aiden"), ROLES)
        request_id = await _ask(policy_gateway_url)
        await _owner(
            test_database_url,
            "UPDATE approval_arguments SET arguments_json = %s WHERE request_id = %s",
            _stored({"ticket_id": "TKT-999999", "status": "closed"}),
            str(request_id),
        )

        with pytest.raises(ApprovalNotShowableError, match="do not match"):
            await approvals.decide(request_id, Decision.APPROVE, None)
        assert (await approvals.queue.get(request_id)).status.value == "pending"
        rejected = await approvals.decide(request_id, Decision.REJECT, "does not match")
        assert rejected.status.value == "rejected"

    async def test_a_request_whose_arguments_were_purged_is_not_approvable(
        self, make_approver: MakeApprover, policy_gateway_url: str, test_database_url: str
    ) -> None:
        approvals = Approvals(await make_approver("aiden"), ROLES)
        request_id = await _ask(policy_gateway_url)
        await _owner(test_database_url, "DELETE FROM approval_arguments")

        with pytest.raises(ApprovalNotShowableError, match="not stored"):
            await approvals.decide(request_id, Decision.APPROVE, None)

    async def test_the_command_signs_in_with_the_login_from_the_environment_and_decides(
        self,
        make_approver: MakeApprover,
        policy_gateway_url: str,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        signed_in = urlsplit(await make_approver("aiden"))
        request_id = await _ask(policy_gateway_url)
        # The URL names the database and nothing else; who signs in comes from the environment.
        database = urlunsplit(signed_in._replace(netloc=f"{signed_in.hostname}:{signed_in.port}"))
        monkeypatch.setenv("POLICY_APPROVER_DATABASE_URL", database)
        monkeypatch.setenv("APPROVER_LOGIN", signed_in.username or "")
        monkeypatch.setenv("APPROVER_PASSWORD", signed_in.password or "")
        monkeypatch.setenv("APPROVAL_ROLES_FILE", str(ROLES_FILE))

        for argv in (
            ["whoami"],
            ["list"],
            ["show", str(request_id)],
            ["approve", "--yes", str(request_id)],
        ):
            await anyio.to_thread.run_sync(main, argv)

        output = capsys.readouterr().out
        assert "human:aiden" in output
        assert str(request_id) in output
        assert "TKT-000001" in output
        assert "upstream   tickets (identity" in output, "the upstream is named, with its identity"
        assert f"approved: {request_id} (decided by human:aiden)" in output

    async def test_the_command_exits_with_one_line_for_a_login_that_is_not_an_approver(
        self,
        policy: None,
        test_database_url: str,
        policy_auditor_url: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The auditor's login connects, and is not an approver: the same refusal as any stranger.
        monkeypatch.setenv("POLICY_APPROVER_DATABASE_URL", policy_auditor_url)
        monkeypatch.setenv("APPROVAL_ROLES_FILE", str(ROLES_FILE))

        with pytest.raises(SystemExit) as exit_info:
            await anyio.to_thread.run_sync(main, ["whoami"])

        assert "not a registered, active approver" in str(exit_info.value)
        assert "\n" not in str(exit_info.value)

    async def test_a_wrong_password_is_one_line_and_never_shows_the_password(
        self,
        make_approver: MakeApprover,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        signed_in = urlsplit(await make_approver("aiden"))
        database = urlunsplit(signed_in._replace(netloc=f"{signed_in.hostname}:{signed_in.port}"))
        monkeypatch.setenv("POLICY_APPROVER_DATABASE_URL", database)
        monkeypatch.setenv("APPROVER_LOGIN", signed_in.username or "")
        monkeypatch.setenv("APPROVER_PASSWORD", "wrong-password-0123456789")
        monkeypatch.setenv("APPROVAL_ROLES_FILE", str(ROLES_FILE))

        with pytest.raises(SystemExit) as exit_info:
            await anyio.to_thread.run_sync(main, ["whoami"])

        assert "wrong-password-0123456789" not in str(exit_info.value)
        assert "\n" not in str(exit_info.value)


def test_the_command_asks_when_the_environment_has_no_login_and_says_so_without_a_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("APPROVER_LOGIN", raising=False)
    monkeypatch.delenv("APPROVER_PASSWORD", raising=False)
    answers = iter(["policy_approver_aiden"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    monkeypatch.setattr("getpass.getpass", lambda _prompt: "p@ss/word:1")

    url = _sign_in("postgresql://postgres:5432/ai_gateway")

    parts = urlsplit(url)
    assert (parts.username, parts.password) == ("policy_approver_aiden", "p%40ss%2Fword%3A1")
    assert unquote(parts.password or "") == "p@ss/word:1"
    assert (parts.hostname, parts.port) == ("postgres", 5432)

    def no_terminal(_prompt: str) -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", no_terminal)
    with pytest.raises(ApproverError, match="APPROVER_LOGIN and APPROVER_PASSWORD"):
        _sign_in("postgresql://postgres:5432/ai_gateway")


def test_a_url_that_already_names_a_login_is_used_as_it_is() -> None:
    url = "postgresql://policy_approver_aiden:pw@postgres:5432/ai_gateway"

    assert _sign_in(url) == url
