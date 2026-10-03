"""A schema left by agent-core v0.1.0a2 is upgraded in place by policy-setup, losing nothing.

tests/fixtures/policy_a2.sql is a dump of a real a2 install with audit records and approval
requests in each state (see make_policy_a2_fixture.py); policy_a2_anchor.jsonl is the audit head
taken from it before any upgrade."""

import shutil
from pathlib import Path
from uuid import UUID

import psycopg
import pytest
from aox_agent_core.approvals import Decision, Principal, PrincipalKind, RoleApproverPolicy
from aox_agent_core.errors import AuditIntegrityError
from aox_agent_core.storage import open_database
from pydantic import SecretStr

from ai_gateway.policy import approval_queue_on, audit_log_on, policy_url
from ai_gateway.policy.anchors import read_anchors, verify_with_anchors
from ai_gateway.policy.setup import PolicyPasswords, setup_policy
from tests.conftest import password_of
from tests.test_audit_anchors import _append

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

FIXTURES = Path(__file__).parent / "fixtures"
PENDING = "7eff70a1-3f92-48c9-a957-1f60b4ec6a55"
STATES = {
    PENDING: "pending",
    "79226c46-fe0d-411f-bf67-dceaac8fb705": "approved",
    "240c4b8d-0fcb-479b-a3dc-05e98ab98887": "rejected",
    "a7631a31-40a0-4798-9213-d99fd8c3a9e4": "consumed",
}


async def _load_a2(owner_url: str) -> None:
    dump = "\n".join(
        line
        for line in (FIXTURES / "policy_a2.sql").read_text().splitlines()
        if not line.startswith("\\")  # psql meta-commands
    )
    async with await psycopg.AsyncConnection.connect(owner_url, autocommit=True) as connection:
        await connection.execute("DROP SCHEMA IF EXISTS policy CASCADE")
        await connection.execute(dump.encode())


async def _upgrade(owner: str, gateway: str, approver: str, auditor: str) -> None:
    await setup_policy(
        owner,
        PolicyPasswords(password_of(gateway), password_of(approver), password_of(auditor)),
    )


async def _rows(owner_url: str, statement: str) -> list[tuple[object, ...]]:
    async with await psycopg.AsyncConnection.connect(
        policy_url(owner_url), autocommit=True
    ) as connection:
        cursor = await connection.execute(statement.encode())
        return await cursor.fetchall()


async def test_an_a2_schema_is_upgraded_in_place_and_the_chain_and_an_old_anchor_still_verify(
    policy: None,
    test_database_url: str,
    policy_gateway_url: str,
    policy_approver_url: str,
    policy_auditor_url: str,
    tmp_path: Path,
) -> None:
    await _load_a2(test_database_url)
    audit_before = await _rows(
        test_database_url, "SELECT seq, event_id, record_hash FROM agent_core_audit ORDER BY seq"
    )
    approvals_before = await _rows(
        test_database_url, "SELECT id, status, payload_sha256 FROM agent_core_approvals ORDER BY id"
    )
    anchors = tmp_path / "anchors.jsonl"
    shutil.copy(FIXTURES / "policy_a2_anchor.jsonl", anchors)
    anchors.chmod(0o600)

    await _upgrade(test_database_url, policy_gateway_url, policy_approver_url, policy_auditor_url)

    assert (
        await _rows(
            test_database_url,
            "SELECT seq, event_id, record_hash FROM agent_core_audit ORDER BY seq",
        )
        == audit_before
    ), "no audit record changed"
    assert (
        await _rows(
            test_database_url,
            "SELECT id, status, payload_sha256 FROM agent_core_approvals ORDER BY id",
        )
        == approvals_before
    ), "no request changed"
    assert {row[0]: row[1] for row in approvals_before} == STATES
    auditor = audit_log_on(open_database(SecretStr(policy_url(policy_auditor_url))))
    head = await verify_with_anchors(auditor, read_anchors(anchors))
    assert head.seq == 13, (
        "the chain from a2 verifies, and so does the anchor taken before the upgrade"
    )

    await _append(
        policy_gateway_url, 3
    )  # the upgraded schema takes new records onto the same chain
    grown = await verify_with_anchors(auditor, read_anchors(anchors))
    assert grown.seq == 16


async def test_the_upgrade_is_repeatable_and_leaves_the_a3_guard_in_place(
    policy: None,
    test_database_url: str,
    policy_gateway_url: str,
    policy_approver_url: str,
    policy_auditor_url: str,
) -> None:
    await _load_a2(test_database_url)
    for _ in range(2):
        await _upgrade(
            test_database_url, policy_gateway_url, policy_approver_url, policy_auditor_url
        )

    guards = await _rows(
        test_database_url,
        "SELECT tgname FROM pg_trigger WHERE tgrelid = 'policy.agent_core_approvals'::regclass"
        " AND NOT tgisinternal ORDER BY tgname",
    )
    assert guards == [("agent_core_approvals_guard",), ("agent_core_approvals_no_truncate",)]
    columns = await _rows(
        test_database_url,
        "SELECT column_name FROM information_schema.columns WHERE table_schema = 'policy'"
        " AND table_name = 'agent_core_audit' AND column_name = 'db_role'",
    )
    assert columns == [("db_role",)], "audit schema 3"


