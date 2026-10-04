"""The lab approver role: there only when asked for, with the approver's powers and no others."""

import argparse
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

import psycopg
import pytest
from aox_agent_core.approvals import Decision
from aox_agent_core.errors import AuditIntegrityError
from psycopg import errors

from ai_gateway.approver.cli import Approvals
from ai_gateway.policy import audit_log_on, policy_url
from ai_gateway.policy.anchors import verify_with_anchors
from ai_gateway.policy.setup import PolicyPasswords, setup_policy
from tests.conftest import password_of
from tests.test_approval_gate import ROLES, _call, _context, _gate
from tests.test_audit_anchors import _append

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

LAB_PASSWORD = "lab-only-password-for-tests"  # noqa: S105 - a test value


def _lab_url(owner_url: str) -> str:
    parts = urlsplit(owner_url)
    netloc = f"policy_lab_approver:{LAB_PASSWORD}@{parts.hostname}:{parts.port}"
    return urlunsplit(parts._replace(netloc=netloc))


async def _setup(owner: str, gateway: str, auditor: str, lab: str | None = None) -> None:
    await setup_policy(
        owner, PolicyPasswords(password_of(gateway), password_of(auditor), lab_approver=lab)
    )


async def _lab_role_exists(owner: str) -> bool:
    async with await psycopg.AsyncConnection.connect(owner, autocommit=True) as connection:
        cursor = await connection.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = 'policy_lab_approver'"
        )
        return await cursor.fetchone() is not None


async def test_without_a_password_the_lab_role_cannot_log_in_and_decides_nothing(
    policy: None,
    test_database_url: str,
    policy_gateway_url: str,
    policy_auditor_url: str,
) -> None:
    args = (test_database_url, policy_gateway_url, policy_auditor_url)
    assert not await _lab_role_exists(test_database_url), "the default stack has none"

    await _setup(*args, lab=LAB_PASSWORD)
    assert await _lab_role_exists(test_database_url)
    await _setup(*args)

    # The role is kept, not dropped: agent-core maps a login to its principal for good, by the
    # role's OID, so one made again could never decide. Without the password it is shut.
    assert await _lab_role_exists(test_database_url)
    async with await psycopg.AsyncConnection.connect(
        test_database_url, autocommit=True
    ) as connection:
        cursor = await connection.execute(
            "SELECT rolcanlogin, pg_has_role('policy_lab_approver', 'policy_approver', 'MEMBER')"
            " FROM pg_roles WHERE rolname = 'policy_lab_approver'"
        )
        assert await cursor.fetchone() == (False, False), "no login, and no powers"
    with pytest.raises(psycopg.OperationalError):
        await psycopg.AsyncConnection.connect(_lab_url(test_database_url))


async def test_the_lab_role_decides_requests_like_the_approver_and_does_nothing_else(
    policy: None,
    test_database_url: str,
    policy_gateway_url: str,
    policy_auditor_url: str,
) -> None:
    await _setup(test_database_url, policy_gateway_url, policy_auditor_url, lab=LAB_PASSWORD)
    gate, ctx, call = _gate(policy_gateway_url, hold_s=0), _context(), _call()
    pending = await gate.decide(ctx, call)
    lab = Approvals(_lab_url(test_database_url), ROLES)

    decided = await lab.decide(UUID(pending.approval_id or ""), Decision.APPROVE, None)

    assert decided.status.value == "approved"
    async with await psycopg.AsyncConnection.connect(
        policy_url(_lab_url(test_database_url)), autocommit=True
    ) as connection:
        for statement in (
            "INSERT INTO agent_core_approvals (id) VALUES ('x')",  # not the requester's side
            "DROP TABLE agent_core_audit",
            "CREATE TABLE policy.planted (x int)",
        ):
            try:
                await connection.execute(statement.encode())
            except (errors.InsufficientPrivilege, errors.RaiseException):
                continue
            raise AssertionError(f"the lab role could run: {statement}")


async def test_a_decision_by_the_lab_role_passes_the_audit_check_and_a_forgery_by_it_does_not(
    policy: None,
    test_database_url: str,
    policy_gateway_url: str,
    policy_auditor_url: str,
) -> None:
    await _setup(test_database_url, policy_gateway_url, policy_auditor_url, lab=LAB_PASSWORD)
    gate, ctx, call = _gate(policy_gateway_url, hold_s=0), _context(), _call()
    pending = await gate.decide(ctx, call)
    await Approvals(_lab_url(test_database_url), ROLES).decide(
        UUID(pending.approval_id or ""), Decision.APPROVE, None
    )
    from aox_agent_core.storage import open_database
    from pydantic import SecretStr

    auditor = audit_log_on(open_database(SecretStr(policy_url(policy_auditor_url))))
    assert (await verify_with_anchors(auditor, [])).seq >= 2, "its decision is a record, and fine"

    await _append(_lab_url(test_database_url), 1)  # a gateway.tool_call written by the lab role

    with pytest.raises(AuditIntegrityError, match="written by role policy_lab_approver"):
        await verify_with_anchors(auditor, [])


async def test_a_grant_or_membership_made_by_hand_does_not_survive_a_lab_mode_setup(
    policy: None,
    test_database_url: str,
    policy_gateway_url: str,
    policy_auditor_url: str,
) -> None:
    args = (test_database_url, policy_gateway_url, policy_auditor_url)
    await _setup(*args, lab=LAB_PASSWORD)
    async with await psycopg.AsyncConnection.connect(
        test_database_url, autocommit=True
    ) as connection:
        await connection.execute("DROP ROLE IF EXISTS lab_bystander")
        await connection.execute("CREATE ROLE lab_bystander NOLOGIN")
        try:
            await connection.execute("GRANT policy_lab_approver TO lab_bystander")  # inherits it
            await connection.execute("GRANT CREATE ON SCHEMA policy TO policy_lab_approver")
            await connection.execute("GRANT SELECT ON policy.approvers TO policy_lab_approver")

            await _setup(*args, lab=LAB_PASSWORD)

            cursor = await connection.execute(
                "SELECT (SELECT count(*) FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.member"
                " WHERE r.rolname = 'lab_bystander'),"
                " has_schema_privilege('policy_lab_approver', 'policy', 'CREATE'),"
                " pg_has_role('policy_lab_approver', 'policy_approver', 'MEMBER')"
            )
            assert await cursor.fetchone() == (0, False, True), "rebuilt: only the membership"
        finally:
            await connection.execute("DROP ROLE IF EXISTS lab_bystander")


async def test_audit_verify_says_when_a_stack_has_been_used_as_a_lab(
    policy: None,
    test_database_url: str,
    policy_gateway_url: str,
    policy_auditor_url: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from ai_gateway.admin.cli import _audit_verify

    await _setup(test_database_url, policy_gateway_url, policy_auditor_url, lab=LAB_PASSWORD)
    pending = await _gate(policy_gateway_url, hold_s=0).decide(_context(), _call())
    await Approvals(_lab_url(test_database_url), ROLES).decide(
        UUID(pending.approval_id or ""), Decision.APPROVE, None
    )

    await _audit_verify(policy_auditor_url, argparse.Namespace(anchors=None))

    output = capsys.readouterr().out
    assert "1 approval decision(s) were made by the lab approver role" in output
