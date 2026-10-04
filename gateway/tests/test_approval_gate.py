"""The approval gate on the real policy database: asking, holding, approving, using once."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import anyio
import psycopg
import pytest
from aox_agent_core.approvals import (
    Decision,
    Principal,
    PrincipalKind,
    RoleApproverPolicy,
    SQLApprovalQueue,
    approval_payload_hash,
)
from aox_agent_core.storage import open_database
from psycopg import errors
from pydantic import SecretStr

from ai_gateway.pipeline.types import CallContext, ClientIdentity, ToolCall
from ai_gateway.policy import approval_queue_on, policy_url
from ai_gateway.policy.approvals import PostgresApprovalGate, _payload, approval_summary
from ai_gateway.policy.database import BoundedPostgresDatabase
from ai_gateway.policy.setup import PolicyPasswords, setup_policy
from ai_gateway.seams.approvals import ApprovalOutcome
from tests.conftest import AIDEN, password_of

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

ROLES = {"tickets__change_status": "approver"}
MARKER = "dock-7-account-4111-1111-1111-1111"
HUMAN = Principal(id=f"human:{AIDEN}", kind=PrincipalKind.HUMAN, roles=frozenset({"approver"}))


def _context(client_id: UUID | None = None, name: str = "harborline-ops-bot") -> CallContext:
    return CallContext(
        request_id=uuid4(),
        client=ClientIdentity(id=client_id or uuid4(), name=name, scopes=frozenset()),
        session_id=None,
        protocol_version="2025-11-25",
    )


def _call(upstream_identity: str = "", **arguments: Any) -> ToolCall:
    return ToolCall.create(
        "tickets__change_status",
        "tickets",
        "change_status",
        arguments or {"ticket_id": "TKT-000001", "status": "closed", "note": MARKER},
        "write",
        upstream_identity=upstream_identity,
    )


def _gate(url: str, **options: Any) -> PostgresApprovalGate:
    database = BoundedPostgresDatabase(SecretStr(policy_url(url)), concurrency=4)
    queue = approval_queue_on(database)
    options = {"hold_s": 0.3, "poll_s": 0.05, "roles_by_action": ROLES, **options}
    return PostgresApprovalGate(queue, **options)


def _approver(url: str) -> SQLApprovalQueue:
    database = open_database(SecretStr(policy_url(url)))
    return approval_queue_on(database, policy=RoleApproverPolicy(roles_by_action=ROLES))


async def _decide_when_asked(approver: SQLApprovalQueue, decision: Decision) -> None:
    """Play the person: wait for a request to appear, then decide it."""
    for _ in range(100):
        pending = await approver.list_pending(HUMAN)
        if pending:
            await approver.resolve(pending[0].id, decision=decision, principal=HUMAN)
            return
        await anyio.sleep(0.02)
    raise AssertionError("no request appeared")


async def test_a_write_with_no_decision_is_pending_and_asking_again_does_not_ask_twice(
    policy: None, policy_gateway_url: str, policy_approver_url: str
) -> None:
    gate, ctx, call = _gate(policy_gateway_url), _context(), _call()

    first = await gate.decide(ctx, call)
    second = await gate.decide(ctx, call)

    assert first.outcome is ApprovalOutcome.PENDING
    assert second.outcome is ApprovalOutcome.PENDING
    assert second.approval_id == first.approval_id, "the retry finds the request it made"
    assert len(await _approver(policy_approver_url).list_pending(HUMAN)) == 1


async def test_an_approval_made_during_the_hold_lets_the_call_through_once(
    policy: None, policy_gateway_url: str, policy_approver_url: str
) -> None:
    gate, ctx, call = _gate(policy_gateway_url, hold_s=5), _context(), _call()
    approver = _approver(policy_approver_url)

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(_decide_when_asked, approver, Decision.APPROVE)
        decision = await gate.decide(ctx, call)

    assert decision.outcome is ApprovalOutcome.APPROVED
    again = await gate.decide(ctx, call)
    assert again.outcome is ApprovalOutcome.PENDING, "one approval, one run: a new request"
    assert again.approval_id != decision.approval_id


async def test_an_approval_that_arrives_after_the_hold_is_used_by_the_retry(
    policy: None, policy_gateway_url: str, policy_approver_url: str
) -> None:
    gate, ctx, call = _gate(policy_gateway_url), _context(), _call()
    pending = await gate.decide(ctx, call)
    await _approver(policy_approver_url).resolve(
        UUID(pending.approval_id or ""), decision=Decision.APPROVE, principal=HUMAN
    )

    retried = await gate.decide(ctx, call)

    assert retried.outcome is ApprovalOutcome.APPROVED
    assert retried.approval_id == pending.approval_id


async def test_a_rejection_stands_and_a_retry_does_not_ask_again(
    policy: None, policy_gateway_url: str, policy_approver_url: str
) -> None:
    gate, ctx, call = _gate(policy_gateway_url), _context(), _call()
    pending = await gate.decide(ctx, call)
    approver = _approver(policy_approver_url)
    await approver.resolve(
        UUID(pending.approval_id or ""), decision=Decision.REJECT, principal=HUMAN
    )

    first, second = await gate.decide(ctx, call), await gate.decide(ctx, call)

    assert [first.outcome, second.outcome] == [ApprovalOutcome.REJECTED] * 2
    assert await approver.list_pending(HUMAN) == []


async def test_an_approval_covers_exactly_the_tool_and_the_arguments_it_was_made_for(
    policy: None, policy_gateway_url: str, policy_approver_url: str
) -> None:
    gate, ctx = _gate(policy_gateway_url), _context()
    pending = await gate.decide(ctx, _call())
    await _approver(policy_approver_url).resolve(
        UUID(pending.approval_id or ""), decision=Decision.APPROVE, principal=HUMAN
    )

    other_arguments = await gate.decide(ctx, _call(ticket_id="TKT-000002", status="closed"))

    assert other_arguments.outcome is ApprovalOutcome.PENDING
    assert other_arguments.approval_id != pending.approval_id


async def test_another_clients_approval_is_not_usable_by_a_client_with_the_same_arguments(
    policy: None, policy_gateway_url: str, policy_approver_url: str
) -> None:
    gate, owner, other = _gate(policy_gateway_url), _context(), _context(name="other-bot")
    pending = await gate.decide(owner, _call())
    await _approver(policy_approver_url).resolve(
        UUID(pending.approval_id or ""), decision=Decision.APPROVE, principal=HUMAN
    )

    stranger = await gate.decide(other, _call())

    assert stranger.outcome is ApprovalOutcome.PENDING, "it asks for an approval of its own"
    assert stranger.approval_id != pending.approval_id


async def test_consume_refuses_a_request_somebody_else_asked_for(
    policy: None, policy_gateway_url: str, policy_approver_url: str
) -> None:
    """The gate checks whose request it is before it consumes (agent-core a3 checks it again)."""
    gate, owner, thief = _gate(policy_gateway_url), _context(), _context(name="other-bot")
    call = _call()
    pending = await gate.decide(owner, call)
    approver = _approver(policy_approver_url)
    request = await approver.resolve(
        UUID(pending.approval_id or ""), decision=Decision.APPROVE, principal=HUMAN
    )

    stolen = await gate._use(
        request,
        call,
        Principal(id=thief.client.actor_id, kind=PrincipalKind.SERVICE),
        _payload(call),
    )

    assert stolen.outcome is ApprovalOutcome.UNAVAILABLE
    still = await gate._queue.get(request.id)
    assert still.status.value == "approved", "and it was not used up"


async def test_a_request_that_expired_before_it_was_decided_is_asked_for_afresh(
    policy: None, policy_gateway_url: str
) -> None:
    gate, ctx, call = _gate(policy_gateway_url, ttl_s=1, hold_s=0), _context(), _call()
    first = await gate.decide(ctx, call)
    await anyio.sleep(1.2)

    second = await gate.decide(ctx, call)

    assert second.outcome is ApprovalOutcome.PENDING
    assert second.approval_id != first.approval_id


async def test_the_arguments_are_kept_for_the_approver_and_hash_to_the_request(
    policy: None, policy_gateway_url: str, policy_approver_url: str
) -> None:
    gate, ctx, call = _gate(policy_gateway_url), _context(), _call()
    pending = await gate.decide(ctx, call)

    async with await psycopg.AsyncConnection.connect(policy_url(policy_approver_url)) as connection:
        cursor = await connection.execute(
            "SELECT a.arguments_json, r.payload_sha256 FROM approval_arguments a"
            " JOIN agent_core_approvals r ON r.id = a.request_id WHERE a.request_id = %s",
            (pending.approval_id,),
        )
        row = await cursor.fetchone()

    assert row is not None
    assert json.loads(row[0]) == {"arguments": call.arguments, "upstream": call.upstream_identity}
    assert approval_payload_hash("tickets__change_status", json.loads(row[0])) == row[1]


async def test_only_the_approver_can_read_the_arguments_and_nobody_else_can_change_them(
    policy: None,
    policy_gateway_url: str,
    policy_approver_url: str,
    policy_auditor_url: str,
) -> None:
    gate = _gate(policy_gateway_url)
    pending = await gate.decide(_context(), _call())

    for url in (policy_gateway_url, policy_auditor_url):
        async with await psycopg.AsyncConnection.connect(policy_url(url)) as connection:
            with pytest.raises(errors.InsufficientPrivilege):
                await connection.execute("SELECT arguments_json FROM approval_arguments")
    for statement in (
        "UPDATE approval_arguments SET arguments_json = '{}'",
        "DELETE FROM approval_arguments",
        "INSERT INTO approval_arguments (request_id, arguments_json) VALUES ('x', '{}')",
    ):
        async with await psycopg.AsyncConnection.connect(
            policy_url(policy_approver_url), autocommit=True
        ) as connection:
            with pytest.raises(errors.InsufficientPrivilege):
                await connection.execute(statement)
    assert pending.approval_id


async def test_the_arguments_are_in_no_audit_record(
    policy: None, policy_gateway_url: str, policy_auditor_url: str
) -> None:
    gate = _gate(policy_gateway_url)
    await gate.decide(_context(), _call())

    async with await psycopg.AsyncConnection.connect(policy_url(policy_auditor_url)) as connection:
        cursor = await connection.execute(
            "SELECT payload, subject_id, actor_id FROM agent_core_audit"
        )
        rows = await cursor.fetchall()

    assert rows, "the request itself is audited"
    assert MARKER not in json.dumps(rows)
    assert "TKT-000001" not in json.dumps(rows)


async def test_arguments_too_large_to_show_an_approver_are_refused(
    policy: None, policy_gateway_url: str, policy_approver_url: str
) -> None:
    gate = _gate(policy_gateway_url)

    decision = await gate.decide(_context(), _call(blob="x" * 70_000))

    assert decision.outcome is ApprovalOutcome.UNAVAILABLE
    assert await _approver(policy_approver_url).list_pending(HUMAN) == []


async def test_a_dead_database_makes_the_approval_unavailable_and_never_raises() -> None:
    gate = _gate("postgresql://policy_gateway:x@127.0.0.1:1/ai_gateway_test")

    decision = await gate.decide(_context(), _call())

    assert decision.outcome is ApprovalOutcome.UNAVAILABLE


async def test_calls_beyond_the_hold_limit_are_answered_pending_at_once(
    policy: None, policy_gateway_url: str
) -> None:
    gate = _gate(policy_gateway_url, hold_s=5, max_holds=0)

    with anyio.fail_after(2):
        decision = await gate.decide(_context(), _call())

    assert decision.outcome is ApprovalOutcome.PENDING


async def test_the_purge_removes_arguments_older_than_a_week_and_only_the_gateway_may_run_it(
    policy: None,
    policy_gateway_url: str,
    policy_approver_url: str,
    policy_auditor_url: str,
    test_database_url: str,
) -> None:
    gate = _gate(policy_gateway_url)
    old = await gate.decide(_context(), _call(n=1))
    fresh = await gate.decide(_context(), _call(n=2))
    async with await psycopg.AsyncConnection.connect(
        policy_url(test_database_url), autocommit=True
    ) as owner:
        await owner.execute(
            "UPDATE approval_arguments SET created_at = %s WHERE request_id = %s",
            (datetime.now(UTC) - timedelta(days=7, minutes=1), old.approval_id),
        )
    for url in (policy_approver_url, policy_auditor_url):
        async with await psycopg.AsyncConnection.connect(policy_url(url)) as connection:
            with pytest.raises(errors.InsufficientPrivilege):
                await connection.execute("SELECT policy.purge_approval_arguments()")

    async with await psycopg.AsyncConnection.connect(
        policy_url(policy_gateway_url), autocommit=True
    ) as connection:
        cursor = await connection.execute("SELECT policy.purge_approval_arguments()")
        assert await cursor.fetchone() == (1,)
    async with await psycopg.AsyncConnection.connect(policy_url(policy_approver_url)) as connection:
        cursor = await connection.execute("SELECT request_id FROM approval_arguments")
        assert await cursor.fetchall() == [(fresh.approval_id,)]


async def test_the_dashboard_reader_sees_requests_without_arguments_and_nothing_else_in_policy(
    telemetry: None, policy: None, policy_gateway_url: str, reader_url: str
) -> None:
    """`telemetry` first: the view's grant goes to the reader role if it exists at setup."""
    pending = await _gate(policy_gateway_url).decide(_context(), _call())

    async with await psycopg.AsyncConnection.connect(policy_url(reader_url)) as connection:
        cursor = await connection.execute("SELECT * FROM dash_approvals")
        assert cursor.description is not None
        columns = [column.name for column in cursor.description]
        rows = await cursor.fetchall()
        assert [row[0] for row in rows] == [pending.approval_id]
        assert {"id", "action", "status", "client_actor", "created_at"} <= set(columns)
        assert not {"summary", "payload_sha256", "reason", "run_context"} & set(columns)
        for table in (
            "agent_core_approvals",
            "approval_arguments",
            "agent_core_audit",
            "approvers",
        ):
            await connection.rollback()
            with pytest.raises(errors.InsufficientPrivilege):
                await connection.execute(f"SELECT * FROM {table}".encode())


