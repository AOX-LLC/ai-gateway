"""The arguments a person approves live with the request, and `approvals-purge` removes them."""

import json
from datetime import timedelta
from typing import Any
from uuid import UUID

import psycopg
import pytest
from aox_agent_core.approvals import Decision, approval_payload_hash
from aox_agent_core.audit import AuditEvent
from aox_agent_core.errors import AuditIntegrityError, ConfigError
from aox_agent_core.storage import open_database
from psycopg import errors
from pydantic import SecretStr

from ai_gateway.policy import (
    PURGER_ROLE,
    approval_queue_on,
    audit_log_on,
    policy_url,
)
from ai_gateway.policy.anchors import verify_with_anchors
from ai_gateway.policy.purge import purge_payloads
from ai_gateway.policy.setup import PolicyPasswords, setup_policy
from ai_gateway.seams.approvals import ApprovalOutcome
from tests.conftest import AIDEN, MakeApprover, login_url, password_of
from tests.test_approval_gate import HUMAN, MARKER, _approver, _call, _context, _gate

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

PURGER_PASSWORD = "ci-only-purger-password"  # noqa: S105 - a test database's password
RETENTION = timedelta(days=7)


async def _setup_with_purger(owner: str, gateway: str, auditor: str) -> None:
    await setup_policy(
        owner,
        PolicyPasswords(password_of(gateway), password_of(auditor), payload_purger=PURGER_PASSWORD),
    )


async def _rows(url: str, statement: str, *params: object) -> list[tuple[Any, ...]]:
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as connection:
        cursor = await connection.execute(statement, params)
        return await cursor.fetchall()


async def _finish_long_ago(owner_url: str, request_id: str) -> None:
    """Age a request's finish past the retention. The guard stamps finish times itself, so a test
    has to turn the guard off to say "eight days ago" (as the owner, the way an operator could)."""
    async with await psycopg.AsyncConnection.connect(
        policy_url(owner_url), autocommit=True
    ) as owner:
        await owner.execute("ALTER TABLE agent_core_approvals DISABLE TRIGGER USER")
        try:
            await owner.execute(
                "UPDATE agent_core_approvals SET consumed_at = to_char(now() AT TIME ZONE 'UTC'"
                " - interval '8 days', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"') WHERE id = %s",
                (request_id,),
            )
        finally:
            await owner.execute("ALTER TABLE agent_core_approvals ENABLE TRIGGER USER")


async def test_the_arguments_are_stored_with_the_request_and_hash_to_it(
    policy: None, policy_gateway_url: str, policy_approver_url: str, test_database_url: str
) -> None:
    gate, ctx, call = _gate(policy_gateway_url), _context(), _call()
    pending = await gate.decide(ctx, call)

    stored = await _approver(policy_approver_url).get(UUID(pending.approval_id or ""))

    assert stored.payload == {"arguments": call.arguments, "upstream": call.upstream_identity}
    assert approval_payload_hash(call.exposed_name, stored.payload) == stored.payload_sha256
    assert await _rows(
        test_database_url, "SELECT to_regclass('policy.approval_arguments') IS NULL"
    ) == [(True,)], "the gateway's own table of arguments is gone"


async def test_the_audit_log_and_the_dashboards_views_never_hold_the_arguments(
    policy: None, policy_gateway_url: str, policy_auditor_url: str
) -> None:
    await _gate(policy_gateway_url).decide(_context(), _call())

    rows = await _rows(policy_url(policy_auditor_url), "SELECT payload FROM agent_core_audit")
    assert rows
    assert MARKER not in json.dumps(rows)
    async with await psycopg.AsyncConnection.connect(policy_url(policy_auditor_url)) as auditor:
        with pytest.raises(errors.InsufficientPrivilege):
            await auditor.execute("SELECT payload_json FROM agent_core_approvals")


