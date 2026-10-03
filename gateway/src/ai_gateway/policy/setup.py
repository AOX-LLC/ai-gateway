"""Set up the policy schema: roles, agent-core's tables, grants, and the approval guard.

Runs as the database owner (`gateway-admin policy-setup`) and is safe to repeat, and safe to run
twice at once (a lock serialises runs). agent-core's own installer is not repeatable (it creates
tables), so it runs only when the tables are missing, and for a scratch role that cannot log in and
is dropped afterwards: the installer grants its app role UPDATE on approvals at once, and the guard
trigger does not exist yet, so a real role must never hold that grant. The grants and the guard
are applied together, in one transaction, guard first, on every run.
"""

import logging
from dataclasses import dataclass

import anyio
from aox_agent_core.storage import install_postgres_schema
from psycopg import AsyncConnection, sql

from ai_gateway.policy import (
    APPROVALS_TABLE,
    APPROVER_ROLE,
    AUDIT_TABLE,
    AUDITOR_ROLE,
    GATEWAY_ROLE,
    ROLES,
    SCHEMA,
    policy_url,
)
from mcp_common.roles import (
    advisory_lock,
    ensure_role,
    ensure_schema,
    reset_role,
    restrict_database_access,
    revoke_role_access,
)

logger = logging.getLogger(__name__)

_SETUP_LOCK = 7_165_201_002
"""The advisory lock that serialises policy setups."""
_INSTALL_ROLE_PREFIX = "policy_install_"


@dataclass(frozen=True)
class PolicyPasswords:
    gateway: str
    approver: str
    auditor: str


async def setup_policy(owner_url: str, passwords: PolicyPasswords) -> None:
    """Create the roles and the schema, install agent-core's tables once, and grant."""
    role_passwords = (
        (GATEWAY_ROLE, passwords.gateway),
        (APPROVER_ROLE, passwords.approver),
        (AUDITOR_ROLE, passwords.auditor),
    )
    for role, password in role_passwords:
        if not password:
            raise ValueError(f"the password of {role} is empty")
    async with (
        await AsyncConnection.connect(owner_url, autocommit=True) as connection,
        advisory_lock(connection, _SETUP_LOCK),
    ):
        for role, password in role_passwords:
            await ensure_role(connection, password, role)
        await ensure_schema(connection, SCHEMA)
        # A scratch role left by a setup that was killed after the install holds UPDATE on
        # approvals: it goes whether or not the tables are missing.
        await _drop_scratch_role(connection)
        if await _tables_exist(connection):
            logger.info("agent-core's audit and approval tables are already installed")
        else:
            await _install_tables(connection, owner_url)
        await grant_policy_access(connection)
        await restrict_database_access(connection, list(ROLES))


async def _scratch_role(connection: AsyncConnection) -> str:
    """The scratch role's name. Roles belong to the cluster, not the database, and a lock is per
    database, so setups of two databases must not share one."""
    cursor = await connection.execute("SELECT md5(current_database())")
    row = await cursor.fetchone()
    if row is None:
        raise RuntimeError("md5 returned no row")
    return f"{_INSTALL_ROLE_PREFIX}{str(row[0])[:12]}"


async def _drop_scratch_role(connection: AsyncConnection) -> None:
    name = await _scratch_role(connection)
    if await _role_exists(connection, name):
        await connection.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(name)))
        await connection.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(name)))


async def _install_tables(connection: AsyncConnection, owner_url: str) -> None:
    """Install agent-core's tables for a scratch role that cannot log in, then drop the role (and
    with it every grant the installer made), so no real role holds a grant before its guard."""
    name = await _scratch_role(connection)
    await _drop_scratch_role(connection)
    await connection.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(name)))
    try:
        # The installer is synchronous (agent-core's drivers are), so it runs on a thread.
        await anyio.to_thread.run_sync(
            lambda: install_postgres_schema(policy_url(owner_url), app_role=name)
        )
    finally:
        await _drop_scratch_role(connection)
    logger.info("installed agent-core's audit and approval tables in schema %s", SCHEMA)


async def _role_exists(connection: AsyncConnection, role: str) -> bool:
    cursor = await connection.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
    return await cursor.fetchone() is not None


async def _tables_exist(connection: AsyncConnection) -> bool:
    cursor = await connection.execute(
        "SELECT to_regclass(%s) IS NOT NULL, to_regclass(%s) IS NOT NULL",
        (f"{SCHEMA}.{AUDIT_TABLE}", f"{SCHEMA}.{APPROVALS_TABLE}"),
    )
    row = await cursor.fetchone()
    if row is None:
        raise RuntimeError("to_regclass returned no row")
    if row[0] != row[1]:
        raise RuntimeError("only one of agent-core's two tables exists; refusing to continue")
    return bool(row[0])


# What the three roles may do, and nothing else. The approval table's finer limits are the
# guard trigger below: a role's right to UPDATE is a right to change any column.
_GRANTS = {
    GATEWAY_ROLE: (
        (AUDIT_TABLE, "SELECT, INSERT"),
        (APPROVALS_TABLE, "SELECT, INSERT, UPDATE"),
    ),
    APPROVER_ROLE: (
        (AUDIT_TABLE, "SELECT, INSERT"),
        (APPROVALS_TABLE, "SELECT, UPDATE"),
    ),
    AUDITOR_ROLE: ((AUDIT_TABLE, "SELECT"),),
}

_NOW_TEXT = "to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"')"
"""agent-core stores times as UTC text of this shape, so they compare as text."""