async def test_submit_is_idempotent_while_a_request_for_the_intent_is_open(
    policy: None, policy_gateway_url: str, policy_approver_url: str
) -> None:
    """The sharing of one request between identical calls is agent-core's, enforced by its unique
    index over open requests: a second submission for the same client, tool and payload returns the
    first while it is pending or approved and unused, and a used approval is closed, so a new
    intent asks again."""
    gate, ctx, call = _gate(policy_gateway_url, hold_s=0), _context(), _call()
    requester = Principal(id=ctx.client.actor_id, kind=PrincipalKind.SERVICE)
    first = await gate.decide(ctx, call)

    again = await gate._submit(ctx, call, requester, _payload(call))
    assert again is not None
    assert str(again.id) == first.approval_id, "the open request, not a second one"
    await _approver(policy_approver_url).resolve(
        UUID(first.approval_id or ""), decision=Decision.APPROVE, principal=HUMAN
    )
    approved = await gate._submit(ctx, call, requester, _payload(call))
    assert approved is not None
    assert str(approved.id) == first.approval_id, "approved and unused is still open"
    assert (await gate.decide(ctx, call)).outcome is ApprovalOutcome.APPROVED  # consumed

    fresh = await gate._submit(ctx, call, requester, _payload(call))
    assert fresh is not None
    assert str(fresh.id) != first.approval_id, "a used approval is closed: a new intent may ask"