async def test_a_finished_request_keeps_its_arguments_for_a_week_and_is_then_purged(
    policy: None,
    make_approver: MakeApprover,
    test_database_url: str,
    policy_gateway_url: str,
    policy_auditor_url: str,
) -> None:
    await _setup_with_purger(test_database_url, policy_gateway_url, policy_auditor_url)
    approver = _approver(await make_approver(AIDEN))
    gate, ctx = _gate(policy_gateway_url, hold_s=0), _context()
    old, fresh, open_ = _call(n=1), _call(n=2), _call(n=3)
    ids = {}
    for name, call in (("old", old), ("fresh", fresh)):
        pending = await gate.decide(ctx, call)
        ids[name] = pending.approval_id
        await approver.resolve(
            UUID(pending.approval_id or ""), decision=Decision.APPROVE, principal=HUMAN
        )
        assert (await gate.decide(ctx, call)).outcome is ApprovalOutcome.APPROVED  # consumed
    ids["open"] = (await gate.decide(ctx, open_)).approval_id
    await _finish_long_ago(test_database_url, ids["old"] or "")

    purger = approval_queue_on(
        open_database(
            SecretStr(
                policy_url(login_url(test_database_url, PURGER_ROLE, PURGER_PASSWORD)),
            ),
            max_connections=1,
        )
    )
    assert await purge_payloads(purger, RETENTION) == 1

    stored = {name: await approver.get(UUID(request_id or "")) for name, request_id in ids.items()}
    assert stored["old"].payload is None
    assert stored["old"].payload_purged_at is not None, "purged is not the same as never stored"
    assert stored["old"].payload_sha256, "the hash stays"
    assert stored["fresh"].payload is not None, "finished less than a week ago"
    assert stored["open"].payload is not None, "still open: never purged"
    records = await _rows(
        policy_url(policy_auditor_url),
        "SELECT db_role, subject_id FROM agent_core_audit WHERE action = 'approval.payload_purged'",
    )
    assert records == [(PURGER_ROLE, ids["old"])], "one record, written by the purge's own login"
    assert MARKER not in json.dumps(
        await _rows(policy_url(policy_auditor_url), "SELECT payload FROM agent_core_audit")
    )


async def test_only_the_purge_login_may_purge_and_it_cannot_make_a_decision_anyone_uses(
    policy: None,
    test_database_url: str,
    policy_gateway_url: str,
    policy_auditor_url: str,
) -> None:
    await _setup_with_purger(test_database_url, policy_gateway_url, policy_auditor_url)
    gate, ctx, call = _gate(policy_gateway_url, hold_s=0), _context(), _call()
    pending = await gate.decide(ctx, call)
    request_id = UUID(pending.approval_id or "")

    # The gateway (the requester side) is refused a purge by agent-core and by the guard.
    gateway_queue = approval_queue_on(open_database(SecretStr(policy_url(policy_gateway_url))))
    with pytest.raises(ConfigError):
        await purge_payloads(gateway_queue, RETENTION)

    # The purge login is an approver-role member that policy.approvers does not list. It could
    # write an approval in plain SQL under the principal it is mapped to; nothing uses it.
    purger_url = login_url(test_database_url, PURGER_ROLE, PURGER_PASSWORD)
    async with await psycopg.AsyncConnection.connect(
        policy_url(purger_url), autocommit=True
    ) as login:
        with pytest.raises(errors.RaiseException, match="mapped to the deciding login"):
            await login.execute(
                b"UPDATE agent_core_approvals SET status = 'approved', decision = 'approve',"
                b" resolved_by = 'human:anyone', resolved_at = to_char(now() AT TIME ZONE 'UTC',"
                b' \'YYYY-MM-DD"T"HH24:MI:SS.US"Z"\') WHERE id = %s',
                (str(request_id),),
            )
    assert (await gate.decide(ctx, call)).outcome is ApprovalOutcome.PENDING


async def test_verification_accepts_a_purge_by_its_login_and_refuses_one_by_anyone_else(
    policy: None,
    make_approver: MakeApprover,
    test_database_url: str,
    policy_gateway_url: str,
    policy_auditor_url: str,
) -> None:
    await _setup_with_purger(test_database_url, policy_gateway_url, policy_auditor_url)
    approver_url = await make_approver(AIDEN)
    auditor = audit_log_on(open_database(SecretStr(policy_url(policy_auditor_url))))
    await _append(
        login_url(test_database_url, PURGER_ROLE, PURGER_PASSWORD), "approval.payload_purged"
    )
    await verify_with_anchors(auditor, [])

    await _append(approver_url, "approval.payload_purged")  # an approver's login, not the purge's

    with pytest.raises(AuditIntegrityError, match=f"written by role .*, not {PURGER_ROLE}"):
        await verify_with_anchors(auditor, [])


