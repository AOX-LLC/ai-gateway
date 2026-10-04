"""The policy schema: what each role can do to the audit log and the approval queue."""

from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import anyio
import psycopg
import pytest
from aox_agent_core.approvals import (
    Decision,
    Principal,
    PrincipalKind,
    RoleApproverPolicy,
    SQLApprovalQueue,
)
from aox_agent_core.audit import AuditEvent
from aox_agent_core.errors import ApprovalError, ApprovalPayloadMismatchError, ConfigError
from aox_agent_core.storage import bind_approver_login, open_database
from psycopg import errors
from pydantic import SecretStr

from ai_gateway.policy import (
    APPROVALS_TABLE,
    AUDIT_TABLE,
    ROLES,
    SCHEMA,
    approval_queue_on,
    audit_log_on,
    policy_url,
)
from ai_gateway.policy.setup import (
    PolicyPasswords,
    grant_policy_access,
    setup_policy,
)
from mcp_common.roles import advisory_lock
from tests.conftest import AIDEN, password_of

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

_REFUSED = (errors.RaiseException, errors.InsufficientPrivilege)
"""a3 refuses in two layers: column grants (InsufficientPrivilege) and the guard trigger."""
# ruff: noqa: E501 - a3 accepts only its canonical timestamp text (long); the SQL is fixed text
TS = "to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"')"
"""A timestamp the way agent-core writes it: UTC text with six decimals and a Z."""
PAYLOAD = {"ticket_id": "TKT-000001", "status": "closed"}
ACTION = "tickets__change_status"
CLIENT = Principal(id=f"client:{uuid4()}", kind=PrincipalKind.AGENT)
HUMAN = Principal(id=f"human:{AIDEN}", kind=PrincipalKind.HUMAN, roles=frozenset({"approver"}))


def _queue(url: str) -> SQLApprovalQueue:
    database = open_database(SecretStr(policy_url(url)))
    return approval_queue_on(
        database, policy=RoleApproverPolicy(roles_by_action={ACTION: "approver"})
    )


def _event() -> AuditEvent:
    return AuditEvent(
        action="gateway.tool_call",
        actor_id=f"client:{uuid4()}",
        subject_id="tickets__get_ticket",
        payload={"request_id": str(uuid4()), "outcome": "forwarded"},
    )


async def _as(url: str) -> psycopg.AsyncConnection:
    return await psycopg.AsyncConnection.connect(policy_url(url), autocommit=True)


async def _pending(gateway_url: str, ttl_seconds: int = 300, *, other: bool = False) -> object:
    return await _queue(gateway_url).submit(
        action=ACTION,
        summary="close a ticket",
        # `other`: a request of its own, since there is one open request per client, tool and payload
        payload={**PAYLOAD, "n": str(uuid4())} if other else PAYLOAD,
        requested_by=CLIENT,
        required_role="approver",
        ttl_seconds=ttl_seconds,
    )


# --- setup -----------------------------------------------------------------------------------


