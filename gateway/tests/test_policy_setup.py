"""The policy schema: what each role can do to the audit log and the approval queue."""

from uuid import uuid4

import anyio
import psycopg
import pytest
from aox_agent_core.approvals import Decision, Principal, PrincipalKind, SQLApprovalQueue
from aox_agent_core.audit import AuditEvent, SQLAuditLog
from aox_agent_core.errors import ApprovalError, ApprovalPayloadMismatchError
from aox_agent_core.storage import open_database
from psycopg import errors
from pydantic import SecretStr

from ai_gateway.policy import APPROVALS_TABLE, AUDIT_TABLE, ROLES, SCHEMA, policy_url
from ai_gateway.policy.setup import (
    PolicyPasswords,
    _install_tables,
    grant_policy_access,
    setup_policy,
)
from mcp_common.roles import advisory_lock, ensure_schema
from tests.conftest import password_of

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

PAYLOAD = {"ticket_id": "TKT-000001", "status": "closed"}
ACTION = "tickets__change_status"
CLIENT = Principal(id=f"client:{uuid4()}", kind=PrincipalKind.AGENT)
HUMAN = Principal(id="human:aiden", kind=PrincipalKind.HUMAN, roles=frozenset({"approver"}))


def _queue(url: str) -> SQLApprovalQueue:
    database = open_database(SecretStr(policy_url(url)))
    return SQLApprovalQueue(database, audit_log=SQLAuditLog(database))


def _event() -> AuditEvent:
    return AuditEvent(
        action="gateway.tool_call",
        actor_id=f"client:{uuid4()}",
        subject_id="tickets__get_ticket",
        payload={"request_id": str(uuid4()), "outcome": "forwarded"},
    )


async def _as(url: str) -> psycopg.AsyncConnection:
    return await psycopg.AsyncConnection.connect(policy_url(url), autocommit=True)


async def _pending(gateway_url: str) -> object:
    return await _queue(gateway_url).submit(
        action=ACTION,
        summary="close a ticket",
        payload=PAYLOAD,
        requested_by=CLIENT,
        required_role="approver",
        ttl_seconds=300,
    )


# --- setup -----------------------------------------------------------------------------------


async def test_setup_installs_the_tables_in_the_policy_schema_once_and_is_safe_to_repeat(
    policy: None,
    test_database_url: str,
    policy_gateway_url: str,
    policy_approver_url: str,
    policy_auditor_url: str,
) -> None:
    await _queue(policy_gateway_url).database.run(lambda s: s.execute("SELECT 1"))
    await SQLAuditLog(open_database(SecretStr(policy_url(policy_gateway_url)))).append(_event())

    await setup_policy(
        test_database_url,
        PolicyPasswords(
            password_of(policy_gateway_url),
            password_of(policy_approver_url),
            password_of(policy_auditor_url),
        ),
    )

    async with await _as(test_database_url) as connection:
        cursor = await connection.execute(
            "SELECT table_schema FROM information_schema.tables WHERE table_name = ANY(%s)"
            " ORDER BY table_name",
            ([AUDIT_TABLE, APPROVALS_TABLE],),
        )
        assert await cursor.fetchall() == [(SCHEMA,), (SCHEMA,)]
        cursor = await connection.execute(b"SELECT count(*) FROM agent_core_audit")
        assert await cursor.fetchone() == (1,), "a repeat setup must not touch the records"


async def test_no_real_role_holds_a_grant_while_agent_cores_tables_are_installed(
    policy: None, test_database_url: str
) -> None:
    """agent-core's installer grants its app role UPDATE on approvals, and the guard does not exist
    yet. It installs for a scratch role that cannot log in, which is dropped with its grants."""
    async with await _as(test_database_url) as connection:
        await connection.execute("DROP SCHEMA policy CASCADE")
        await ensure_schema(connection, SCHEMA)
        await _install_tables(connection, test_database_url)

        cursor = await connection.execute(
            "SELECT r.rolname, t.relname FROM pg_roles r CROSS JOIN pg_class t"
            " WHERE r.rolname = ANY(%s) AND t.relnamespace = 'policy'::regnamespace"
            " AND t.relkind = 'r' AND (has_table_privilege(r.oid, t.oid, 'SELECT')"
            " OR has_table_privilege(r.oid, t.oid, 'INSERT')"
            " OR has_table_privilege(r.oid, t.oid, 'UPDATE'))",
            (list(ROLES),),
        )
        assert await cursor.fetchall() == []
        cursor = await connection.execute(
            "SELECT count(*) FROM pg_roles WHERE rolname LIKE 'policy_install_%'"
        )
        assert await cursor.fetchone() == (0,)
        cursor = await connection.execute(
            "SELECT count(*) FROM pg_class WHERE relnamespace = 'policy'::regnamespace"
            " AND relname IN ('agent_core_audit', 'agent_core_approvals')"
        )
        assert await cursor.fetchone() == (2,)