async def test_a_call_made_while_an_approved_request_exists_uses_it_and_asks_nobody(
    policy: None, policy_gateway_url: str, policy_approver_url: str
) -> None:
    """One approval, one run: never two requests a person could approve for one intent."""
    gate, ctx, call = _gate(policy_gateway_url, hold_s=0), _context(), _call()
    older = await gate.decide(ctx, call)
    await _approver(policy_approver_url).resolve(
        UUID(older.approval_id or ""), decision=Decision.APPROVE, principal=HUMAN
    )

    decision = await gate.decide(ctx, call)

    assert decision.outcome is ApprovalOutcome.APPROVED
    assert decision.approval_id == older.approval_id, "the approved request ran"
    assert await _approver(policy_approver_url).list_pending(HUMAN) == [], "nothing new was asked"


async def test_simultaneous_identical_calls_share_one_pending_request(
    policy: None, policy_gateway_url: str, policy_approver_url: str
) -> None:
    gate, ctx, call = _gate(policy_gateway_url, hold_s=0), _context(), _call()
    ids: list[str | None] = []

    async def ask() -> None:
        ids.append((await gate.decide(ctx, call)).approval_id)

    async with anyio.create_task_group() as tasks:
        for _ in range(6):
            tasks.start_soon(ask)

    assert len(set(ids)) == 1, "one request, not six"
    assert None not in ids
    assert len(await _approver(policy_approver_url).list_pending(HUMAN)) == 1