async def test_setup_installs_the_tables_in_the_policy_schema_once_and_is_safe_to_repeat(
    policy: None,
    test_database_url: str,
    policy_gateway_url: str,
    policy_auditor_url: str,
) -> None:
    await _queue(policy_gateway_url).database.run(lambda s: s.execute("SELECT 1"))
    await audit_log_on(open_database(SecretStr(policy_url(policy_gateway_url)))).append(_event())

    await setup_policy(
        test_database_url,
        PolicyPasswords(password_of(policy_gateway_url), password_of(policy_auditor_url)),
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


async def test_two_setups_at_once_do_not_collide(
    test_database_url: str,
    policy_gateway_url: str,
    policy_approver_url: str,
    policy_auditor_url: str,
) -> None:
    async with await _as(test_database_url) as connection:
        await connection.execute("DROP SCHEMA IF EXISTS policy CASCADE")
    passwords = PolicyPasswords(password_of(policy_gateway_url), password_of(policy_auditor_url))
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


@pytest.mark.parametrize("empty", ["gateway", "auditor"])
async def test_setup_refuses_an_empty_password(test_database_url: str, empty: str) -> None:
    passwords = {"gateway": "g", "auditor": "u", **{empty: ""}}

    with pytest.raises(ValueError, match="empty"):
        await setup_policy(test_database_url, PolicyPasswords(**passwords))


# --- the audit log -----------------------------------------------------------------------------


async def test_the_gateway_can_append_and_read_but_nobody_can_change_or_remove(
    policy: None, policy_gateway_url: str, test_database_url: str
) -> None:
    log = audit_log_on(open_database(SecretStr(policy_url(policy_gateway_url))))
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
    await audit_log_on(open_database(SecretStr(policy_url(policy_gateway_url)))).append(_event())
    auditor = audit_log_on(open_database(SecretStr(policy_url(policy_auditor_url))))

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

    with pytest.raises(Exception, match="cannot decide requests"):
        await _queue(policy_gateway_url).resolve(
            request.id,  # type: ignore[attr-defined]
            decision=Decision.APPROVE,
            principal=HUMAN,  # a policy that would allow it is not enough: the database refuses
        )
    async with await _as(policy_gateway_url) as connection:
        with pytest.raises(_REFUSED):
            await connection.execute(
                "UPDATE agent_core_approvals SET status = 'approved', decision = 'approve',"
                f" resolved_by = 'human:{AIDEN}', resolved_at = to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"') WHERE id = %s",
                (str(request.id),),  # type: ignore[attr-defined]
            )


async def test_nobody_can_create_a_request_that_is_already_decided(
    policy: None, policy_gateway_url: str, test_database_url: str
) -> None:
    insert = (
        "INSERT INTO agent_core_approvals (id, action, summary, payload_sha256, requested_by,"
        " required_role, created_at, expires_at, status, decision, resolved_by, resolved_at)"
        " VALUES (gen_random_uuid()::text, 'a', 's', repeat('a', 64), 'client:b', 'approver',"
        f" to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"'), to_char((now() + interval '1 hour') AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"'), 'approved', 'approve', 'human:{AIDEN}',"
        " to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"'))"
    )
    for url in (policy_gateway_url, test_database_url):
        async with await _as(url) as connection:
            with pytest.raises(_REFUSED):
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
        with pytest.raises(_REFUSED):
            await connection.execute(
                b"INSERT INTO agent_core_approvals (id, action, summary, payload_sha256,"
                b" requested_by, required_role, created_at, expires_at, status)"
                b" VALUES (gen_random_uuid()::text,"
                b" 'a', 's', repeat('a', 64), 'c', 'approver', to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"'),"
                b" to_char((now() + interval '1 hour') AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"'), 'pending')"
            )
        with pytest.raises(_REFUSED):
            await connection.execute(
                "UPDATE agent_core_approvals SET status = 'consumed', consumed_at = to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"')"
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


async def _approved(gateway_url: str, approver_url: str, ttl_seconds: int = 300) -> object:
    request = await _pending(gateway_url, ttl_seconds)
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
        "expires_at = to_char((now() + interval '9 days') AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"')",
        "reason = 'because'",
    ],
)
async def test_the_gateway_can_consume_an_approval_and_change_nothing_else_in_the_same_update(
    policy: None, policy_gateway_url: str, policy_approver_url: str, assignment: str
) -> None:
    request = await _approved(policy_gateway_url, policy_approver_url)

    with pytest.raises(_REFUSED):
        await _run(
            policy_gateway_url,
            "UPDATE agent_core_approvals SET status = 'consumed', consumed_at = to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"'),"
            f" {assignment} WHERE id = %s",
            str(request.id),  # type: ignore[attr-defined]
        )


