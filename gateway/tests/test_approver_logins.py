"""An approver's own database login: made, rotated and removed, kept as recorded by setup, and what
the audit log and the database say about who decided."""

import argparse
import os
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from aox_agent_core.approvals import Decision
from aox_agent_core.audit import AuditEvent
from aox_agent_core.errors import AuditIntegrityError
from aox_agent_core.storage import open_database
from psycopg import errors
from pydantic import SecretStr

from ai_gateway.admin.cli import (
    AdminError,
    _approver_add,
    _approver_list,
    _approver_logins,
    _approver_remove,
    _approver_rotate,
)
from ai_gateway.approver.cli import Approvals
from ai_gateway.policy import audit_log_on, policy_url
from ai_gateway.policy.anchors import verify_with_anchors
from ai_gateway.policy.approver_logins import (
    ApproverLoginError,
    add_approver,
    remove_approver,
    rotate_approver,
)
from ai_gateway.policy.setup import PolicyPasswords, setup_policy
from ai_gateway.seams.approvals import ApprovalOutcome
from tests.conftest import MakeApprover, login_url, password_of
from tests.test_approval_gate import ROLES, _call, _context, _gate

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


async def _owner(url: str) -> psycopg.AsyncConnection[Any]:
    return await psycopg.AsyncConnection.connect(url, autocommit=True)


async def _rows(url: str, statement: str, *params: object) -> list[tuple[Any, ...]]:
    async with await _owner(url) as connection:
        cursor = await connection.execute(statement, params)
        return await cursor.fetchall()


def _audit(gateway_url: str) -> Any:
    return audit_log_on(open_database(SecretStr(policy_url(gateway_url))))


async def _ask(gateway_url: str, **arguments: Any) -> UUID:
    decision = await _gate(gateway_url).decide(_context(), _call(**arguments))
    assert decision.outcome is ApprovalOutcome.PENDING
    assert decision.approval_id
    return UUID(decision.approval_id)


async def _setup(owner: str, gateway: str, auditor: str, lab: str | None = None) -> None:
    await setup_policy(
        owner, PolicyPasswords(password_of(gateway), password_of(auditor), lab_approver=lab)
    )


# --- what a login is -----------------------------------------------------------------------


async def test_a_login_can_decide_and_has_nothing_else(
    policy: None, test_database_url: str, policy_gateway_url: str
) -> None:
    async with await _owner(test_database_url) as owner:
        added = await add_approver(
            owner, _audit(policy_gateway_url), "aiden", "Aiden", ["approver"]
        )
    assert added.password
    assert added.login == "policy_approver_aiden"

    attributes = await _rows(
        test_database_url,
        "SELECT rolcanlogin, rolsuper, rolcreaterole, rolcreatedb, rolbypassrls, rolconnlimit,"
        " rolvaliduntil FROM pg_roles WHERE rolname = %s",
        added.login,
    )
    login, superuser, createrole, createdb, bypass, limit, valid_until = attributes[0]
    assert (login, superuser, createrole, createdb, bypass, limit) == (
        True,
        False,
        False,
        False,
        False,
        2,
    )
    left = valid_until - datetime.now(UTC)
    assert timedelta(days=89) < left < timedelta(days=91), "a password stops working in 90 days"

    memberships = await _rows(
        test_database_url,
        "SELECT parent.rolname, m.inherit_option, m.set_option FROM pg_auth_members m"
        " JOIN pg_roles parent ON parent.oid = m.roleid"
        " JOIN pg_roles member ON member.oid = m.member"
        " WHERE member.rolname = %s",
        added.login,
    )
    assert memberships == [("policy_approver", True, False)]

    url = login_url(test_database_url, added.login, added.password)
    async with await _owner(url) as connection:
        with pytest.raises(errors.InsufficientPrivilege):
            await connection.execute("SET ROLE policy_approver")
        for statement in (
            "UPDATE policy.approvers SET active = false",
            "INSERT INTO policy.approvers (id, display_name, roles) VALUES ('x', 'X', '{a}')",
            "DELETE FROM policy.approvers",
        ):
            with pytest.raises(errors.InsufficientPrivilege):
                await connection.execute(statement.encode())
        cursor = await connection.execute("SELECT count(*) FROM policy.approvers")
        assert await cursor.fetchone() == (1,), "it can read who the approvers are"


