"""An approver's own database login: made, rotated and removed, kept as recorded by setup, and what
the audit log and the database say about who decided."""

import argparse
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from aox_agent_core.approvals import Decision, Principal, PrincipalKind, RoleApproverPolicy
from aox_agent_core.audit import AuditEvent
from aox_agent_core.errors import AuditIntegrityError, ConfigError, NotAuthorizedToResolveError
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
    _audit_anchor,
    _audit_verify,
)
from ai_gateway.approver.cli import Approvals
from ai_gateway.policy import (
    APPROVER_ID_PATTERN,
    approval_queue_on,
    audit_log_on,
    new_approver_id,
    policy_url,
)
from ai_gateway.policy.anchors import verify_with_anchors
from ai_gateway.policy.approver_logins import (
    ApproverLoginError,
    add_approver,
    remove_approver,
    rotate_approver,
)
from ai_gateway.policy.setup import PolicyPasswords, setup_policy
from ai_gateway.seams.approvals import ApprovalOutcome
from tests.conftest import AIDEN, INTERN, TYLER, MakeApprover, MakeClient, login_url, password_of
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
        added = await add_approver(owner, _audit(policy_gateway_url), AIDEN, "Aiden", ["approver"])
    assert added.password
    assert added.login == f"policy_approver_{AIDEN}"

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
        added = await add_approver(owner, _audit(policy_gateway_url), AIDEN, "Aiden", ["approver"])

    records = await _rows(
        test_database_url,
        "SELECT action, actor_id, subject_id, db_role, payload FROM policy.agent_core_audit",
    )
    assert [(a, actor, subject, role) for a, actor, subject, role, _ in records] == [
        ("approver.added", "admin", AIDEN, "policy_gateway")
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
        first = await add_approver(owner, audit, AIDEN, "Aiden", ["approver"])
        again = await add_approver(owner, audit, AIDEN, "Aiden W.", ["approver", "reader"])

    assert again.password is None
    assert again.login == first.login
    assert await _rows(
        test_database_url, f"SELECT display_name, roles FROM policy.approvers WHERE id = '{AIDEN}'"
    ) == [("Aiden W.", ["approver", "reader"])]
    async with await _owner(login_url(test_database_url, first.login, first.password or "")):
        pass


@pytest.mark.parametrize(
    ("approver_id", "why"),
    [
        ("Aiden", "opaque"),
        ("aiden", "opaque"),
        ("tyler.weatherwax", "opaque"),
        ("appr_" + "a" * 11, "opaque"),
        ("appr_aaaaaaaaaA", "opaque"),
        ("lab-approver", "reserved"),
        ("a b", "opaque"),
    ],
)
async def test_an_id_that_cannot_be_a_login_is_refused(
    policy: None, test_database_url: str, policy_gateway_url: str, approver_id: str, why: str
) -> None:
    async with await _owner(test_database_url) as owner:
        with pytest.raises(ApproverLoginError, match=why):
            await add_approver(owner, _audit(policy_gateway_url), approver_id, "X", ["approver"])
    assert await _rows(test_database_url, "SELECT count(*) FROM policy.approvers") == [(0,)]


async def test_a_role_that_already_has_the_login_name_is_not_taken_over(
    policy: None, test_database_url: str, policy_gateway_url: str
) -> None:
    async with await _owner(test_database_url) as owner:
        await owner.execute("CREATE ROLE policy_approver_appr_aaaaaaaaaa NOLOGIN")
        try:
            with pytest.raises(ApproverLoginError, match="already exists"):
                await add_approver(
                    owner, _audit(policy_gateway_url), "appr_aaaaaaaaaa", "Bob", ["approver"]
                )
        finally:
            await owner.execute("DROP ROLE policy_approver_appr_aaaaaaaaaa")


async def test_an_approver_id_is_opaque_and_the_name_is_kept_in_the_approvers_table_only(
    policy: None, test_database_url: str, policy_gateway_url: str
) -> None:
    """The login name, the principal and the audit record are written for good (the audit log and
    agent-core's mapping are append-only), so none of them may carry a person's name."""
    async with await _owner(test_database_url) as owner:
        added = await add_approver(
            owner, _audit(policy_gateway_url), new_approver_id(), "Tyler Weatherwax", ["approver"]
        )

    assert re.fullmatch(APPROVER_ID_PATTERN, added.approver_id)
    assert added.login == f"policy_approver_{added.approver_id}"
    assert await _rows(test_database_url, "SELECT id, display_name FROM policy.approvers") == [
        (added.approver_id, "Tyler Weatherwax")
    ]
    where_a_name_would_stick = await _rows(
        test_database_url,
        "SELECT login, principal FROM policy.agent_core_approver_logins"
        " UNION ALL SELECT payload, actor_id FROM policy.agent_core_audit"
        " UNION ALL SELECT rolname, '' FROM pg_roles WHERE rolname ~ '^policy_approver_'",
    )
    assert "tyler" not in json.dumps(where_a_name_would_stick).lower()
    assert "weatherwax" not in json.dumps(where_a_name_would_stick).lower()


async def test_the_generated_ids_are_opaque_and_do_not_repeat() -> None:
    ids = {new_approver_id() for _ in range(200)}

    assert len(ids) == 200
    assert all(re.fullmatch(APPROVER_ID_PATTERN, approver_id) for approver_id in ids)


async def test_nothing_is_kept_when_the_audit_log_cannot_take_the_record(
    policy: None, test_database_url: str, policy_auditor_url: str
) -> None:
    """The auditor role can only read: appending as it fails, as a down audit log would."""
    async with await _owner(test_database_url) as owner:
        with pytest.raises(ApproverLoginError, match="no login was kept"):
            await add_approver(owner, _audit(policy_auditor_url), AIDEN, "Aiden", ["approver"])
        assert await _rows(test_database_url, "SELECT count(*) FROM policy.approvers") == [(0,)]
        assert (
            await _rows(
                test_database_url,
                f"SELECT 1 FROM pg_roles WHERE rolname = 'policy_approver_{AIDEN}'",
            )
            == []
        ), "the login that was made is dropped"


# --- rotation and removal ------------------------------------------------------------------


async def test_rotating_replaces_the_password_at_once_and_ends_the_open_sessions(
    policy: None, test_database_url: str, policy_gateway_url: str
) -> None:
    async with await _owner(test_database_url) as owner:
        audit = _audit(policy_gateway_url)
        first = await add_approver(owner, audit, AIDEN, "Aiden", ["approver"])
        old_url = login_url(test_database_url, first.login, first.password or "")
        open_session = await _owner(old_url)

        rotated = await rotate_approver(owner, audit, AIDEN)

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


async def test_rotating_refuses_to_make_a_login_again_whose_mapping_is_for_the_old_role(
    policy: None, test_database_url: str, policy_gateway_url: str
) -> None:
    """A login is mapped to its principal by the role's OID, and never twice. A role made again has
    a new OID, so the new login could never decide: say so, and make nothing."""
    async with await _owner(test_database_url) as owner:
        audit = _audit(policy_gateway_url)
        await add_approver(owner, audit, AIDEN, "Aiden", ["approver"])
        await owner.execute(f"DROP OWNED BY policy_approver_{AIDEN}")
        await owner.execute(f"DROP ROLE policy_approver_{AIDEN}")

        with pytest.raises(ApproverLoginError, match="remove approver"):
            await rotate_approver(owner, audit, AIDEN)

    assert (
        await _rows(
            test_database_url, f"SELECT 1 FROM pg_roles WHERE rolname = 'policy_approver_{AIDEN}'"
        )
        == []
    )


async def test_removing_takes_the_login_and_the_sessions_and_keeps_the_name_for_good(
    policy: None, test_database_url: str, policy_gateway_url: str
) -> None:
    async with await _owner(test_database_url) as owner:
        audit = _audit(policy_gateway_url)
        added = await add_approver(owner, audit, AIDEN, "Aiden", ["approver"])
        url = login_url(test_database_url, added.login, added.password or "")
        open_session = await _owner(url)

        await remove_approver(owner, audit, AIDEN)

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
        assert row == [(False, True, f"policy_approver_{AIDEN}")], "the row stays, as history"
        with pytest.raises(ApproverLoginError, match="never reused"):
            await add_approver(owner, audit, AIDEN, "Someone Else", ["approver"])
        with pytest.raises(ApproverLoginError, match="no active approver"):
            await rotate_approver(owner, audit, AIDEN)


async def test_a_decision_stays_attributed_after_the_approver_is_removed(
    policy: None,
    make_approver: MakeApprover,
    test_database_url: str,
    policy_gateway_url: str,
    policy_auditor_url: str,
) -> None:
    aiden = Approvals(await make_approver(AIDEN), ROLES)
    tyler = Approvals(await make_approver(TYLER), ROLES)
    await aiden.decide(await _ask(policy_gateway_url), Decision.REJECT, "no")
    other = await _ask(policy_gateway_url, ticket_id="TKT-000002", status="closed")
    await tyler.decide(other, Decision.REJECT, "no")

    async with await _owner(test_database_url) as owner:
        await remove_approver(owner, _audit(policy_gateway_url), AIDEN)

    auditor = audit_log_on(open_database(SecretStr(policy_url(policy_auditor_url))))
    logins = await _approver_logins(policy_auditor_url)
    assert logins[f"policy_approver_{AIDEN}"] == AIDEN, "a removed approver's login stays known"
    await verify_with_anchors(auditor, [], approver_logins=logins)
    assert logins[f"policy_approver_{TYLER}"] == TYLER


async def test_removing_takes_effect_on_what_the_approver_approved_but_nobody_used(
    policy: None,
    make_approver: MakeApprover,
    test_database_url: str,
    policy_gateway_url: str,
) -> None:
    aiden = Approvals(await make_approver(AIDEN), ROLES)
    gate, ctx, call = _gate(policy_gateway_url, hold_s=0), _context(), _call()
    pending = await gate.decide(ctx, call)
    await aiden.decide(UUID(pending.approval_id or ""), Decision.APPROVE, None)

    async with await _owner(test_database_url) as owner:
        await remove_approver(owner, _audit(policy_gateway_url), AIDEN)

    assert (await gate.decide(ctx, call)).outcome is ApprovalOutcome.UNAVAILABLE
    status = await _rows(test_database_url, "SELECT status FROM policy.agent_core_approvals")
    assert status == [("approved",)], "refused by the gate: not consumed"


async def test_a_login_cannot_record_another_approvers_name_as_its_decision(
    policy: None, make_approver: MakeApprover, policy_gateway_url: str
) -> None:
    """Login binding: the guard judges `session_user`, the login that authenticated, and refuses a
    decision whose `resolved_by` is not the principal the owner mapped to it. By the library, by
    plain SQL, and after a `SET ROLE` to the group or to another approver's login."""
    aiden, tyler_url = await make_approver(AIDEN), await make_approver(TYLER)
    gate, ctx, call = _gate(policy_gateway_url, hold_s=0), _context(), _call()
    pending = await gate.decide(ctx, call)
    request_id = UUID(pending.approval_id or "")
    queue = approval_queue_on(
        open_database(SecretStr(policy_url(aiden)), max_connections=1),
        policy=RoleApproverPolicy(roles_by_action=ROLES),
    )
    tyler = Principal(id=f"human:{TYLER}", kind=PrincipalKind.HUMAN, roles=frozenset({"approver"}))

    with pytest.raises(NotAuthorizedToResolveError, match="mapped"):
        await queue.resolve(request_id, decision=Decision.APPROVE, principal=tyler)
    await queue.database.aclose()  # the login may hold two connections
    with pytest.raises(errors.RaiseException, match="mapped to the deciding login"):
        await _approve_in_plain_sql(aiden, request_id, claiming=f"human:{TYLER}")
    async with await _owner(aiden) as connection:
        for role in ("policy_approver", f"policy_approver_{TYLER}"):
            with pytest.raises(errors.InsufficientPrivilege):
                await connection.execute(f"SET ROLE {role}".encode())
            await connection.rollback()
    del tyler_url

    assert (await gate.decide(ctx, call)).outcome is ApprovalOutcome.PENDING, "nothing was decided"
    await _approve_in_plain_sql(aiden, request_id, claiming=f"human:{AIDEN}")  # its own: allowed


async def test_a_login_with_no_mapping_cannot_decide_at_all(
    policy: None, test_database_url: str, policy_gateway_url: str
) -> None:
    """A member of the approver role that the owner has not mapped (a login made by hand, or a
    service such as the payload purge) can read the queue but never decide."""
    request_id = await _ask(policy_gateway_url)
    async with await _owner(test_database_url) as owner:
        await owner.execute("CREATE ROLE policy_approver_unmapped LOGIN PASSWORD 'unmapped-pw'")
        await owner.execute("GRANT policy_approver TO policy_approver_unmapped")
    try:
        with pytest.raises(errors.RaiseException, match="mapped to the deciding login"):
            await _approve_in_plain_sql(
                login_url(test_database_url, "policy_approver_unmapped", "unmapped-pw"),
                request_id,
                claiming="human:anyone",
            )
    finally:
        async with await _owner(test_database_url) as owner:
            await owner.execute("DROP OWNED BY policy_approver_unmapped")
            await owner.execute("DROP ROLE policy_approver_unmapped")


async def test_the_mapping_is_written_by_the_owner_alone_and_never_reused(
    policy: None, make_approver: MakeApprover, test_database_url: str, policy_gateway_url: str
) -> None:
    aiden = await make_approver(AIDEN)
    async with await _owner(aiden) as login:
        with pytest.raises(errors.InsufficientPrivilege):
            await login.execute(
                "INSERT INTO policy.agent_core_approver_logins (login, login_oid, principal)"
                f" VALUES ('policy_approver_{AIDEN}', 1, 'human:other')"
            )
    async with await _owner(test_database_url) as owner:
        await remove_approver(owner, _audit(policy_gateway_url), AIDEN)
        with pytest.raises(ApproverLoginError, match="removed"):
            await add_approver(owner, _audit(policy_gateway_url), AIDEN, "Again", ["approver"])
    assert await _rows(
        test_database_url,
        "SELECT principal, removed_at IS NOT NULL FROM policy.agent_core_approver_logins",
    ) == [(f"human:{AIDEN}", True)], "the row stays, ended, so the login and principal are taken"


async def test_setup_goes_on_when_the_only_decisions_are_by_removed_approvers(
    policy: None,
    make_approver: MakeApprover,
    test_database_url: str,
    policy_gateway_url: str,
    policy_auditor_url: str,
) -> None:
    """agent-core's installer refuses to cancel anything when no current approver has decided
    anything. Everyone who ever decided having left must not stop the stack from starting."""
    aiden = Approvals(await make_approver(AIDEN), ROLES)
    pending = await _gate(policy_gateway_url, hold_s=0).decide(_context(), _call())
    await aiden.decide(UUID(pending.approval_id or ""), Decision.APPROVE, None)
    async with await _owner(test_database_url) as owner:
        await remove_approver(owner, _audit(policy_gateway_url), AIDEN)

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
    aiden = Approvals(await make_approver(AIDEN), ROLES)
    tyler = Approvals(await make_approver(TYLER), ROLES)
    first = await _gate(policy_gateway_url, hold_s=0).decide(_context(), _call())
    await aiden.decide(UUID(first.approval_id or ""), Decision.APPROVE, None)
    second = await _ask(policy_gateway_url, ticket_id="TKT-000002", status="closed")
    await tyler.decide(second, Decision.APPROVE, None)
    async with await _owner(test_database_url) as owner:
        await remove_approver(owner, _audit(policy_gateway_url), AIDEN)

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
    url = await make_approver(AIDEN)
    async with await _owner(test_database_url) as owner:
        await owner.execute(f"GRANT pg_read_all_data TO policy_approver_{AIDEN}")
        await owner.execute(f"GRANT policy_gateway TO policy_approver_{AIDEN}")
        await owner.execute(f"GRANT policy_approver TO policy_approver_{AIDEN} WITH SET TRUE")
        await owner.execute(f"GRANT SELECT ON policy.agent_core_audit TO policy_approver_{AIDEN}")
        await owner.execute(f"ALTER ROLE policy_approver_{AIDEN} SUPERUSER CREATEROLE")

    await _setup(test_database_url, policy_gateway_url, policy_auditor_url)

    memberships = await _rows(
        test_database_url,
        "SELECT parent.rolname, m.inherit_option, m.set_option FROM pg_auth_members m"
        " JOIN pg_roles parent ON parent.oid = m.roleid"
        " JOIN pg_roles member ON member.oid = m.member"
        f" WHERE member.rolname = 'policy_approver_{AIDEN}' ORDER BY 1",
    )
    assert memberships == [("policy_approver", True, False)]
    assert await _rows(
        test_database_url,
        "SELECT rolsuper, rolcreaterole, has_table_privilege(oid, 'policy.agent_core_audit',"
        f" 'SELECT') FROM pg_roles WHERE rolname = 'policy_approver_{AIDEN}'",
    ) == [(False, False, True)], "SELECT on the audit log comes from the approver role, not a grant"
    direct = await _rows(
        test_database_url,
        "SELECT count(*) FROM information_schema.role_table_grants"
        f" WHERE grantee = 'policy_approver_{AIDEN}'",
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
    url = await make_approver(AIDEN)
    async with await _owner(test_database_url) as owner:
        await owner.execute(
            f"UPDATE policy.approvers SET active = false, removed_at = now() WHERE id = '{AIDEN}'"
        )

    await _setup(test_database_url, policy_gateway_url, policy_auditor_url)

    assert await _rows(
        test_database_url,
        f"SELECT rolcanlogin FROM pg_roles WHERE rolname = 'policy_approver_{AIDEN}'",
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
    await make_approver(AIDEN)
    async with await _owner(test_database_url) as owner:
        await owner.execute(f"DROP OWNED BY policy_approver_{AIDEN}")
        await owner.execute(f"DROP ROLE policy_approver_{AIDEN}")

    await _setup(test_database_url, policy_gateway_url, policy_auditor_url)

    assert f"approver {AIDEN} has no login role" in caplog.text


# --- the table that says who is who --------------------------------------------------------


async def test_only_the_owner_writes_the_approvers_and_none_of_them_is_ever_deleted_or_reused(
    policy: None,
    make_approver: MakeApprover,
    test_database_url: str,
    policy_gateway_url: str,
    policy_auditor_url: str,
) -> None:
    await make_approver(AIDEN)
    privileges = await _rows(
        test_database_url,
        "SELECT r, p, has_table_privilege(r, 'policy.approvers', p) FROM"
        " unnest(ARRAY['policy_gateway', 'policy_approver', 'policy_auditor',"
        f" 'policy_approver_{AIDEN}']) r,"
        " unnest(ARRAY['INSERT', 'UPDATE', 'DELETE', 'TRUNCATE']) p",
    )
    assert [(r, p) for r, p, allowed in privileges if allowed] == [], "owner only"

    async with await _owner(test_database_url) as owner:
        for statement, why in (
            (f"DELETE FROM policy.approvers WHERE id = '{AIDEN}'", "never deleted"),
            ("TRUNCATE policy.approvers", "never deleted"),
            ("UPDATE policy.approvers SET db_role = 'policy_approver_other'", "keeps its id"),
            ("UPDATE policy.approvers SET id = 'other'", "keeps its id"),
        ):
            with pytest.raises(errors.IntegrityConstraintViolation, match=why):
                await owner.execute(statement.encode())
        await owner.execute(
            f"UPDATE policy.approvers SET active = false, removed_at = now() WHERE id = '{AIDEN}'"
        )
        with pytest.raises(errors.IntegrityConstraintViolation, match="stays removed"):
            await owner.execute(f"UPDATE policy.approvers SET active = true WHERE id = '{AIDEN}'")
        with pytest.raises(errors.IntegrityConstraintViolation, match="stays removed"):
            await owner.execute(
                f"UPDATE policy.approvers SET removed_at = NULL WHERE id = '{AIDEN}'"
            )
        with pytest.raises(errors.UniqueViolation):
            await owner.execute(
                "INSERT INTO policy.approvers (id, display_name, roles, db_role)"
                f" VALUES ('twin', 'Twin', '{{approver}}', 'policy_approver_{AIDEN}')"
            )


async def test_the_dashboard_reader_sees_the_upstream_the_client_and_who_decided_and_no_more(
    policy: None,
    telemetry: None,
    make_approver: MakeApprover,
    make_client: MakeClient,
    test_database_url: str,
    policy_gateway_url: str,
    policy_auditor_url: str,
    reader_url: str,
) -> None:
    aiden = Approvals(await make_approver(AIDEN), ROLES)
    client_id, _ = await make_client("harborline-ops-bot", [])
    # The reader's role exists now (the telemetry setup made it), so this setup grants it the view.
    await _setup(test_database_url, policy_gateway_url, policy_auditor_url)
    registered = await _gate(policy_gateway_url).decide(_context(client_id), _call())
    unregistered = await _ask(policy_gateway_url, ticket_id="TKT-000002", status="closed")
    await aiden.decide(UUID(registered.approval_id or ""), Decision.APPROVE, None)
    await aiden.decide(unregistered, Decision.REJECT, "no")

    rows = await _rows(
        reader_url,
        "SELECT namespace, resolved_by, resolved_by_name, status, client_name"
        " FROM policy.dash_approvals ORDER BY status",
    )

    assert rows == [
        ("tickets", f"human:{AIDEN}", "Aiden", "approved", "harborline-ops-bot"),
        ("tickets", f"human:{AIDEN}", "Aiden", "rejected", None),
    ], "a client that is not in the registry has no name to show"
    with pytest.raises(errors.InsufficientPrivilege):
        await _rows(reader_url, "SELECT * FROM policy.approvers")
    with pytest.raises(errors.InsufficientPrivilege):
        await _rows(reader_url, "SELECT * FROM policy.approver_logins")
    with pytest.raises(errors.InsufficientPrivilege):
        await _rows(reader_url, "SELECT * FROM public.clients")


# --- what the audit log says ---------------------------------------------------------------


async def _forge(
    url: str, action: str, actor: str, subject: str | None = None, **payload: Any
) -> None:
    log = audit_log_on(open_database(SecretStr(policy_url(url))))
    await log.append(
        AuditEvent(
            action=action, actor_id=actor, subject_id=subject or str(uuid4()), payload=payload
        )
    )


async def test_anchoring_and_verifying_accept_a_decision_by_an_approvers_login(
    policy: None,
    make_approver: MakeApprover,
    policy_gateway_url: str,
    policy_auditor_url: str,
    tmp_path: Path,
) -> None:
    """Both commands check the chain before they trust it, and both must know which login is
    which approver, or every decision a person makes would fail them."""
    aiden = Approvals(await make_approver(AIDEN), ROLES)
    await aiden.decide(await _ask(policy_gateway_url), Decision.REJECT, "no")
    anchors = tmp_path / "anchors.jsonl"

    await _audit_anchor(policy_auditor_url, argparse.Namespace(file=anchors))
    await _audit_verify(policy_auditor_url, argparse.Namespace(anchors=anchors))


async def test_only_the_gateway_role_may_have_written_a_provisioning_record(
    policy: None,
    make_approver: MakeApprover,
    policy_gateway_url: str,
    policy_auditor_url: str,
) -> None:
    aiden = await make_approver(AIDEN)  # its own `approver.added` is by the gateway role: fine
    auditor = audit_log_on(open_database(SecretStr(policy_url(policy_auditor_url))))
    logins = await _approver_logins(policy_auditor_url)
    await verify_with_anchors(auditor, [], approver_logins=logins)

    await _forge(aiden, "approver.removed", "admin")  # an approver's login writing one

    with pytest.raises(AuditIntegrityError, match=f"written by role policy_approver_{AIDEN}"):
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
            test_database_url, argparse.Namespace(id=AIDEN, name="Aiden", role=None)
        )
    monkeypatch.setenv("POLICY_GATEWAY_DATABASE_URL", policy_gateway_url)

    await _approver_add(test_database_url, argparse.Namespace(id=AIDEN, name="Aiden", role=None))
    added = capsys.readouterr().out
    await _approver_list(test_database_url, argparse.Namespace())
    listed = capsys.readouterr().out
    await _approver_rotate(test_database_url, argparse.Namespace(id=AIDEN))
    rotated = capsys.readouterr().out
    await _approver_remove(test_database_url, argparse.Namespace(id=AIDEN))
    removed = capsys.readouterr().out

    first = next(line for line in added.splitlines() if line.startswith("password  "))
    second = next(line for line in rotated.splitlines() if line.startswith("password  "))
    assert f"login     policy_approver_{AIDEN}" in added
    assert first != second
    assert first.split()[1] not in listed + removed, "shown once, never listed"
    assert f"active  policy_approver_{AIDEN}" in listed
    assert "removed" in removed


# --- what the gatekeeper's review asked to be proved --------------------------------------------

_TS = "to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"')"


async def _approve_in_plain_sql(login: str, request_id: UUID, claiming: str) -> None:
    """What a login can do without the tool: approve a pending request itself, saying who did."""
    async with await _owner(login) as connection:
        await connection.execute(
            "UPDATE policy.agent_core_approvals SET status = 'approved', decision = 'approve',"
            f" resolved_by = %s, resolved_at = {_TS} WHERE id = %s".encode(),
            (claiming, str(request_id)),
        )


async def test_the_lab_login_cannot_set_role_to_the_shared_approver_role(
    policy: None,
    test_database_url: str,
    policy_gateway_url: str,
    policy_auditor_url: str,
) -> None:
    await _setup(test_database_url, policy_gateway_url, policy_auditor_url, lab="lab-pw-0123456789")

    async with await _owner(
        login_url(test_database_url, "policy_lab_approver", "lab-pw-0123456789")
    ) as lab:
        with pytest.raises(errors.InsufficientPrivilege):
            await lab.execute("SET ROLE policy_approver")
    assert await _rows(
        test_database_url,
        "SELECT m.inherit_option, m.set_option, m.admin_option FROM pg_auth_members m"
        " JOIN pg_roles r ON r.oid = m.member WHERE r.rolname = 'policy_lab_approver'",
    ) == [(True, False, False)]


async def test_setup_takes_back_the_right_to_grant_on_and_roles_that_are_members_of_a_login(
    policy: None,
    make_approver: MakeApprover,
    test_database_url: str,
    policy_gateway_url: str,
    policy_auditor_url: str,
) -> None:
    await make_approver(AIDEN)
    async with await _owner(test_database_url) as owner:
        await owner.execute("CREATE ROLE bystander NOLOGIN")
        try:
            await owner.execute(
                f"GRANT policy_approver TO policy_approver_{AIDEN}"
                " WITH ADMIN TRUE, INHERIT TRUE, SET FALSE"
            )
            await owner.execute(
                f"GRANT policy_approver_{AIDEN} TO bystander"
            )  # could SET ROLE to it

            await _setup(test_database_url, policy_gateway_url, policy_auditor_url)

            assert await _rows(
                test_database_url,
                "SELECT parent.rolname, member.rolname, m.admin_option FROM pg_auth_members m"
                " JOIN pg_roles parent ON parent.oid = m.roleid"
                " JOIN pg_roles member ON member.oid = m.member"
                f" WHERE member.rolname IN ('policy_approver_{AIDEN}', 'bystander')"
                f" OR parent.rolname = 'policy_approver_{AIDEN}'",
            ) == [("policy_approver", f"policy_approver_{AIDEN}", False)]
        finally:
            await owner.execute("DROP ROLE IF EXISTS bystander")


async def test_a_decision_by_an_approver_without_the_role_the_request_needed_is_not_used(
    policy: None, make_approver: MakeApprover, policy_gateway_url: str
) -> None:
    intern = await make_approver(INTERN, ["reader"])
    gate, ctx, call = _gate(policy_gateway_url, hold_s=0), _context(), _call()
    pending = await gate.decide(ctx, call)
    request_id = UUID(pending.approval_id or "")

    await _approve_in_plain_sql(intern, request_id, claiming=f"human:{INTERN}")
    await _forge(
        intern, "approval.resolved", f"human:{INTERN}", str(request_id), decision="approve"
    )

    assert (await gate.decide(ctx, call)).outcome is ApprovalOutcome.UNAVAILABLE


async def test_a_second_decision_on_one_request_fails_verification(
    policy: None,
    make_approver: MakeApprover,
    policy_gateway_url: str,
    policy_auditor_url: str,
) -> None:
    aiden = await make_approver(AIDEN)
    request_id = await _ask(policy_gateway_url)
    await Approvals(aiden, ROLES).decide(request_id, Decision.REJECT, "no")
    auditor = audit_log_on(open_database(SecretStr(policy_url(policy_auditor_url))))
    logins = await _approver_logins(policy_auditor_url)
    await verify_with_anchors(auditor, [], approver_logins=logins)

    await _forge(aiden, "approval.resolved", f"human:{AIDEN}", str(request_id), decision="approve")

    with pytest.raises(AuditIntegrityError, match="second decision"):
        await verify_with_anchors(auditor, [], approver_logins=logins)


async def test_setup_still_stops_for_a_plain_sql_approval_when_only_removed_approvers_decided(
    policy: None,
    make_approver: MakeApprover,
    test_database_url: str,
    policy_gateway_url: str,
    policy_auditor_url: str,
) -> None:
    """The fallback is for approvals a removed approver made through the tool. One made in plain
    SQL has no decision on record, and setup must still refuse to go on."""
    aiden = await make_approver(AIDEN)
    explained = Approvals(aiden, ROLES)
    first = await _gate(policy_gateway_url, hold_s=0).decide(_context(), _call())
    await explained.decide(UUID(first.approval_id or ""), Decision.APPROVE, None)
    second = await _ask(policy_gateway_url, ticket_id="TKT-000002", status="closed")
    await _approve_in_plain_sql(aiden, second, claiming=f"human:{AIDEN}")
    async with await _owner(test_database_url) as owner:
        await remove_approver(owner, _audit(policy_gateway_url), AIDEN)

    with pytest.raises(ConfigError, match=r"approval\.resolved"):
        await _setup(test_database_url, policy_gateway_url, policy_auditor_url)


async def test_rotating_records_first_so_a_log_that_is_down_locks_nobody_out(
    policy: None, test_database_url: str, policy_gateway_url: str, policy_auditor_url: str
) -> None:
    async with await _owner(test_database_url) as owner:
        added = await add_approver(owner, _audit(policy_gateway_url), AIDEN, "Aiden", ["approver"])
        url = login_url(test_database_url, added.login, added.password or "")
        async with await _owner(url) as session:
            with pytest.raises(Exception):  # noqa: B017, PT011 - the auditor role cannot append
                await rotate_approver(owner, _audit(policy_auditor_url), AIDEN)

            await session.execute("SELECT 1")  # still signed in
        async with await _owner(url):
            pass  # and the old password still works


async def test_removing_goes_ahead_when_the_log_is_down_and_says_it_is_not_recorded(
    policy: None, test_database_url: str, policy_gateway_url: str, policy_auditor_url: str
) -> None:
    async with await _owner(test_database_url) as owner:
        await add_approver(owner, _audit(policy_gateway_url), AIDEN, "Aiden", ["approver"])

        with pytest.raises(ApproverLoginError, match="could not record it"):
            await remove_approver(owner, _audit(policy_auditor_url), AIDEN)

    assert (
        await _rows(
            test_database_url, f"SELECT 1 FROM pg_roles WHERE rolname = 'policy_approver_{AIDEN}'"
        )
        == []
    ), "a person who must lose access loses it"


async def test_the_reserved_lab_id_cannot_be_rotated_or_removed(
    policy: None, test_database_url: str, policy_gateway_url: str
) -> None:
    async with await _owner(test_database_url) as owner:
        for action in (rotate_approver, remove_approver):
            with pytest.raises(ApproverLoginError, match="reserved"):
                await action(owner, _audit(policy_gateway_url), "lab-approver")


async def test_the_url_agent_core_maps_logins_with_keeps_the_owners_connection_settings(
    policy: None, test_database_url: str
) -> None:
    """The mapping functions open a connection of their own: it must not be weaker than the
    owner's (a URL that dropped `sslmode` would quietly downgrade it)."""
    from urllib.parse import parse_qs, urlsplit

    from ai_gateway.policy.approver_logins import owner_url_of

    url = test_database_url + ("&" if "?" in test_database_url else "?") + "sslmode=prefer"
    async with await _owner(url) as owner:
        rebuilt = urlsplit(owner_url_of(owner))

    assert parse_qs(rebuilt.query)["sslmode"] == ["prefer"]
    assert rebuilt.username == urlsplit(test_database_url).username
    assert rebuilt.path == urlsplit(test_database_url).path