async def test_the_gateway_cannot_consume_an_approval_that_has_expired(
    policy: None, policy_gateway_url: str, policy_approver_url: str, test_database_url: str
) -> None:
    request = await _approved(policy_gateway_url, policy_approver_url, ttl_seconds=1)
    await anyio.sleep(1.2)  # a3 forbids changing a lifetime, even for the owner: let it pass

    with pytest.raises(_REFUSED):
        await _run(
            policy_gateway_url,
            "UPDATE agent_core_approvals SET status = 'consumed', consumed_at = to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"')"
            " WHERE id = %s",
            str(request.id),  # type: ignore[attr-defined]
        )


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("payload_sha256", "repeat('b', 64)"),  # approve something other than what was asked
        ("action", "'tickets__assign'"),
        ("requested_by", f"'human:{AIDEN}'"),  # so a request can look self-made
        (
            "expires_at",
            "to_char((now() + interval '9 days') AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"')",
        ),
        ("required_role", "'nobody'"),
        ("decision", "'reject'"),  # a decision that contradicts the status
        ("decision", "NULL"),  # no decision at all: NULL must not slip past the comparison
        ("resolved_by", "NULL"),
        ("resolved_at", "NULL"),
        ("consumed_at", "to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"')"),
    ],
)
async def test_the_approver_decides_a_request_and_changes_nothing_else_in_the_same_update(
    policy: None, policy_gateway_url: str, policy_approver_url: str, column: str, value: str
) -> None:
    request = await _pending(policy_gateway_url)
    assignments = {
        "status": "'approved'",
        "decision": "'approve'",
        "resolved_by": f"'human:{AIDEN}'",
        "resolved_at": "to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"')",
        column: value,  # the one thing that should not be allowed
    }
    changes = ", ".join(f"{name} = {expression}" for name, expression in assignments.items())

    with pytest.raises(_REFUSED):
        await _run(
            policy_approver_url,
            f"UPDATE agent_core_approvals SET {changes} WHERE id = %s",
            str(request.id),  # type: ignore[attr-defined]
        )


async def test_the_statements_the_attack_tests_alter_are_valid_when_nothing_is_altered(
    policy: None, policy_gateway_url: str, policy_approver_url: str
) -> None:
    """The control for every refusal above: the same statements, with nothing wrong in them,
    succeed. A refusal then comes from the clause under test, not from a malformed statement."""
    request = await _pending(policy_gateway_url)
    await _run(
        policy_approver_url,
        "UPDATE agent_core_approvals SET status = 'approved', decision = 'approve',"
        f" resolved_by = 'human:{AIDEN}', resolved_at = {TS} WHERE id = %s",
        str(request.id),  # type: ignore[attr-defined]
    )
    await _run(
        policy_gateway_url,
        f"UPDATE agent_core_approvals SET status = 'consumed', consumed_at = {TS} WHERE id = %s",
        str(request.id),  # type: ignore[attr-defined]
    )

    stored = await _queue(policy_approver_url).get(request.id)  # type: ignore[attr-defined]
    assert stored.status.value == "consumed"
    assert stored.resolved_by == f"human:{AIDEN}"


async def test_the_approver_cannot_decide_a_request_twice_or_after_it_expired(
    policy: None, policy_gateway_url: str, policy_approver_url: str, test_database_url: str
) -> None:
    decided = await _approved(policy_gateway_url, policy_approver_url)
    with pytest.raises(_REFUSED):
        await _run(
            policy_approver_url,
            "UPDATE agent_core_approvals SET status = 'rejected', decision = 'reject'"
            " WHERE id = %s",
            str(decided.id),  # type: ignore[attr-defined]
        )

    expired = await _pending(policy_gateway_url, ttl_seconds=1, other=True)
    await anyio.sleep(1.2)
    with pytest.raises(_REFUSED):
        await _run(
            policy_approver_url,
            "UPDATE agent_core_approvals SET status = 'approved', decision = 'approve',"
            f" resolved_by = 'human:{AIDEN}', resolved_at = to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"') WHERE id = %s",
            str(expired.id),  # type: ignore[attr-defined]
        )


async def test_an_upsert_cannot_decide_a_request_either(
    policy: None, policy_gateway_url: str
) -> None:
    request = await _pending(policy_gateway_url)

    with pytest.raises(_REFUSED):
        await _run(
            policy_gateway_url,
            "INSERT INTO agent_core_approvals (id, action, summary, payload_sha256, requested_by,"
            " required_role, created_at, expires_at, status) VALUES (%s, 'a', 's', repeat('a', 64),"
            " 'c', 'approver', to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"'), to_char((now() + interval '1 hour') AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"'), 'pending')"
            " ON CONFLICT (id) DO UPDATE SET status = 'approved', decision = 'approve',"
            f" resolved_by = 'human:{AIDEN}', resolved_at = to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"')",
            str(request.id),  # type: ignore[attr-defined]
        )