async def test_nobody_can_sign_in_as_the_shared_approver_role(
    policy: None, test_database_url: str, policy_approver_url: str
) -> None:
    assert await _rows(
        test_database_url, "SELECT rolcanlogin FROM pg_roles WHERE rolname = 'policy_approver'"
    ) == [(False,)]
    with pytest.raises(psycopg.OperationalError):
        await psycopg.AsyncConnection.connect(
            login_url(test_database_url, "policy_approver", "anything")
        )


async def test_adding_is_audited_by_the_gateway_role_and_the_password_is_in_no_record(
    policy: None, test_database_url: str, policy_gateway_url: str
) -> None:
    async with await _owner(test_database_url) as owner:
        added = await add_approver(
            owner, _audit(policy_gateway_url), "aiden", "Aiden", ["approver"]
        )

    records = await _rows(
        test_database_url,
        "SELECT action, actor_id, subject_id, db_role, payload FROM policy.agent_core_audit",
    )
    assert [(a, actor, subject, role) for a, actor, subject, role, _ in records] == [
        ("approver.added", "admin", "aiden", "policy_gateway")
    ]
    assert added.password
    assert added.password not in str(records)
    assert "Aiden" not in str(records), "a person's name is not in the audit log"
    async with await _owner(login_url(test_database_url, added.login, added.password)):
        pass  # the password it printed works


async def test_adding_again_updates_the_roles_and_leaves_the_login_and_password_alone(
    policy: None, test_database_url: str, policy_gateway_url: str
) -> None:
    async with await _owner(test_database_url) as owner:
        audit = _audit(policy_gateway_url)
        first = await add_approver(owner, audit, "aiden", "Aiden", ["approver"])
        again = await add_approver(owner, audit, "aiden", "Aiden W.", ["approver", "reader"])

    assert again.password is None
    assert again.login == first.login
    assert await _rows(
        test_database_url, "SELECT display_name, roles FROM policy.approvers WHERE id = 'aiden'"
    ) == [("Aiden W.", ["approver", "reader"])]
    async with await _owner(login_url(test_database_url, first.login, first.password or "")):
        pass


@pytest.mark.parametrize(
    ("approver_id", "why"),
    [
        ("Aiden", "lowercase"),
        ("1aiden", "starting with a letter"),
        ("a" * 41, "at most 40"),
        ("lab-approver", "reserved"),
        ("a b", "lowercase"),
    ],
)
async def test_an_id_that_cannot_be_a_login_is_refused(
    policy: None, test_database_url: str, policy_gateway_url: str, approver_id: str, why: str
) -> None:
    async with await _owner(test_database_url) as owner:
        with pytest.raises(ApproverLoginError, match=why):
            await add_approver(owner, _audit(policy_gateway_url), approver_id, "X", ["approver"])
    assert await _rows(test_database_url, "SELECT count(*) FROM policy.approvers") == [(0,)]


async def test_two_ids_that_make_one_login_name_and_an_unrelated_role_are_not_taken_over(
    policy: None, test_database_url: str, policy_gateway_url: str
) -> None:
    async with await _owner(test_database_url) as owner:
        audit = _audit(policy_gateway_url)
        await add_approver(owner, audit, "ann.lee", "Ann", ["approver"])
        with pytest.raises(ApproverLoginError, match=r"already belongs to approver ann\.lee"):
            await add_approver(owner, audit, "ann-lee", "Another Ann", ["approver"])

        await owner.execute("CREATE ROLE policy_approver_bob NOLOGIN")
        try:
            with pytest.raises(ApproverLoginError, match="already exists"):
                await add_approver(owner, audit, "bob", "Bob", ["approver"])
        finally:
            await owner.execute("DROP ROLE policy_approver_bob")


