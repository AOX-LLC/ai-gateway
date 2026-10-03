"""The lab approver role: there only when asked for, with the approver's powers and no others."""

import argparse
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

import psycopg
import pytest
from aox_agent_core.approvals import Decision
from aox_agent_core.errors import AuditIntegrityError
from psycopg import errors

from ai_gateway.admin.cli import _approver_add
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


async def _setup(
    owner: str, gateway: str, approver: str, auditor: str, lab: str | None = None
) -> None:
    await setup_policy(
        owner,
        PolicyPasswords(
            password_of(gateway), password_of(approver), password_of(auditor), lab_approver=lab
        ),
    )


async def _lab_role_exists(owner: str) -> bool:
    async with await psycopg.AsyncConnection.connect(owner, autocommit=True) as connection:
        cursor = await connection.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = 'policy_lab_approver'"
        )
        return await cursor.fetchone() is not None


async def test_without_a_password_there_is_no_lab_role_and_a_later_setup_removes_one(
    policy: None,
    test_database_url: str,
    policy_gateway_url: str,
    policy_approver_url: str,
    policy_auditor_url: str,
) -> None:
    args = (test_database_url, policy_gateway_url, policy_approver_url, policy_auditor_url)
    assert not await _lab_role_exists(test_database_url), "the default stack has none"

    await _setup(*args, lab=LAB_PASSWORD)
    assert await _lab_role_exists(test_database_url)
    await _setup(*args)

    assert not await _lab_role_exists(test_database_url), "setup without the password drops it"


async def test_the_lab_role_decides_requests_like_the_approver_and_does_nothing_else(
    policy: None,
    test_database_url: str,
    policy_gateway_url: str,
    policy_approver_url: str,
    policy_auditor_url: str,
) -> None:
    await _setup(
        test_database_url,
        policy_gateway_url,
        policy_approver_url,
        policy_auditor_url,
        lab=LAB_PASSWORD,
    )
    await _approver_add(
        test_database_url, argparse.Namespace(id="lab-approver", name="Lab approver", role=None)
    )
    gate, ctx, call = _gate(policy_gateway_url, hold_s=0), _context(), _call()
    pending = await gate.decide(ctx, call)
    lab = Approvals(_lab_url(test_database_url), ROLES)

    decided = await lab.decide(
        UUID(pending.approval_id or ""), "lab-approver", Decision.APPROVE, None
    )

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
    policy_approver_url: str,
    policy_auditor_url: str,
) -> None:
    await _setup(
        test_database_url,
        policy_gateway_url,
        policy_approver_url,
        policy_auditor_url,
        lab=LAB_PASSWORD,
    )
    await _approver_add(
        test_database_url, argparse.Namespace(id="lab-approver", name="Lab approver", role=None)
    )
    gate, ctx, call = _gate(policy_gateway_url, hold_s=0), _context(), _call()
    pending = await gate.decide(ctx, call)
    await Approvals(_lab_url(test_database_url), ROLES).decide(
        UUID(pending.approval_id or ""), "lab-approver", Decision.APPROVE, None
    )
    from aox_agent_core.storage import open_database
    from pydantic import SecretStr

    auditor = audit_log_on(open_database(SecretStr(policy_url(policy_auditor_url))))
    assert (await verify_with_anchors(auditor, [])).seq >= 2, "its decision is a record, and fine"

    await _append(_lab_url(test_database_url), 1)  # a gateway.tool_call written by the lab role

    with pytest.raises(AuditIntegrityError, match="written by role policy_lab_approver"):
        await verify_with_anchors(auditor, [])