async def _login_member_of(
    owner_url: str, parent: str, name: str = "policy_intruder"
) -> tuple[str, psycopg.AsyncConnection]:
    """A role that can log in and is a member of `parent`, and a connection as that role."""
    async with await _as(owner_url) as connection:
        await connection.execute(f"DROP OWNED BY {name}".encode()) if await _exists(
            connection, name
        ) else None
        await connection.execute(f"DROP ROLE IF EXISTS {name}".encode())
        await connection.execute(f"CREATE ROLE {name} LOGIN PASSWORD 'intruder-pw'".encode())
        await connection.execute(f"GRANT {parent} TO {name}".encode())
    parts = urlsplit(owner_url)
    netloc = f"{name}:intruder-pw@{parts.hostname}:{parts.port}"
    return name, await psycopg.AsyncConnection.connect(
        policy_url(urlunsplit(parts._replace(netloc=netloc))), autocommit=True
    )


async def _exists(connection: psycopg.AsyncConnection, role: str) -> bool:
    cursor = await connection.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
    return await cursor.fetchone() is not None


async def test_a_member_of_the_gateway_role_is_the_requester_side_and_can_never_approve(
    policy: None, policy_gateway_url: str, test_database_url: str
) -> None:
    """a3 counts a member of a policy role as that role's side (3a refused such a member by name).
    A member of the requester role has the requester's powers and not the approver's: it cannot
    approve. `policy-setup` removes the membership on its next run (tested below)."""
    request = await _pending(policy_gateway_url)
    _, connection = await _login_member_of(test_database_url, "policy_gateway")
    async with connection:
        with pytest.raises(_REFUSED):
            await connection.execute(
                "UPDATE agent_core_approvals SET status = 'approved', decision = 'approve',"
                f" resolved_by = 'human:{AIDEN}', resolved_at = {TS} WHERE id = %s",
                (str(request.id),),  # type: ignore[attr-defined]
            )
        await connection.execute(  # the control: the requester's own change is allowed
            f"UPDATE agent_core_approvals SET status = 'cancelled', closed_at = {TS} WHERE id = %s",
            (str(request.id),),  # type: ignore[attr-defined]
        )
    stored = await _queue(policy_gateway_url).get(request.id)  # type: ignore[attr-defined]
    assert stored.status.value == "cancelled"
    await _drop_role(test_database_url, "policy_intruder")


async def test_a_member_of_the_approver_role_holds_its_privileges_until_setup_removes_the_membership(
    policy: None,
    policy_gateway_url: str,
    policy_approver_url: str,
    policy_auditor_url: str,
    test_database_url: str,
) -> None:
    """The gap in the other direction, stated as it is: membership of the approver role is the
    approver's capability (3a would have let such a member `SET ROLE` to the approver and do the
    same). What bounds it is that setup removes every membership in the policy roles, each run."""
    _, connection = await _login_member_of(test_database_url, "policy_approver")
    async with connection:
        cursor = await connection.execute("SELECT count(*) FROM agent_core_approvals")
        assert await cursor.fetchone() == (0,), "it inherits the approver's rights"
    await _setup(test_database_url, policy_gateway_url, policy_auditor_url)

    # Its only way into the database was the role it was a member of: without it, it cannot connect.
    parts = urlsplit(test_database_url)
    netloc = f"policy_intruder:intruder-pw@{parts.hostname}:{parts.port}"
    with pytest.raises(psycopg.OperationalError, match="CONNECT"):
        await psycopg.AsyncConnection.connect(policy_url(urlunsplit(parts._replace(netloc=netloc))))
    await _drop_role(test_database_url, "policy_intruder")