async def test_nothing_is_kept_when_the_audit_log_cannot_take_the_record(
    policy: None, test_database_url: str, policy_auditor_url: str
) -> None:
    """The auditor role can only read: appending as it fails, as a down audit log would."""
    async with await _owner(test_database_url) as owner:
        with pytest.raises(ApproverLoginError, match="nothing was kept"):
            await add_approver(owner, _audit(policy_auditor_url), "aiden", "Aiden", ["approver"])
        assert await _rows(test_database_url, "SELECT count(*) FROM policy.approvers") == [(0,)]
        assert (
            await _rows(
                test_database_url, "SELECT 1 FROM pg_roles WHERE rolname = 'policy_approver_aiden'"
            )
            == []
        ), "the login that was made is dropped"


# --- rotation and removal ------------------------------------------------------------------


async def test_rotating_replaces_the_password_at_once_and_ends_the_open_sessions(
    policy: None, test_database_url: str, policy_gateway_url: str
) -> None:
    async with await _owner(test_database_url) as owner:
        audit = _audit(policy_gateway_url)
        first = await add_approver(owner, audit, "aiden", "Aiden", ["approver"])
        old_url = login_url(test_database_url, first.login, first.password or "")
        open_session = await _owner(old_url)

        rotated = await rotate_approver(owner, audit, "aiden")

    assert rotated.password
    assert rotated.password != first.password
    with pytest.raises((psycopg.OperationalError, psycopg.errors.AdminShutdown)):
        await open_session.execute("SELECT 1")
    with pytest.raises(psycopg.OperationalError):
        await psycopg.AsyncConnection.connect(old_url)
    async with await _owner(login_url(test_database_url, first.login, rotated.password)):
        pass
    assert [
        r[0]
        for r in await _rows(
            test_database_url, "SELECT action FROM policy.agent_core_audit ORDER BY seq"
        )
    ] == ["approver.added", "approver.rotated"]


async def test_rotating_makes_the_login_again_if_its_role_is_missing(
    policy: None, test_database_url: str, policy_gateway_url: str
) -> None:
    async with await _owner(test_database_url) as owner:
        audit = _audit(policy_gateway_url)
        await add_approver(owner, audit, "aiden", "Aiden", ["approver"])
        await owner.execute("DROP OWNED BY policy_approver_aiden")
        await owner.execute("DROP ROLE policy_approver_aiden")

        rotated = await rotate_approver(owner, audit, "aiden")

    async with await _owner(login_url(test_database_url, rotated.login, rotated.password or "")):
        pass


async def test_removing_takes_the_login_and_the_sessions_and_keeps_the_name_for_good(
    policy: None, test_database_url: str, policy_gateway_url: str
) -> None:
    async with await _owner(test_database_url) as owner:
        audit = _audit(policy_gateway_url)
        added = await add_approver(owner, audit, "aiden", "Aiden", ["approver"])
        url = login_url(test_database_url, added.login, added.password or "")
        open_session = await _owner(url)

        await remove_approver(owner, audit, "aiden")

        with pytest.raises((psycopg.OperationalError, psycopg.errors.AdminShutdown)):
            await open_session.execute("SELECT 1")
        with pytest.raises(psycopg.OperationalError):
            await psycopg.AsyncConnection.connect(url)
        assert (
            await _rows(test_database_url, "SELECT 1 FROM pg_roles WHERE rolname = %s", added.login)
            == []
        )
        row = await _rows(
            test_database_url,
            "SELECT active, removed_at IS NOT NULL, db_role FROM policy.approvers",
        )
        assert row == [(False, True, "policy_approver_aiden")], "the row stays, as history"
        with pytest.raises(ApproverLoginError, match="never reused"):
            await add_approver(owner, audit, "aiden", "Someone Else", ["approver"])
        with pytest.raises(ApproverLoginError, match="no active approver"):
            await rotate_approver(owner, audit, "aiden")