async def test_two_setups_at_once_do_not_collide(
    test_database_url: str,
    policy_gateway_url: str,
    policy_approver_url: str,
    policy_auditor_url: str,
) -> None:
    async with await _as(test_database_url) as connection:
        await connection.execute("DROP SCHEMA IF EXISTS policy CASCADE")
    passwords = PolicyPasswords(
        password_of(policy_gateway_url),
        password_of(policy_approver_url),
        password_of(policy_auditor_url),
    )
    failures: list[BaseException] = []

    async def run() -> None:
        try:
            await setup_policy(test_database_url, passwords)
        except BaseException as error:
            failures.append(error)

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(run)
        tasks.start_soon(run)

    assert failures == []
    async with await _as(test_database_url) as connection:
        cursor = await connection.execute(
            "SELECT count(*) FROM pg_trigger WHERE tgname = 'agent_core_approvals_guard'"
        )
        assert await cursor.fetchone() == (1,)


@pytest.mark.parametrize("empty", ["gateway", "approver", "auditor"])
async def test_setup_refuses_an_empty_password(test_database_url: str, empty: str) -> None:
    passwords = {"gateway": "g", "approver": "a", "auditor": "u", **{empty: ""}}

    with pytest.raises(ValueError, match="empty"):
        await setup_policy(test_database_url, PolicyPasswords(**passwords))


# --- the audit log -----------------------------------------------------------------------------


async def test_the_gateway_can_append_and_read_but_nobody_can_change_or_remove(
    policy: None, policy_gateway_url: str, test_database_url: str
) -> None:
    log = SQLAuditLog(open_database(SecretStr(policy_url(policy_gateway_url))))
    record = await log.append(_event())
    assert record.seq == 1

    for role_url in (policy_gateway_url, test_database_url):  # the owner too: triggers
        async with await _as(role_url) as connection:
            for statement in (
                "UPDATE agent_core_audit SET action = 'x.y'",
                "DELETE FROM agent_core_audit",
                "TRUNCATE agent_core_audit",
            ):
                with pytest.raises((errors.RaiseException, errors.InsufficientPrivilege)):
                    await connection.execute(statement.encode())


async def test_the_auditor_reads_the_log_and_can_write_nothing(
    policy: None, policy_gateway_url: str, policy_auditor_url: str
) -> None:
    await SQLAuditLog(open_database(SecretStr(policy_url(policy_gateway_url)))).append(_event())
    auditor = SQLAuditLog(open_database(SecretStr(policy_url(policy_auditor_url))))

    assert (await auditor.verify()).seq == 1
    with pytest.raises(Exception, match=r"(?i)privilege|permission|insert|append"):
        await auditor.append(_event())


@pytest.mark.parametrize("role", ["gateway", "approver", "auditor"])
async def test_no_role_reaches_the_registry_or_another_schema(
    policy: None,
    role: str,
    policy_gateway_url: str,
    policy_approver_url: str,
    policy_auditor_url: str,
) -> None:
    url = {"gateway": policy_gateway_url, "approver": policy_approver_url}.get(
        role, policy_auditor_url
    )
    async with await _as(url) as connection:
        for statement in (
            "SELECT * FROM public.client_tokens",
            "SELECT * FROM telemetry.requests",
            "CREATE TABLE policy.intruder (id integer)",
        ):
            with pytest.raises((errors.InsufficientPrivilege, errors.UndefinedTable)):
                await connection.execute(statement.encode())


# --- the approval guard ------------------------------------------------------------------------


async def test_the_library_flow_works_across_the_two_roles(
    policy: None, policy_gateway_url: str, policy_approver_url: str
) -> None:
    gateway, approver = _queue(policy_gateway_url), _queue(policy_approver_url)
    request = await _pending(policy_gateway_url)

    await approver.resolve(request.id, decision=Decision.APPROVE, principal=HUMAN)  # type: ignore[attr-defined]
    consumed = await gateway.consume(
        request.id,  # type: ignore[attr-defined]
        action=ACTION,
        payload=PAYLOAD,
        principal=CLIENT,
    )

    assert consumed.status.value == "consumed"