async def _append(url: str, action: str) -> None:
    log = audit_log_on(open_database(SecretStr(policy_url(url)), max_connections=1))
    await log.append(AuditEvent(action=action, actor_id="service:payload-purger", payload={}))
    await log.database.aclose()


async def test_a_decision_the_purge_login_writes_under_its_own_principal_is_never_used(
    policy: None,
    test_database_url: str,
    policy_gateway_url: str,
    policy_auditor_url: str,
) -> None:
    """The guard lets the purge login record its own mapped principal, as it would any login. The
    gateway then refuses the approval (that principal is no approver in `policy.approvers`), and
    verification refuses the decision's record."""
    await _setup_with_purger(test_database_url, policy_gateway_url, policy_auditor_url)
    gate, ctx, call = _gate(policy_gateway_url, hold_s=0), _context(), _call()
    request_id = (await gate.decide(ctx, call)).approval_id or ""
    purger_url = login_url(test_database_url, PURGER_ROLE, PURGER_PASSWORD)
    async with await psycopg.AsyncConnection.connect(
        policy_url(purger_url), autocommit=True
    ) as login:
        await login.execute(
            b"UPDATE agent_core_approvals SET status = 'approved', decision = 'approve',"
            b" resolved_by = 'service:payload-purger', resolved_at = to_char(now() AT TIME ZONE"
            b" 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"') WHERE id = %s",
            (request_id,),
        )
    await _append_as(purger_url, "approval.resolved", request_id)

    assert (await gate.decide(ctx, call)).outcome is ApprovalOutcome.UNAVAILABLE
    auditor = audit_log_on(open_database(SecretStr(policy_url(policy_auditor_url))))
    with pytest.raises(AuditIntegrityError, match="not an approver's login"):
        await verify_with_anchors(auditor, [])


async def _append_as(url: str, action: str, subject: str) -> None:
    log = audit_log_on(open_database(SecretStr(policy_url(url)), max_connections=1))
    await log.append(
        AuditEvent(
            action=action,
            actor_id="service:payload-purger",
            subject_id=subject,
            payload={"decision": "approve"},
        )
    )
    await log.database.aclose()


async def test_the_purge_login_is_hardened_like_an_approvers_and_stays_so(
    policy: None, test_database_url: str, policy_gateway_url: str, policy_auditor_url: str
) -> None:
    """The same login hardening a person's gets, with one connection: an expiring password, no
    attribute that bypasses a check, exactly the approver role's membership (inheriting, no `SET`,
    no `ADMIN`) and no table privilege of its own. A hand-made change does not survive a setup."""
    from tests.test_lab_approver import _ONLY_THE_APPROVER_ROLE, _SERVICE_LOGIN_FACTS

    expected = (True, False, False, False, False, False, 1, True, _ONLY_THE_APPROVER_ROLE, 0)
    await _setup_with_purger(test_database_url, policy_gateway_url, policy_auditor_url)
    async with await psycopg.AsyncConnection.connect(
        test_database_url, autocommit=True
    ) as connection:
        cursor = await connection.execute(_SERVICE_LOGIN_FACTS, (PURGER_ROLE,))
        assert await cursor.fetchone() == expected

        await connection.execute(
            f"ALTER ROLE {PURGER_ROLE} CREATEROLE REPLICATION CONNECTION LIMIT 50"
            " VALID UNTIL 'infinity'"
        )
        await connection.execute(f"GRANT SELECT ON policy.approvers TO {PURGER_ROLE}")
        await connection.execute(f"GRANT pg_read_all_data TO {PURGER_ROLE}")

        await _setup_with_purger(test_database_url, policy_gateway_url, policy_auditor_url)

        cursor = await connection.execute(_SERVICE_LOGIN_FACTS, (PURGER_ROLE,))
        assert await cursor.fetchone() == expected, "put back by the next setup"