async def test_a_decision_stays_attributed_after_the_approver_is_removed(
    policy: None,
    make_approver: MakeApprover,
    test_database_url: str,
    policy_gateway_url: str,
    policy_auditor_url: str,
) -> None:
    aiden = Approvals(await make_approver("aiden"), ROLES)
    tyler = await make_approver("tyler")
    await aiden.decide(await _ask(policy_gateway_url), Decision.REJECT, "no")

    async with await _owner(test_database_url) as owner:
        await remove_approver(owner, _audit(policy_gateway_url), "aiden")

    auditor = audit_log_on(open_database(SecretStr(policy_url(policy_auditor_url))))
    logins = await _approver_logins(policy_auditor_url)
    assert logins["policy_approver_aiden"] == "aiden", "a removed approver's login stays known"
    await verify_with_anchors(auditor, [], approver_logins=logins)
    assert tyler


async def test_removing_takes_effect_on_what_the_approver_approved_but_nobody_used(
    policy: None,
    make_approver: MakeApprover,
    test_database_url: str,
    policy_gateway_url: str,
) -> None:
    aiden = Approvals(await make_approver("aiden"), ROLES)
    gate, ctx, call = _gate(policy_gateway_url, hold_s=0), _context(), _call()
    pending = await gate.decide(ctx, call)
    await aiden.decide(UUID(pending.approval_id or ""), Decision.APPROVE, None)

    async with await _owner(test_database_url) as owner:
        await remove_approver(owner, _audit(policy_gateway_url), "aiden")

    assert (await gate.decide(ctx, call)).outcome is ApprovalOutcome.UNAVAILABLE
    status = await _rows(test_database_url, "SELECT status FROM policy.agent_core_approvals")
    assert status == [("approved",)], "refused by the gate: not consumed"


async def test_setup_goes_on_when_the_only_decisions_are_by_removed_approvers(
    policy: None,
    make_approver: MakeApprover,
    test_database_url: str,
    policy_gateway_url: str,
    policy_auditor_url: str,
) -> None:
    """agent-core's installer refuses to cancel anything when no current approver has decided
    anything. Everyone who ever decided having left must not stop the stack from starting."""
    aiden = Approvals(await make_approver("aiden"), ROLES)
    pending = await _gate(policy_gateway_url, hold_s=0).decide(_context(), _call())
    await aiden.decide(UUID(pending.approval_id or ""), Decision.APPROVE, None)
    async with await _owner(test_database_url) as owner:
        await remove_approver(owner, _audit(policy_gateway_url), "aiden")

    await _setup(test_database_url, policy_gateway_url, policy_auditor_url)

    assert await _rows(test_database_url, "SELECT status FROM policy.agent_core_approvals") == [
        ("approved",)
    ]


async def test_setup_cancels_what_a_removed_approver_approved_when_others_have_decided(
    policy: None,
    make_approver: MakeApprover,
    test_database_url: str,
    policy_gateway_url: str,
    policy_auditor_url: str,
) -> None:
    aiden = Approvals(await make_approver("aiden"), ROLES)
    tyler = Approvals(await make_approver("tyler"), ROLES)
    first = await _gate(policy_gateway_url, hold_s=0).decide(_context(), _call())
    await aiden.decide(UUID(first.approval_id or ""), Decision.APPROVE, None)
    second = await _ask(policy_gateway_url, ticket_id="TKT-000002", status="closed")
    await tyler.decide(second, Decision.APPROVE, None)
    async with await _owner(test_database_url) as owner:
        await remove_approver(owner, _audit(policy_gateway_url), "aiden")

    await _setup(test_database_url, policy_gateway_url, policy_auditor_url)

    statuses = dict(
        await _rows(test_database_url, "SELECT id, status FROM policy.agent_core_approvals")
    )
    assert statuses[first.approval_id] == "cancelled", "approved by someone who may no longer"
    assert statuses[str(second)] == "approved"


# --- setup keeps the logins as recorded ----------------------------------------------------