async def test_the_gateway_cannot_decide_a_request_by_any_route(
    policy: None, policy_gateway_url: str
) -> None:
    request = await _pending(policy_gateway_url)

    with pytest.raises(Exception, match="may only consume"):
        await _queue(policy_gateway_url).resolve(
            request.id,  # type: ignore[attr-defined]
            decision=Decision.APPROVE,
            principal=HUMAN,  # a policy that would allow it is not enough: the database refuses
        )
    async with await _as(policy_gateway_url) as connection:
        with pytest.raises(errors.RaiseException, match="may only consume"):
            await connection.execute(
                "UPDATE agent_core_approvals SET status = 'approved', decision = 'approve',"
                " resolved_by = 'human:x', resolved_at = now()::text WHERE id = %s",
                (str(request.id),),  # type: ignore[attr-defined]
            )


async def test_nobody_can_create_a_request_that_is_already_decided(
    policy: None, policy_gateway_url: str, test_database_url: str
) -> None:
    insert = (
        "INSERT INTO agent_core_approvals (id, action, summary, payload_sha256, requested_by,"
        " required_role, created_at, expires_at, status, decision, resolved_by, resolved_at)"
        " VALUES (gen_random_uuid()::text, 'a', 's', repeat('a', 64), 'client:b', 'approver',"
        " now()::text, (now() + interval '1 hour')::text, 'approved', 'approve', 'human:x',"
        " now()::text)"
    )
    for url in (policy_gateway_url, test_database_url):
        async with await _as(url) as connection:
            with pytest.raises(errors.RaiseException, match="only be created pending"):
                await connection.execute(insert.encode())


async def test_the_approver_cannot_create_requests_or_use_one(
    policy: None, policy_gateway_url: str, policy_approver_url: str
) -> None:
    request = await _pending(policy_gateway_url)
    await _queue(policy_approver_url).resolve(
        request.id,  # type: ignore[attr-defined]
        decision=Decision.APPROVE,
        principal=HUMAN,
    )

    async with await _as(policy_approver_url) as connection:
        with pytest.raises(errors.InsufficientPrivilege):
            await connection.execute(
                b"INSERT INTO agent_core_approvals (id, action, summary, payload_sha256,"
                b" requested_by, required_role, created_at, expires_at, status)"
                b" VALUES (gen_random_uuid()::text,"
                b" 'a', 's', repeat('a', 64), 'c', 'approver', now()::text,"
                b" (now() + interval '1 hour')::text, 'pending')"
            )
        with pytest.raises(errors.RaiseException, match="only decide a pending"):
            await connection.execute(
                "UPDATE agent_core_approvals SET status = 'consumed', consumed_at = now()::text"
                " WHERE id = %s",
                (str(request.id),),  # type: ignore[attr-defined]
            )


async def test_an_approval_is_single_use_and_bound_to_its_arguments(
    policy: None, policy_gateway_url: str, policy_approver_url: str
) -> None:
    gateway, approver = _queue(policy_gateway_url), _queue(policy_approver_url)
    request = await _pending(policy_gateway_url)
    await approver.resolve(request.id, decision=Decision.APPROVE, principal=HUMAN)  # type: ignore[attr-defined]

    with pytest.raises(ApprovalPayloadMismatchError):
        await gateway.consume(
            request.id,  # type: ignore[attr-defined]
            action=ACTION,
            payload={**PAYLOAD, "status": "open"},
            principal=CLIENT,
        )
    await gateway.consume(request.id, action=ACTION, payload=PAYLOAD, principal=CLIENT)  # type: ignore[attr-defined]
    with pytest.raises(ApprovalError):
        await gateway.consume(request.id, action=ACTION, payload=PAYLOAD, principal=CLIENT)  # type: ignore[attr-defined]


# --- the grants hold ---------------------------------------------------------------------------


async def test_a_widened_grant_or_role_attribute_is_taken_away_again(
    policy: None, test_database_url: str, policy_auditor_url: str
) -> None:
    async with await _as(test_database_url) as connection:
        await connection.execute(
            b"GRANT INSERT, UPDATE, DELETE ON policy.agent_core_audit TO policy_auditor"
        )
        await connection.execute("ALTER ROLE policy_auditor CREATEDB")
        await connection.execute("GRANT pg_read_all_data TO policy_auditor")

    async with await _as(test_database_url) as connection:
        await grant_policy_access(connection)
        cursor = await connection.execute(
            "SELECT rolcreatedb, (SELECT count(*) FROM pg_auth_members WHERE member = r.oid),"
            " has_table_privilege('policy_auditor', %s, 'INSERT')"
            " FROM pg_roles r WHERE rolname = 'policy_auditor'",
            ("policy.agent_core_audit",),
        )
        assert await cursor.fetchone() == (False, 0, False)