async def test_an_approver_cannot_get_two_runs_from_one_intent(
    policy: None, policy_gateway_url: str, policy_approver_url: str
) -> None:
    """Six identical calls, then one approval: exactly one of them runs, and the next identical call
    is a new intent that asks again."""
    gate, ctx, call = _gate(policy_gateway_url, hold_s=0), _context(), _call()
    first = await gate.decide(ctx, call)
    await _approver(policy_approver_url).resolve(
        UUID(first.approval_id or ""), decision=Decision.APPROVE, principal=HUMAN
    )
    outcomes = []

    async def retry() -> None:
        outcomes.append((await gate.decide(ctx, call)).outcome)

    async with anyio.create_task_group() as tasks:
        for _ in range(6):
            tasks.start_soon(retry)

    assert outcomes.count(ApprovalOutcome.APPROVED) == 1
    after = await gate.decide(ctx, call)
    assert after.outcome is ApprovalOutcome.PENDING
    assert after.approval_id != first.approval_id


async def test_a_repeat_that_differs_in_role_or_delegates_is_a_conflict_not_a_reuse(
    policy: None, policy_gateway_url: str, policy_approver_url: str
) -> None:
    ctx, call = _context(), _call()
    first = await _gate(policy_gateway_url, hold_s=0).decide(ctx, call)
    assert first.outcome is ApprovalOutcome.PENDING

    other_role = _gate(
        policy_gateway_url, hold_s=0, roles_by_action={"tickets__change_status": "senior"}
    )
    other_lifetime = _gate(policy_gateway_url, hold_s=0, ttl_s=60)

    for gate in (other_role, other_lifetime):
        decision = await gate.decide(ctx, call)
        assert decision.outcome is ApprovalOutcome.UNAVAILABLE, "refused, and nothing reused"
    stored = await _approver(policy_approver_url).get(UUID(first.approval_id or ""))
    assert stored.status.value == "pending"

    # A request with delegates, made by something other than the gateway (it never passes any).
    database = BoundedPostgresDatabase(SecretStr(policy_url(policy_gateway_url)), concurrency=2)
    queue = approval_queue_on(database)
    requester = Principal(id=_context().client.actor_id, kind=PrincipalKind.SERVICE)
    second_ctx = _context()
    payload = _payload(call)
    await queue.submit(
        action=call.exposed_name,
        summary="s",
        payload=payload,
        requested_by=Principal(id=second_ctx.client.actor_id, kind=PrincipalKind.SERVICE),
        required_role="approver",
        ttl_seconds=1800,
        delegates=["client:someone-else"],
    )
    assert requester.id != second_ctx.client.actor_id
    decision = await _gate(policy_gateway_url, hold_s=0).decide(second_ctx, call)
    assert decision.outcome is ApprovalOutcome.UNAVAILABLE