async def test_setup_keeps_an_approvers_login_and_undoes_what_was_done_to_it_by_hand(
    policy: None,
    make_approver: MakeApprover,
    test_database_url: str,
    policy_gateway_url: str,
    policy_auditor_url: str,
) -> None:
    url = await make_approver("aiden")
    async with await _owner(test_database_url) as owner:
        await owner.execute("GRANT pg_read_all_data TO policy_approver_aiden")
        await owner.execute("GRANT policy_gateway TO policy_approver_aiden")
        await owner.execute("GRANT policy_approver TO policy_approver_aiden WITH SET TRUE")
        await owner.execute("GRANT SELECT ON policy.agent_core_audit TO policy_approver_aiden")
        await owner.execute("ALTER ROLE policy_approver_aiden SUPERUSER CREATEROLE")

    await _setup(test_database_url, policy_gateway_url, policy_auditor_url)

    memberships = await _rows(
        test_database_url,
        "SELECT parent.rolname, m.inherit_option, m.set_option FROM pg_auth_members m"
        " JOIN pg_roles parent ON parent.oid = m.roleid"
        " JOIN pg_roles member ON member.oid = m.member"
        " WHERE member.rolname = 'policy_approver_aiden' ORDER BY 1",
    )
    assert memberships == [("policy_approver", True, False)]
    assert await _rows(
        test_database_url,
        "SELECT rolsuper, rolcreaterole, has_table_privilege(oid, 'policy.agent_core_audit',"
        " 'SELECT') FROM pg_roles WHERE rolname = 'policy_approver_aiden'",
    ) == [(False, False, True)], "SELECT on the audit log comes from the approver role, not a grant"
    direct = await _rows(
        test_database_url,
        "SELECT count(*) FROM information_schema.role_table_grants"
        " WHERE grantee = 'policy_approver_aiden'",
    )
    assert direct == [(0,)]
    async with await _owner(url):
        pass  # and the login still works, with the password it had


async def test_setup_disables_a_removed_approvers_login_that_was_put_back(
    policy: None,
    make_approver: MakeApprover,
    test_database_url: str,
    policy_gateway_url: str,
    policy_auditor_url: str,
) -> None:
    url = await make_approver("aiden")
    async with await _owner(test_database_url) as owner:
        await owner.execute(
            "UPDATE policy.approvers SET active = false, removed_at = now() WHERE id = 'aiden'"
        )

    await _setup(test_database_url, policy_gateway_url, policy_auditor_url)

    assert await _rows(
        test_database_url,
        "SELECT rolcanlogin FROM pg_roles WHERE rolname = 'policy_approver_aiden'",
    ) == [(False,)]
    with pytest.raises(psycopg.OperationalError):
        await psycopg.AsyncConnection.connect(url)