async def test_every_role_exists_without_login_extras(policy: None, test_database_url: str) -> None:
    async with await _as(test_database_url) as connection:
        cursor = await connection.execute(
            "SELECT rolname, rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls"
            " FROM pg_roles WHERE rolname = ANY(%s) ORDER BY rolname",
            (list(ROLES),),
        )
        rows = await cursor.fetchall()

    assert [row[0] for row in rows] == sorted(ROLES)
    assert all(not any(row[1:]) for row in rows)


# --- the guard's finer rules ----------------------------------------------------------------------


async def _approved(gateway_url: str, approver_url: str) -> object:
    request = await _pending(gateway_url)
    await _queue(approver_url).resolve(
        request.id,  # type: ignore[attr-defined]
        decision=Decision.APPROVE,
        principal=HUMAN,
    )
    return request


async def _run(url: str, statement: str, *params: object) -> None:
    async with await _as(url) as connection:
        await connection.execute(statement.encode(), params or None)


@pytest.mark.parametrize(
    "assignment",
    [
        "summary = 'something else'",
        "payload_sha256 = repeat('b', 64)",
        "action = 'tickets__assign'",
        "requested_by = 'client:someone-else'",
        "required_role = 'nobody'",
        "expires_at = (now() + interval '9 days')::text",
        "reason = 'because'",
    ],
)
async def test_the_gateway_can_consume_an_approval_and_change_nothing_else_in_the_same_update(
    policy: None, policy_gateway_url: str, policy_approver_url: str, assignment: str
) -> None:
    request = await _approved(policy_gateway_url, policy_approver_url)

    with pytest.raises(errors.RaiseException, match="may only consume"):
        await _run(
            policy_gateway_url,
            "UPDATE agent_core_approvals SET status = 'consumed', consumed_at = now()::text,"  # noqa: S608
            f" {assignment} WHERE id = %s",
            str(request.id),  # type: ignore[attr-defined]
        )


async def test_the_gateway_cannot_consume_an_approval_that_has_expired(
    policy: None, policy_gateway_url: str, policy_approver_url: str, test_database_url: str
) -> None:
    request = await _approved(policy_gateway_url, policy_approver_url)
    await _run(
        test_database_url,
        "UPDATE agent_core_approvals SET expires_at = (now() - interval '1 minute')::text"
        " WHERE id = %s",
        str(request.id),  # type: ignore[attr-defined]
    )

    with pytest.raises(errors.RaiseException, match="may only consume"):
        await _run(
            policy_gateway_url,
            "UPDATE agent_core_approvals SET status = 'consumed', consumed_at = now()::text"
            " WHERE id = %s",
            str(request.id),  # type: ignore[attr-defined]
        )


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("payload_sha256", "repeat('b', 64)"),  # approve something other than what was asked
        ("action", "'tickets__assign'"),
        ("requested_by", "'human:aiden'"),  # so a request can look self-made
        ("expires_at", "(now() + interval '9 days')::text"),
        ("required_role", "'nobody'"),
        ("decision", "'reject'"),  # a decision that contradicts the status
        ("resolved_by", "NULL"),
        ("resolved_at", "NULL"),
        ("consumed_at", "now()::text"),
    ],
)
async def test_the_approver_decides_a_request_and_changes_nothing_else_in_the_same_update(
    policy: None, policy_gateway_url: str, policy_approver_url: str, column: str, value: str
) -> None:
    request = await _pending(policy_gateway_url)
    assignments = {
        "status": "'approved'",
        "decision": "'approve'",
        "resolved_by": "'human:x'",
        "resolved_at": "now()::text",
        column: value,  # the one thing that should not be allowed
    }
    changes = ", ".join(f"{name} = {expression}" for name, expression in assignments.items())

    with pytest.raises(errors.RaiseException, match="may only decide"):
        await _run(
            policy_approver_url,
            f"UPDATE agent_core_approvals SET {changes} WHERE id = %s",  # noqa: S608
            str(request.id),  # type: ignore[attr-defined]
        )