async def test_one_client_cannot_fill_every_wait_slot(
    policy: None, policy_gateway_url: str, policy_approver_url: str
) -> None:
    gate = _gate(policy_gateway_url, hold_s=2, poll_s=0.1, max_holds=6, max_holds_per_client=2)
    greedy = _context(name="greedy-bot")
    polite = _context(name="polite-bot")
    results: dict[str, tuple[ApprovalOutcome, float]] = {}

    async def ask(label: str, ctx: CallContext, n: int) -> None:
        started = anyio.current_time()
        decision = await gate.decide(ctx, _call(ticket_id=f"TKT-{label}-{n}", status="closed"))
        results[f"{label}{n}"] = (decision.outcome, anyio.current_time() - started)

    async with anyio.create_task_group() as tasks:
        for n in range(4):  # four different writes at once from one client: two may wait
            tasks.start_soon(ask, "g", greedy, n)
        await anyio.sleep(0.3)
        tasks.start_soon(ask, "p", polite, 0)  # another client still gets a slot

    waited = sorted(label for label, (_, seconds) in results.items() if seconds >= 1.5)
    quick = sorted(label for label, (_, seconds) in results.items() if seconds < 1.5)
    assert len([label for label in waited if label.startswith("g")]) == 2
    assert "p0" in waited, "the other client waited like any other: it was not locked out"
    assert len([label for label in quick if label.startswith("g")]) == 2