_GUARD_FUNCTION = f"""
CREATE OR REPLACE FUNCTION {APPROVALS_TABLE}_guard() RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        -- A request is created pending; nobody may create one that is already decided.
        IF NEW.status <> 'pending' OR NEW.decision IS NOT NULL OR NEW.resolved_by IS NOT NULL
           OR NEW.resolved_at IS NOT NULL OR NEW.consumed_at IS NOT NULL THEN
            RAISE EXCEPTION 'an approval request can only be created pending';
        END IF;
        RETURN NEW;
    END IF;

    IF current_user = '{GATEWAY_ROLE}' THEN
        -- The gateway uses an approval: it may only consume one that was approved and has not
        -- expired, and change nothing else about it. It can never decide a request.
        IF NOT (OLD.status = 'approved' AND NEW.status = 'consumed' AND NEW.consumed_at IS NOT NULL
            AND OLD.expires_at > {_NOW_TEXT}
            AND (NEW.id, NEW.action, NEW.summary, NEW.payload_sha256, NEW.requested_by,
                 NEW.required_role, NEW.created_at, NEW.expires_at, NEW.decision, NEW.resolved_by,
                 NEW.resolved_at, NEW.reason, NEW.run_context)
                IS NOT DISTINCT FROM
                (OLD.id, OLD.action, OLD.summary, OLD.payload_sha256, OLD.requested_by,
                 OLD.required_role, OLD.created_at, OLD.expires_at, OLD.decision, OLD.resolved_by,
                 OLD.resolved_at, OLD.reason, OLD.run_context)) THEN
            RAISE EXCEPTION 'the gateway role may only consume an approved request';
        END IF;
    ELSIF current_user = '{APPROVER_ROLE}' THEN
        -- An approver decides a pending, unexpired request, once, and says who and when. The
        -- request itself (what it authorises, who asked, when it expires) stays as it was.
        IF NOT (OLD.status = 'pending' AND OLD.expires_at > {_NOW_TEXT}
            AND ((NEW.status = 'approved' AND NEW.decision = 'approve')
                 OR (NEW.status = 'rejected' AND NEW.decision = 'reject'))
            AND NEW.resolved_by IS NOT NULL AND NEW.resolved_at IS NOT NULL
            AND NEW.consumed_at IS NULL
            AND (NEW.id, NEW.action, NEW.summary, NEW.payload_sha256, NEW.requested_by,
                 NEW.required_role, NEW.created_at, NEW.expires_at, NEW.run_context)
                IS NOT DISTINCT FROM
                (OLD.id, OLD.action, OLD.summary, OLD.payload_sha256, OLD.requested_by,
                 OLD.required_role, OLD.created_at, OLD.expires_at, OLD.run_context)) THEN
            RAISE EXCEPTION 'the approver role may only decide a pending request';
        END IF;
    ELSIF NOT (
        current_user = (SELECT pg_get_userbyid(c.relowner) FROM pg_class c WHERE c.oid = TG_RELID)
        OR (SELECT r.rolsuper FROM pg_roles r WHERE r.rolname = current_user)
    ) THEN
        -- Any other role, including one that merely inherits a policy role's privileges (the guard
        -- keys on current_user, which is the member, not the role it inherits from): refused.
        RAISE EXCEPTION 'only the gateway and approver roles may change an approval request';
    END IF;
    RETURN NEW;
END $$
"""  # noqa: S608 - fixed names and no input: a trigger body, not a query


async def grant_policy_access(connection: AsyncConnection) -> None:
    """Make the three roles' grants exactly these, and (re)install the approval guard.

    The tables and roles must exist. Everything runs in one transaction, so there is never a moment
    when a role holds a grant and the guard is missing, and the guard is created before any grant.
    Each role's attributes and memberships are reset and everything it holds in the schema is
    revoked first, so a widened grant is narrowed again."""
    schema = sql.Identifier(SCHEMA)
    async with connection.transaction():
        # The guard function and trigger belong to the policy schema, like the table they guard.
        await connection.execute(sql.SQL("SET LOCAL search_path TO {}").format(schema))
        await connection.execute(_GUARD_FUNCTION.encode())
        await connection.execute(
            f"DROP TRIGGER IF EXISTS {APPROVALS_TABLE}_guard ON {APPROVALS_TABLE}".encode()
        )
        await connection.execute(
            f"CREATE TRIGGER {APPROVALS_TABLE}_guard BEFORE INSERT OR UPDATE ON {APPROVALS_TABLE}"
            f" FOR EACH ROW EXECUTE FUNCTION {APPROVALS_TABLE}_guard()".encode()
        )
        # A role that is a member of a policy role inherits its privileges, but the guard keys on
        # the member's own name; nobody is a member of these roles.
        cursor = await connection.execute(
            "SELECT member.rolname, parent.rolname FROM pg_auth_members m"
            " JOIN pg_roles parent ON parent.oid = m.roleid"
            " JOIN pg_roles member ON member.oid = m.member WHERE parent.rolname = ANY(%s)",
            (list(ROLES),),
        )
        for member, parent in await cursor.fetchall():
            await connection.execute(
                sql.SQL("REVOKE {} FROM {}").format(sql.Identifier(parent), sql.Identifier(member))
            )
        for role in ROLES:
            await reset_role(connection, role)
            await revoke_role_access(connection, SCHEMA, role)
            await connection.execute(
                sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(schema, sql.Identifier(role))
            )
            for table, privileges in _GRANTS[role]:
                await connection.execute(
                    sql.SQL("GRANT {} ON {}.{} TO {}").format(
                        sql.SQL(privileges), schema, sql.Identifier(table), sql.Identifier(role)
                    )
                )
