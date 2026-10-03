"""The policy schema: what each role can do to the audit log and the approval queue."""

from uuid import uuid4

import psycopg
import pytest
from aox_agent_core.approvals import Decision, Principal, PrincipalKind, SQLApprovalQueue
from aox_agent_core.audit import AuditEvent, SQLAuditLog
from aox_agent_core.errors import ApprovalError, ApprovalPayloadMismatchError
from aox_agent_core.storage import open_database
from psycopg import errors
from pydantic import SecretStr

from ai_gateway.policy import APPROVALS_TABLE, AUDIT_TABLE, ROLES, SCHEMA, policy_url
from ai_gateway.policy.setup import PolicyPasswords, grant_policy_access, setup_policy
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