async def test_the_gateway_inserts_only_the_arguments_and_cannot_choose_their_retention(
    policy: None, policy_gateway_url: str
) -> None:
    pending = await _gate(policy_gateway_url, hold_s=0).decide(_context(), _call())

    async with await psycopg.AsyncConnection.connect(
        policy_url(policy_gateway_url), autocommit=True
    ) as connection:
        for statement in (
            "INSERT INTO approval_arguments (request_id, arguments_json, created_at)"
            " VALUES (%s, '{}', '9999-01-01')",
            "UPDATE approval_arguments SET arguments_json = '{}'",
            "DELETE FROM approval_arguments",
            "SELECT arguments_json FROM approval_arguments",
        ):
            with pytest.raises(errors.InsufficientPrivilege):
                await connection.execute(
                    statement.encode(), (pending.approval_id,) if "%s" in statement else None
                )


async def test_a_hand_given_create_on_the_schema_does_not_survive_setup(
    policy: None,
    test_database_url: str,
    policy_gateway_url: str,
    policy_approver_url: str,
    policy_auditor_url: str,
) -> None:
    async with await psycopg.AsyncConnection.connect(
        test_database_url, autocommit=True
    ) as connection:
        await connection.execute("GRANT CREATE ON SCHEMA policy TO policy_approver, policy_gateway")
    await setup_policy(
        test_database_url,
        PolicyPasswords(password_of(policy_gateway_url), password_of(policy_auditor_url)),
    )

    async with await psycopg.AsyncConnection.connect(
        test_database_url, autocommit=True
    ) as connection:
        cursor = await connection.execute(
            "SELECT r, has_schema_privilege(r, 'policy', 'CREATE'),"
            " has_schema_privilege(r, 'policy', 'USAGE')"
            " FROM unnest(ARRAY['policy_gateway', 'policy_approver', 'policy_auditor']) AS r"
            " ORDER BY 1"
        )
        assert await cursor.fetchall() == [
            ("policy_approver", False, True),
            ("policy_auditor", False, True),
            ("policy_gateway", False, True),
        ]


