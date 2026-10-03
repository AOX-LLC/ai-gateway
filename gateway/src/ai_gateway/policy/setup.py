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
    APPROVERS_TABLE,
    ARGUMENTS_PURGE_FUNCTION,
    ARGUMENTS_RETENTION_DAYS,
    ARGUMENTS_TABLE,
    AUDIT_TABLE,
    AUDITOR_ROLE,
    DASHBOARD_VIEW,
    GATEWAY_ROLE,
    ROLES,
    SCHEMA,
    policy_url,
)
from ai_gateway.telemetry import READER_ROLE
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
        await _ensure_approval_tables(connection)
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
        (ARGUMENTS_TABLE, "INSERT"),  # it stores the arguments and can never read them back
    ),
    APPROVER_ROLE: (
        (AUDIT_TABLE, "SELECT, INSERT"),
        (APPROVALS_TABLE, "SELECT, UPDATE"),
        (ARGUMENTS_TABLE, "SELECT"),
        (APPROVERS_TABLE, "SELECT"),
    ),
    AUDITOR_ROLE: ((AUDIT_TABLE, "SELECT"),),  # arguments are never in the audit trail
}

_APPROVAL_TABLES = f"""
CREATE TABLE IF NOT EXISTS {ARGUMENTS_TABLE} (
    request_id TEXT PRIMARY KEY REFERENCES {APPROVALS_TABLE} (id),
    arguments_json TEXT NOT NULL CHECK (length(arguments_json) <= 65536),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS {ARGUMENTS_TABLE}_created ON {ARGUMENTS_TABLE} (created_at);
CREATE TABLE IF NOT EXISTS {APPROVERS_TABLE} (
    id TEXT PRIMARY KEY CHECK (id ~ '^[a-z][a-z0-9._-]{{0,62}}$'),
    display_name TEXT NOT NULL CHECK (length(display_name) BETWEEN 1 AND 100),
    roles TEXT[] NOT NULL CHECK (cardinality(roles) > 0),
    active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

_PURGE_FUNCTION = f"""
CREATE OR REPLACE FUNCTION {ARGUMENTS_PURGE_FUNCTION}() RETURNS bigint
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    WITH purged AS (
        DELETE FROM {SCHEMA}.{ARGUMENTS_TABLE}
        WHERE created_at < now() - interval '{ARGUMENTS_RETENTION_DAYS} days' RETURNING 1
    ) SELECT count(*) FROM purged
$$
"""  # noqa: S608 - fixed names and no input: a function body, not a query

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
        -- Only the gateway asks for approvals (the table's owner and a superuser, as below).
        IF current_user <> '{GATEWAY_ROLE}' AND NOT (
            current_user = (
                SELECT pg_get_userbyid(c.relowner) FROM pg_class c WHERE c.oid = TG_RELID)
            OR (SELECT r.rolsuper FROM pg_roles r WHERE r.rolname = current_user)
        ) THEN
            RAISE EXCEPTION 'only the gateway role may create an approval request';
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
            AND ((NEW.status = 'approved' AND NEW.decision IS NOT DISTINCT FROM 'approve')
                 OR (NEW.status = 'rejected' AND NEW.decision IS NOT DISTINCT FROM 'reject'))
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


_DASHBOARD_VIEW = f"""
CREATE OR REPLACE VIEW {DASHBOARD_VIEW} AS
SELECT id, action, requested_by AS client_actor, status, decision, resolved_by,
       created_at::timestamptz AS created_at, expires_at::timestamptz AS expires_at,
       resolved_at::timestamptz AS resolved_at, consumed_at::timestamptz AS consumed_at
FROM {APPROVALS_TABLE}
"""  # noqa: S608 - fixed names and no input

_DASHBOARD_GRANT = f"""
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{READER_ROLE}') THEN
        REVOKE ALL ON ALL TABLES IN SCHEMA {SCHEMA} FROM {READER_ROLE};
        GRANT USAGE ON SCHEMA {SCHEMA} TO {READER_ROLE};
        GRANT SELECT ON {SCHEMA}.{DASHBOARD_VIEW} TO {READER_ROLE};
    END IF;
END $$
"""  # noqa: S608 - fixed names and no input


async def _ensure_approval_tables(connection: AsyncConnection) -> None:
    """The tables this gateway adds to agent-core's: arguments for the approver, and approvers."""
    async with connection.transaction():
        await connection.execute(
            sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(SCHEMA))
        )
        await connection.execute(_APPROVAL_TABLES.encode())
        await connection.execute(_PURGE_FUNCTION.encode())
        await connection.execute(_DASHBOARD_VIEW.encode())
        # The dashboard's reader sees the view and nothing else in this schema. If its role does
        # not exist yet (telemetry-setup runs first in Compose), there is nothing to grant.
        await connection.execute(_DASHBOARD_GRANT.encode())


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
        # the member's own name; nobody is a member of these roles. A grant is revoked as the role
        # that made it (PostgreSQL ignores a revoke by anyone else), and the check is made again.
        await _revoke_memberships(connection)
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
        # The purge runs with its owner's rights, so who may call it is the whole control.
        purge = sql.SQL("{}.{}()").format(schema, sql.Identifier(ARGUMENTS_PURGE_FUNCTION))
        await connection.execute(sql.SQL("REVOKE ALL ON FUNCTION {} FROM PUBLIC").format(purge))
        for role in ROLES:
            verb = (
                "GRANT EXECUTE ON FUNCTION {} TO {}"
                if role == GATEWAY_ROLE
                else ("REVOKE ALL ON FUNCTION {} FROM {}")
            )
            await connection.execute(sql.SQL(verb).format(purge, sql.Identifier(role)))


_MEMBERSHIPS = (
    "SELECT member.rolname, parent.rolname, grantor.rolname FROM pg_auth_members m"
    " JOIN pg_roles parent ON parent.oid = m.roleid"
    " JOIN pg_roles member ON member.oid = m.member"
    " JOIN pg_roles grantor ON grantor.oid = m.grantor WHERE parent.rolname = ANY(%s)"
)


async def _memberships(connection: AsyncConnection) -> list[tuple[str, str, str]]:
    cursor = await connection.execute(_MEMBERSHIPS, (list(ROLES),))
    return [(str(a), str(b), str(c)) for a, b, c in await cursor.fetchall()]


async def _revoke_memberships(connection: AsyncConnection) -> None:
    for member, parent, grantor in await _memberships(connection):
        await connection.execute(
            sql.SQL("REVOKE {} FROM {} GRANTED BY {} CASCADE").format(
                sql.Identifier(parent), sql.Identifier(member), sql.Identifier(grantor)
            )
        )
    remaining = await _memberships(connection)
    if remaining:
        raise RuntimeError(
            f"{len(remaining)} membership(s) of the policy roles could not be revoked"
        )