async def test_an_approval_that_plain_sql_made_stops_setup_and_leaves_the_roles_their_grants(
    policy: None,
    policy_gateway_url: str,
    policy_approver_url: str,
    policy_auditor_url: str,
    test_database_url: str,
) -> None:
    """a3's installer refuses to go on when an approved request has no approver's decision on
    record and the audit log holds none to compare with. That must not leave the gateway and the
    approver with no access: the installer runs before their grants are cleared."""
    request = await _pending(policy_gateway_url)
    login, connection = await _login_member_of(test_database_url, "policy_approver")
    # Binding is on, so plain SQL can only approve as the principal the owner mapped to the login.
    bind_approver_login(test_database_url, login=login, principal="human:x", schema="policy")
    async with connection:
        await connection.execute(
            "UPDATE agent_core_approvals SET status = 'approved', decision = 'approve',"
            f" resolved_by = 'human:x', resolved_at = {TS} WHERE id = %s",
            (str(request.id),),  # type: ignore[attr-defined]
        )
    await _drop_role(test_database_url, "policy_intruder")

    with pytest.raises(ConfigError, match=r"approval\.resolved"):
        await _setup(test_database_url, policy_gateway_url, policy_auditor_url)

    stored = await _queue(policy_gateway_url).get(request.id)  # type: ignore[attr-defined]
    assert stored.status.value == "approved", "nothing was changed"
    await _queue(policy_approver_url).list_pending(HUMAN)  # the roles can still work


async def _setup(owner: str, gateway: str, auditor: str) -> None:
    await setup_policy(owner, PolicyPasswords(password_of(gateway), password_of(auditor)))


async def _drop_role(owner_url: str, name: str) -> None:
    async with await _as(owner_url) as connection:
        if await _exists(connection, name):
            await connection.execute(f"DROP OWNED BY {name}".encode())
            await connection.execute(f"DROP ROLE {name}".encode())


async def test_a_membership_granted_by_another_role_is_revoked_and_one_that_stays_is_an_error(
    policy: None, test_database_url: str
) -> None:
    async with await _as(test_database_url) as connection:
        await connection.execute("DROP ROLE IF EXISTS policy_intruder")
        await connection.execute("DROP ROLE IF EXISTS policy_grantor")
        await connection.execute("CREATE ROLE policy_intruder NOLOGIN")
        await connection.execute("CREATE ROLE policy_grantor NOLOGIN CREATEROLE")
        try:
            await connection.execute("GRANT policy_approver TO policy_grantor WITH ADMIN OPTION")
            await connection.execute(
                "GRANT policy_approver TO policy_intruder GRANTED BY policy_grantor"
            )
            await grant_policy_access(connection)
            cursor = await connection.execute(
                "SELECT count(*) FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.member"
                " WHERE r.rolname = 'policy_intruder'"
            )
            assert await cursor.fetchone() == (0,)
        finally:
            await connection.execute("DROP OWNED BY policy_intruder, policy_grantor")
            await connection.execute("DROP ROLE policy_intruder, policy_grantor")


async def test_a_role_that_only_inherits_the_gateway_role_cannot_create_a_request(
    policy: None, test_database_url: str
) -> None:
    async with await _as(test_database_url) as connection:
        await connection.execute("DROP ROLE IF EXISTS policy_intruder")
        await connection.execute("CREATE ROLE policy_intruder NOLOGIN")
        await connection.execute("GRANT policy_gateway TO policy_intruder")
        await connection.execute("SET ROLE policy_intruder")
        try:
            with pytest.raises(_REFUSED):
                await connection.execute(
                    b"INSERT INTO agent_core_approvals (id, action, summary, payload_sha256,"
                    b" requested_by, required_role, created_at, expires_at, status)"
                    b" VALUES (gen_random_uuid()::text, 'a', 's', repeat('a', 64), 'c',"
                    b" 'approver', to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"'), to_char((now() + interval '1 hour') AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"'), 'pending')"
                )
        finally:
            await connection.execute("RESET ROLE")
            await connection.execute("DROP OWNED BY policy_intruder")
            await connection.execute("DROP ROLE policy_intruder")


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