async def test_two_retries_at_once_after_one_approval_run_the_call_exactly_once(
    policy: None, policy_gateway_url: str, policy_approver_url: str
) -> None:
    gate, ctx, call = _gate(policy_gateway_url, hold_s=0), _context(), _call()
    pending = await gate.decide(ctx, call)
    await _approver(policy_approver_url).resolve(
        UUID(pending.approval_id or ""), decision=Decision.APPROVE, principal=HUMAN
    )
    outcomes = []

    async def retry() -> None:
        outcomes.append((await gate.decide(ctx, call)).outcome)

    async with anyio.create_task_group() as tasks:
        for _ in range(4):
            tasks.start_soon(retry)

    assert outcomes.count(ApprovalOutcome.APPROVED) == 1, "one approval, one run"
    assert not set(outcomes) - {
        ApprovalOutcome.APPROVED,
        ApprovalOutcome.UNAVAILABLE,
        ApprovalOutcome.PENDING,
    }


async def test_an_approval_that_expired_unused_does_not_block_the_same_write_for_good(
    policy: None, policy_gateway_url: str, policy_approver_url: str
) -> None:
    """An approved request that runs out unused must not stay in the way: the same write, later, is
    asked for afresh and gets a request of its own (agent-core closes the lapsed one as expired)."""
    gate, ctx, call = _gate(policy_gateway_url, ttl_s=1, hold_s=0), _context(), _call()
    pending = await gate.decide(ctx, call)
    await _approver(policy_approver_url).resolve(
        UUID(pending.approval_id or ""), decision=Decision.APPROVE, principal=HUMAN
    )
    await anyio.sleep(1.2)

    for _ in range(3):  # not once: every identical call after it
        retried = await gate.decide(ctx, call)
        assert retried.outcome is ApprovalOutcome.PENDING
        assert retried.approval_id != pending.approval_id
    stored = await _approver(policy_approver_url).get(UUID(pending.approval_id or ""))
    assert stored.status.value == "expired", "the old one was closed, unused"


async def test_an_approval_does_not_carry_over_to_another_upstream(
    policy: None, policy_gateway_url: str, policy_approver_url: str
) -> None:
    """The payload a person approves names the upstream the arguments go to. The same arguments
    sent to a repointed upstream are a different request, asked afresh, never approved by this."""
    gate, ctx = _gate(policy_gateway_url, hold_s=0), _context()
    before = _call(upstream_identity="aaaa")
    after = _call(upstream_identity="bbbb")
    pending = await gate.decide(ctx, before)
    await _approver(policy_approver_url).resolve(
        UUID(pending.approval_id or ""), decision=Decision.APPROVE, principal=HUMAN
    )

    moved = await gate.decide(ctx, after)

    assert moved.outcome is ApprovalOutcome.PENDING, "a new request for the new upstream"
    assert moved.approval_id != pending.approval_id
    assert (await gate.decide(ctx, before)).outcome is ApprovalOutcome.APPROVED, (
        "the old one is intact"
    )


# --- agent-core a7: summary, conflicts, standing rejections --------------------------------------


def test_the_summary_is_a_pure_function_of_the_tool_and_its_arguments() -> None:
    first = approval_summary("tickets__change_status", {"ticket_id": "TKT-000001", "status": "x"})
    reordered = approval_summary(
        "tickets__change_status", {"status": "x", "ticket_id": "TKT-000001"}
    )
    other = approval_summary("tickets__change_status", {"ticket_id": "TKT-000002", "status": "x"})

    assert first == reordered, "key order does not matter"
    assert first != other, "another call reads differently"
    assert first.startswith("tickets__change_status ")
    assert "TKT-000001" not in first, "no argument text: the approver reads the stored payload"