async def test_the_approver_cannot_decide_a_request_twice_or_after_it_expired(
    policy: None, policy_gateway_url: str, policy_approver_url: str, test_database_url: str
) -> None:
    decided = await _approved(policy_gateway_url, policy_approver_url)
    with pytest.raises(errors.RaiseException, match="may only decide"):
        await _run(
            policy_approver_url,
            "UPDATE agent_core_approvals SET status = 'rejected', decision = 'reject'"
            " WHERE id = %s",
            str(decided.id),  # type: ignore[attr-defined]
        )

    expired = await _pending(policy_gateway_url)
    await _run(
        test_database_url,
        "UPDATE agent_core_approvals SET expires_at = (now() - interval '1 minute')::text"
        " WHERE id = %s",
        str(expired.id),  # type: ignore[attr-defined]
    )
    with pytest.raises(errors.RaiseException, match="may only decide"):
        await _run(
            policy_approver_url,
            "UPDATE agent_core_approvals SET status = 'approved', decision = 'approve',"
            " resolved_by = 'human:x', resolved_at = now()::text WHERE id = %s",
            str(expired.id),  # type: ignore[attr-defined]
        )


async def test_an_upsert_cannot_decide_a_request_either(
    policy: None, policy_gateway_url: str
) -> None:
    request = await _pending(policy_gateway_url)

    with pytest.raises(errors.RaiseException):
        await _run(
            policy_gateway_url,
            "INSERT INTO agent_core_approvals (id, action, summary, payload_sha256, requested_by,"
            " required_role, created_at, expires_at, status) VALUES (%s, 'a', 's', repeat('a', 64),"
            " 'c', 'approver', now()::text, (now() + interval '1 hour')::text, 'pending')"
            " ON CONFLICT (id) DO UPDATE SET status = 'approved', decision = 'approve',"
            " resolved_by = 'human:x', resolved_at = now()::text",
            str(request.id),  # type: ignore[attr-defined]
        )


async def test_a_role_that_only_inherits_a_policy_role_cannot_change_an_approval_and_loses_it(
    policy: None, policy_gateway_url: str, test_database_url: str
) -> None:
    """The guard keys on the current user, which for a member is the member and not the role it
    inherits from; it refuses roles it does not know, and setup removes such memberships."""
    request = await _pending(policy_gateway_url)
    async with await _as(test_database_url) as connection:
        await connection.execute("DROP ROLE IF EXISTS policy_intruder")
        await connection.execute("CREATE ROLE policy_intruder NOLOGIN")
        await connection.execute("GRANT policy_gateway TO policy_intruder")
        await connection.execute("SET ROLE policy_intruder")
        with pytest.raises(errors.RaiseException, match="only the gateway and approver"):
            await connection.execute(
                b"UPDATE agent_core_approvals SET status = 'approved', decision = 'approve',"
                b" resolved_by = 'human:x', resolved_at = now()::text WHERE id = %s",
                (str(request.id),),  # type: ignore[attr-defined]
            )
        await connection.execute("RESET ROLE")

        await grant_policy_access(connection)

        cursor = await connection.execute(
            "SELECT count(*) FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.member"
            " WHERE r.rolname = 'policy_intruder'"
        )
        assert await cursor.fetchone() == (0,)
        await connection.execute("DROP ROLE policy_intruder")


async def test_a_scratch_role_left_by_a_killed_setup_is_dropped_by_the_next_one(
    policy: None,
    test_database_url: str,
    policy_gateway_url: str,
    policy_approver_url: str,
    policy_auditor_url: str,
) -> None:
    async with await _as(test_database_url) as connection:
        cursor = await connection.execute(
            "SELECT 'policy_install_' || left(md5(current_database()), 12)"
        )
        row = await cursor.fetchone()
        assert row is not None
        scratch = str(row[0])
        await connection.execute(f"CREATE ROLE {scratch} NOLOGIN".encode())
        await connection.execute(
            f"GRANT UPDATE ON agent_core_approvals TO {scratch}".encode().replace(
                b"ON agent", b"ON policy.agent"
            )
        )

    await setup_policy(
        test_database_url,
        PolicyPasswords(
            password_of(policy_gateway_url),
            password_of(policy_approver_url),
            password_of(policy_auditor_url),
        ),
    )

    async with await _as(test_database_url) as connection:
        cursor = await connection.execute(
            "SELECT count(*) FROM pg_roles WHERE rolname = %s", (scratch,)
        )
        assert await cursor.fetchone() == (0,)


async def test_a_setup_waits_for_the_lock_only_so_long(test_database_url: str) -> None:
    async with (
        await _as(test_database_url) as holder,
        await _as(test_database_url) as waiter,
        advisory_lock(holder, 7_165_209_999),
    ):
        started = anyio.current_time()
        with pytest.raises(errors.LockNotAvailable):
            async with advisory_lock(waiter, 7_165_209_999, wait="300ms"):
                pass
        assert anyio.current_time() - started < 3