async def test_setup_says_when_a_recorded_login_has_no_role_and_still_finishes(
    policy: None,
    make_approver: MakeApprover,
    test_database_url: str,
    policy_gateway_url: str,
    policy_auditor_url: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await make_approver("aiden")
    async with await _owner(test_database_url) as owner:
        await owner.execute("DROP OWNED BY policy_approver_aiden")
        await owner.execute("DROP ROLE policy_approver_aiden")

    await _setup(test_database_url, policy_gateway_url, policy_auditor_url)

    assert "approver aiden has no login role" in caplog.text


# --- the table that says who is who --------------------------------------------------------


async def test_only_the_owner_writes_the_approvers_and_none_of_them_is_ever_deleted_or_reused(
    policy: None,
    make_approver: MakeApprover,
    test_database_url: str,
    policy_gateway_url: str,
    policy_auditor_url: str,
) -> None:
    await make_approver("aiden")
    privileges = await _rows(
        test_database_url,
        "SELECT r, p, has_table_privilege(r, 'policy.approvers', p) FROM"
        " unnest(ARRAY['policy_gateway', 'policy_approver', 'policy_auditor',"
        " 'policy_approver_aiden']) r, unnest(ARRAY['INSERT', 'UPDATE', 'DELETE', 'TRUNCATE']) p",
    )
    assert [(r, p) for r, p, allowed in privileges if allowed] == [], "owner only"

    async with await _owner(test_database_url) as owner:
        for statement, why in (
            ("DELETE FROM policy.approvers WHERE id = 'aiden'", "never deleted"),
            ("UPDATE policy.approvers SET db_role = 'policy_approver_other'", "keeps its id"),
            ("UPDATE policy.approvers SET id = 'other'", "keeps its id"),
        ):
            with pytest.raises(errors.IntegrityConstraintViolation, match=why):
                await owner.execute(statement.encode())
        await owner.execute(
            "UPDATE policy.approvers SET active = false, removed_at = now() WHERE id = 'aiden'"
        )
        with pytest.raises(errors.IntegrityConstraintViolation, match="stays removed"):
            await owner.execute("UPDATE policy.approvers SET active = true WHERE id = 'aiden'")
        with pytest.raises(errors.IntegrityConstraintViolation, match="stays removed"):
            await owner.execute("UPDATE policy.approvers SET removed_at = NULL WHERE id = 'aiden'")
        with pytest.raises(errors.UniqueViolation):
            await owner.execute(
                "INSERT INTO policy.approvers (id, display_name, roles, db_role)"
                " VALUES ('twin', 'Twin', '{approver}', 'policy_approver_aiden')"
            )


async def test_the_dashboard_reader_sees_the_upstream_and_who_decided_and_no_more(
    policy: None,
    telemetry: None,
    make_approver: MakeApprover,
    test_database_url: str,
    policy_gateway_url: str,
    policy_auditor_url: str,
    reader_url: str,
) -> None:
    aiden = Approvals(await make_approver("aiden"), ROLES)
    # The reader's role exists now (the telemetry setup made it), so this setup grants it the view.
    await _setup(test_database_url, policy_gateway_url, policy_auditor_url)
    await aiden.decide(await _ask(policy_gateway_url), Decision.APPROVE, None)

    rows = await _rows(
        reader_url,
        "SELECT namespace, resolved_by, resolved_by_name, status FROM policy.dash_approvals",
    )

    assert rows == [("tickets", "human:aiden", "Aiden", "approved")]
    with pytest.raises(errors.InsufficientPrivilege):
        await _rows(reader_url, "SELECT * FROM policy.approvers")
    with pytest.raises(errors.InsufficientPrivilege):
        await _rows(reader_url, "SELECT * FROM policy.approver_logins")


# --- what the audit log says ---------------------------------------------------------------


async def _forge(url: str, action: str, actor: str) -> None:
    log = audit_log_on(open_database(SecretStr(policy_url(url))))
    await log.append(AuditEvent(action=action, actor_id=actor, subject_id=str(uuid4())))


async def test_a_decision_that_claims_to_be_someone_elses_fails_verification(
    policy: None,
    make_approver: MakeApprover,
    policy_gateway_url: str,
    policy_auditor_url: str,
) -> None:
    """Anyone holding a login can append to the audit log. The record carries the login that wrote
    it, set by the database, so a decision written by aiden's login that says tyler decided is
    caught."""
    aiden = await make_approver("aiden")
    await make_approver("tyler")
    auditor = audit_log_on(open_database(SecretStr(policy_url(policy_auditor_url))))
    logins = await _approver_logins(policy_auditor_url)
    await _forge(aiden, "approval.resolved", "human:aiden")
    await verify_with_anchors(auditor, [], approver_logins=logins)

    await _forge(aiden, "approval.resolved", "human:tyler")

    with pytest.raises(AuditIntegrityError, match=r"login of approver aiden.*human:tyler"):
        await verify_with_anchors(auditor, [], approver_logins=logins)


async def test_only_the_gateway_role_may_have_written_a_provisioning_record(
    policy: None,
    make_approver: MakeApprover,
    policy_gateway_url: str,
    policy_auditor_url: str,
) -> None:
    aiden = await make_approver("aiden")  # its own `approver.added` is by the gateway role: fine
    auditor = audit_log_on(open_database(SecretStr(policy_url(policy_auditor_url))))
    logins = await _approver_logins(policy_auditor_url)
    await verify_with_anchors(auditor, [], approver_logins=logins)

    await _forge(aiden, "approver.removed", "admin")  # an approver's login writing one

    with pytest.raises(AuditIntegrityError, match="written by role policy_approver_aiden"):
        await verify_with_anchors(auditor, [], approver_logins=logins)


async def test_a_decision_by_a_login_no_approver_has_is_refused_by_verification(
    policy: None,
    test_database_url: str,
    policy_gateway_url: str,
    policy_auditor_url: str,
) -> None:
    async with await _owner(test_database_url) as owner:
        await owner.execute("CREATE ROLE policy_approver_ghost LOGIN PASSWORD 'ghost-pw'")
        await owner.execute("GRANT policy_approver TO policy_approver_ghost")
    try:
        await _forge(
            login_url(test_database_url, "policy_approver_ghost", "ghost-pw"),
            "approval.resolved",
            "human:ghost",
        )
        auditor = audit_log_on(open_database(SecretStr(policy_url(policy_auditor_url))))

        with pytest.raises(AuditIntegrityError, match="not an approver's login"):
            await verify_with_anchors(
                auditor, [], approver_logins=await _approver_logins(policy_auditor_url)
            )
    finally:
        async with await _owner(test_database_url) as owner:
            await owner.execute("DROP OWNED BY policy_approver_ghost")
            await owner.execute("DROP ROLE policy_approver_ghost")


async def test_the_lab_approver_is_a_recorded_approver_and_the_id_cannot_be_added(
    policy: None,
    test_database_url: str,
    policy_gateway_url: str,
    policy_auditor_url: str,
) -> None:
    await _setup(test_database_url, policy_gateway_url, policy_auditor_url, lab="lab-pw-0123456789")

    assert await _rows(
        test_database_url,
        "SELECT db_role, approver_id FROM policy.approver_logins",
    ) == [("policy_lab_approver", "lab-approver")]
    async with await _owner(test_database_url) as owner:
        with pytest.raises(ApproverLoginError, match="reserved"):
            await add_approver(owner, _audit(policy_gateway_url), "lab-approver", "X", ["approver"])

    await _setup(test_database_url, policy_gateway_url, policy_auditor_url)
    assert await _rows(test_database_url, "SELECT active FROM policy.approvers") == [(False,)]


# --- the admin commands --------------------------------------------------------------------


async def test_the_commands_show_the_password_once_and_need_the_gateway_role_to_audit(
    policy: None,
    test_database_url: str,
    policy_gateway_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("POLICY_GATEWAY_DATABASE_URL", raising=False)
    with pytest.raises(AdminError, match="POLICY_GATEWAY_DATABASE_URL"):
        await _approver_add(
            test_database_url, argparse.Namespace(id="aiden", name="Aiden", role=None)
        )
    monkeypatch.setenv("POLICY_GATEWAY_DATABASE_URL", policy_gateway_url)

    await _approver_add(test_database_url, argparse.Namespace(id="aiden", name="Aiden", role=None))
    added = capsys.readouterr().out
    await _approver_list(test_database_url, argparse.Namespace())
    listed = capsys.readouterr().out
    await _approver_rotate(test_database_url, argparse.Namespace(id="aiden"))
    rotated = capsys.readouterr().out
    await _approver_remove(test_database_url, argparse.Namespace(id="aiden"))
    removed = capsys.readouterr().out

    first = next(line for line in added.splitlines() if line.startswith("password  "))
    second = next(line for line in rotated.splitlines() if line.startswith("password  "))
    assert "login     policy_approver_aiden" in added
    assert first != second
    assert first.split()[1] not in listed + removed, "shown once, never listed"
    assert "active  policy_approver_aiden" in listed
    assert "removed" in removed
    assert os.environ.get("APPROVER_PASSWORD") is None