async def test_two_retries_by_differently_named_clients_submit_the_identical_summary(
    policy: None, policy_gateway_url: str, policy_approver_url: str
) -> None:
    """agent-core treats a repeat with another summary as a conflict, so nothing about who asks or
    when may reach it."""
    client_id = uuid4()
    gate, call = _gate(policy_gateway_url, hold_s=0), _call()
    first = await gate.decide(_context(client_id, "harborline-ops-bot"), call)
    renamed = await gate.decide(_context(client_id, "renamed-since"), call)

    assert renamed.outcome is ApprovalOutcome.PENDING, "the same request, not a conflict"
    assert renamed.approval_id == first.approval_id
    stored = await _approver(policy_approver_url).get(UUID(first.approval_id or ""))
    assert stored.summary == approval_summary(call.exposed_name, call.arguments)


async def _leave_a_request_as_the_previous_release_did(
    gate: PostgresApprovalGate, ctx: CallContext, call: ToolCall
) -> UUID:
    """An open request with the previous release's client-named summary (and no stored payload)."""
    request = await gate._queue.submit(
        action=call.exposed_name,
        summary=f"{call.exposed_name} for client {ctx.client.name}",
        payload=_payload(call),
        requested_by=Principal(id=ctx.client.actor_id, kind=PrincipalKind.SERVICE),
        required_role=ROLES[call.exposed_name],
        ttl_seconds=gate._ttl_s,
    )
    return request.id


async def test_a_pending_request_left_by_the_previous_release_is_withdrawn_and_asked_afresh(
    policy: None, policy_gateway_url: str, policy_approver_url: str
) -> None:
    gate, ctx, call = _gate(policy_gateway_url, hold_s=0), _context(), _call()
    old = await _leave_a_request_as_the_previous_release_did(gate, ctx, call)

    decision = await gate.decide(ctx, call)

    assert decision.outcome is ApprovalOutcome.PENDING
    assert decision.approval_id != str(old), "a new request, with the current summary"
    approver = _approver(policy_approver_url)
    assert (await approver.get(old)).status.value == "cancelled"
    assert [str(r.id) for r in await approver.list_pending(HUMAN)] == [decision.approval_id]


async def test_an_approved_request_left_by_the_previous_release_is_still_used(
    policy: None, policy_gateway_url: str, policy_approver_url: str
) -> None:
    gate, ctx, call = _gate(policy_gateway_url, hold_s=0), _context(), _call()
    old = await _leave_a_request_as_the_previous_release_did(gate, ctx, call)
    await _approver(policy_approver_url).resolve(old, decision=Decision.APPROVE, principal=HUMAN)

    decision = await gate.decide(ctx, call)

    assert decision.outcome is ApprovalOutcome.APPROVED
    assert decision.approval_id == str(old)


async def test_a_conflict_that_is_not_the_previous_releases_is_refused_and_logged(
    policy: None, policy_gateway_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    gate, ctx, call = _gate(policy_gateway_url, hold_s=0), _context(), _call()
    await gate.decide(ctx, call)

    decision = await _gate(policy_gateway_url, hold_s=0, ttl_s=60).decide(ctx, call)

    assert decision.outcome is ApprovalOutcome.UNAVAILABLE
    assert "ApprovalConflictError" in caplog.text
    assert "lifetime" in caplog.text


async def test_a_rejection_stands_for_a_retry_even_though_agent_core_counts_it_finished(
    policy: None, policy_gateway_url: str, policy_approver_url: str
) -> None:
    gate, ctx, call = _gate(policy_gateway_url, hold_s=0), _context(), _call()
    pending = await gate.decide(ctx, call)
    await _approver(policy_approver_url).resolve(
        UUID(pending.approval_id or ""), decision=Decision.REJECT, principal=HUMAN
    )

    for _ in range(2):
        retried = await gate.decide(ctx, call)
        assert retried.outcome is ApprovalOutcome.REJECTED
        assert retried.approval_id == pending.approval_id, "no new request for the approvers"
    assert await _approver(policy_approver_url).list_pending(HUMAN) == []