async def test_an_approval_that_plain_sql_made_under_a2_is_cancelled_by_the_upgrade(
    policy: None,
    test_database_url: str,
    policy_gateway_url: str,
    policy_approver_url: str,
    policy_auditor_url: str,
) -> None:
    """a2 let the app role approve its own request with SQL. Such an approval has no
    approval.resolved event from an approver, and must not survive the upgrade as usable."""
    await _load_a2(test_database_url)
    async with await psycopg.AsyncConnection.connect(
        policy_url(test_database_url), autocommit=True
    ) as connection:
        await connection.execute(
            b"UPDATE agent_core_approvals SET status = 'approved', decision = 'approve',"
            b" resolved_by = 'human:nobody', resolved_at = created_at WHERE id = %s",
            (PENDING,),
        )

    await _upgrade(test_database_url, policy_gateway_url, policy_approver_url, policy_auditor_url)

    rows = await _rows(test_database_url, "SELECT id, status FROM agent_core_approvals")
    assert {str(row[0]): row[1] for row in rows}[PENDING] == "cancelled"


async def test_a_wrong_anchor_still_fails_against_the_upgraded_chain(
    policy: None,
    test_database_url: str,
    policy_gateway_url: str,
    policy_approver_url: str,
    policy_auditor_url: str,
    tmp_path: Path,
) -> None:
    """The control for the test above: it passes because the anchor matches, not because nothing
    is checked."""
    await _load_a2(test_database_url)
    await _upgrade(test_database_url, policy_gateway_url, policy_approver_url, policy_auditor_url)
    anchors = tmp_path / "anchors.jsonl"
    anchors.write_text(
        (FIXTURES / "policy_a2_anchor.jsonl").read_text().replace("89d74057", "00000000")
    )
    anchors.chmod(0o600)
    auditor = audit_log_on(open_database(SecretStr(policy_url(policy_auditor_url))))

    with pytest.raises(AuditIntegrityError):
        await verify_with_anchors(auditor, read_anchors(anchors))


@pytest.mark.parametrize(
    "held",
    [
        "SELECT, INSERT, UPDATE",  # what 3a gave: the table-level UPDATE the guard used to limit
        "SELECT",  # what an incomplete first repair left: something, but not the layout
    ],
)
async def test_roles_that_held_other_grants_get_exactly_the_layout_and_can_still_work(
    held: str,
    policy: None,
    test_database_url: str,
    policy_gateway_url: str,
    policy_approver_url: str,
    policy_auditor_url: str,
) -> None:
    """3a gave the gateway and the approver table-level UPDATE. The installer grants a role its
    layout only where it holds nothing, so those grants must go and the layout come in after them,
    or the approver can no longer decide anything."""
    await _load_a2(test_database_url)
    async with await psycopg.AsyncConnection.connect(
        test_database_url, autocommit=True
    ) as connection:
        for role in ("policy_gateway", "policy_approver"):
            await connection.execute(f"GRANT USAGE ON SCHEMA policy TO {role}".encode())
            await connection.execute(
                f"GRANT {held} ON policy.agent_core_approvals TO {role}".encode()
            )
            await connection.execute(
                f"GRANT SELECT, INSERT ON policy.agent_core_audit TO {role}".encode()
            )

    await _upgrade(test_database_url, policy_gateway_url, policy_approver_url, policy_auditor_url)

    rows = await _rows(
        test_database_url,
        "SELECT r.rolname, has_table_privilege(r.oid, 'policy.agent_core_approvals', 'UPDATE'),"
        " has_column_privilege(r.oid, 'policy.agent_core_approvals', 'decision', 'UPDATE'),"
        " has_column_privilege(r.oid, 'policy.agent_core_approvals', 'status', 'UPDATE'),"
        " has_column_privilege(r.oid, 'policy.agent_core_approvals', 'expires_at', 'UPDATE')"
        " FROM pg_roles r WHERE r.rolname IN ('policy_gateway', 'policy_approver') ORDER BY 1",
    )
    assert rows == [
        ("policy_approver", False, True, True, False),
        ("policy_gateway", False, False, True, False),
    ]
    approver = approval_queue_on(
        open_database(SecretStr(policy_url(policy_approver_url))),
        policy=RoleApproverPolicy(roles_by_action={"tickets__change_status": "approver"}),
    )
    request = await approver.resolve(
        UUID(PENDING),
        decision=Decision.APPROVE,
        principal=Principal(
            id="human:fixture", kind=PrincipalKind.HUMAN, roles=frozenset({"approver"})
        ),
    )
    assert request.status.value == "approved"
