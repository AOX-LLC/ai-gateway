"""Set up the policy schema: roles, agent-core's tables and guard, and what this gateway adds.

Runs as the database owner (`gateway-admin policy-setup`) and is safe to repeat, and safe to run
twice at once (a lock serialises runs). agent-core's installer (v0.1.0a3) creates the audit and
approval tables, grants its requester role (the gateway) and its approver role their layouts, and
installs the guard trigger that enforces who may make each change to an approval, in one
transaction. It is idempotent and upgrades an a2 schema in place, so it runs every time. This module
adds what is the gateway's own: the auditor role's read access, the approver's copy of a write's
arguments, the approvers, the arguments purge and the dashboard's view, and takes back what a role
holds beyond its layout.
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
    LAB_APPROVER_ROLE,
    POLICY_IDLE_IN_TRANSACTION_MS,
    ROLES,
    SCHEMA,
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


@dataclass(frozen=True)
class PolicyPasswords:
    gateway: str
    approver: str
    auditor: str
    lab_approver: str | None = None
    """Set only for a lab stack: creates the lab approver role. Unset, the role is dropped."""


async def setup_policy(owner_url: str, passwords: PolicyPasswords) -> None:
    """Create the roles and the schema, install or upgrade agent-core's tables, and grant."""
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
            await ensure_role(connection, password, role, POLICY_IDLE_IN_TRANSACTION_MS)
            await reset_role(connection, role)
        await ensure_schema(connection, SCHEMA)
        # A role may not create objects in the schema (they could shadow what the owner's setup
        # then runs). USAGE is left: a setup that stops must not leave the roles unable to work.
        for role in ROLES:
            await connection.execute(
                sql.SQL("REVOKE CREATE ON SCHEMA {} FROM {}").format(
                    sql.Identifier(SCHEMA), sql.Identifier(role)
                )
            )
        # The installer refuses roles that are members of each other.
        await _revoke_memberships(connection)
        # Install first: it checks what it needs (the roles, the audit evidence of approvals) and
        # raises before changing anything it cannot undo. Only then are the roles' grants cleared
        # and the same, idempotent install run again to grant each exactly its layout, so a setup
        # that cannot install never leaves the roles with nothing.
        await _install(owner_url)
        await _clear_layout_grants(connection)
        await _install(owner_url)
        await _ensure_approval_tables(connection)
        await grant_policy_access(connection)
        await _set_up_lab_role(connection, passwords.lab_approver)
        await restrict_database_access(
            connection, [*ROLES, *([LAB_APPROVER_ROLE] if passwords.lab_approver else [])]
        )


async def _install(owner_url: str) -> None:
    """Install or upgrade agent-core's tables and guard. The installer is synchronous (agent-core's
    drivers are), so it runs on a thread. An approved, unused request that no approver's decision
    approves (plain SQL could make one under a2) is cancelled, and said so."""
    report = await anyio.to_thread.run_sync(
        lambda: install_postgres_schema(
            owner_url,
            schema=SCHEMA,
            requester_role=GATEWAY_ROLE,
            approver_role=APPROVER_ROLE,
            close_unaudited_approvals=True,
        )
    )
    if report.closed_approvals:
        logger.warning(
            "cancelled %d approved request(s) that no approver's decision approves: %s",
            len(report.closed_approvals),
            ", ".join(report.closed_approvals),
        )
    for grant in report.outside_layout:
        if grant.role not in (AUDITOR_ROLE, READER_ROLE):
            logger.warning("held outside agent-core's layout, to revoke if unused: %s", grant)
    logger.info("agent-core's audit and approval tables are installed in schema %s", SCHEMA)


# What this gateway grants, on its own tables and the audit log for the auditor. The gateway's and
# the approver's rights on agent-core's tables are the installer's layout.
_GRANTS = {
    # It stores the arguments and can never read them back, nor choose when they are purged
    # (`created_at` is the database's): only these two columns.
    GATEWAY_ROLE: ((ARGUMENTS_TABLE, "INSERT (request_id, arguments_json)"),),
    APPROVER_ROLE: ((ARGUMENTS_TABLE, "SELECT"), (APPROVERS_TABLE, "SELECT")),
    AUDITOR_ROLE: ((AUDIT_TABLE, "SELECT"),),  # arguments are never in the audit trail
}
_OWN_TABLES = (ARGUMENTS_TABLE, APPROVERS_TABLE)

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

_FIND_INDEX = f"""
CREATE INDEX IF NOT EXISTS policy_approvals_find
ON {APPROVALS_TABLE} (requested_by, payload_sha256, created_at DESC)
"""  # the gate looks a request up by client and argument hash on every write

_ONE_OPEN_REQUEST = f"""
CREATE UNIQUE INDEX IF NOT EXISTS policy_approvals_one_open
ON {APPROVALS_TABLE} (requested_by, action, payload_sha256)
WHERE status IN ('pending', 'approved')
"""  # at most one open request per client, tool and payload: REPLACE WITH AGENT-CORE A5'S INDEX

_PURGE_FUNCTION = f"""
CREATE OR REPLACE FUNCTION {ARGUMENTS_PURGE_FUNCTION}() RETURNS bigint
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    WITH purged AS (
        DELETE FROM {SCHEMA}.{ARGUMENTS_TABLE}
        WHERE created_at < now() - interval '{ARGUMENTS_RETENTION_DAYS} days' RETURNING 1
    ) SELECT count(*) FROM purged
$$
"""  # noqa: S608 - fixed names and no input: a function body, not a query

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
        # Created and closed to PUBLIC in the one transaction: it runs with its owner's rights.
        await connection.execute(
            f"REVOKE ALL ON FUNCTION {ARGUMENTS_PURGE_FUNCTION}() FROM PUBLIC".encode()
        )
        await connection.execute(_FIND_INDEX.encode())
        await connection.execute(_ONE_OPEN_REQUEST.encode())
        await connection.execute(_DASHBOARD_VIEW.encode())
        # The dashboard's reader sees the view and nothing else in this schema. If its role does
        # not exist yet (telemetry-setup runs first in Compose), there is nothing to grant.
        await connection.execute(_DASHBOARD_GRANT.encode())


async def grant_policy_access(connection: AsyncConnection) -> None:
    """Make the roles' grants what they should be.

    The tables and roles must exist. Nobody may be a member of a policy role (a member inherits its
    privileges). The auditor gets the audit log to read and nothing else. The gateway and approver
    keep the installer's layout on agent-core's tables (`_clear_layout_grants` made them start from
    nothing before the installer ran), and get this gateway's own tables."""
    schema = sql.Identifier(SCHEMA)
    async with connection.transaction():
        await connection.execute(sql.SQL("SET LOCAL search_path TO {}").format(schema))
        await _revoke_memberships(connection)
        for role in ROLES:
            await reset_role(connection, role)
            name = sql.Identifier(role)
            if role == AUDITOR_ROLE:
                await revoke_role_access(connection, SCHEMA, role)
            else:
                for table in _OWN_TABLES:
                    await connection.execute(
                        sql.SQL("REVOKE ALL ON {}.{} FROM {}").format(
                            schema, sql.Identifier(table), name
                        )
                    )
            await connection.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(schema, name))
            for table, privileges in _GRANTS[role]:
                await connection.execute(
                    sql.SQL("GRANT {} ON {}.{} TO {}").format(
                        sql.SQL(privileges), schema, sql.Identifier(table), name
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


async def _clear_layout_grants(connection: AsyncConnection) -> None:
    """Take every grant the gateway's and approver's roles hold on agent-core's tables, so the
    installer grants each exactly its layout.

    The installer grants a role its layout on a table only where the role holds nothing yet, and
    never revokes. A role that held anything (a2's, 3a's table-level UPDATE, a grant someone
    widened or narrowed by hand) would otherwise keep that and miss the rest of its layout. The
    installer runs straight after, in the same setup, under the same lock; between the two the
    roles can do nothing, so a write in that moment is refused, not allowed."""
    for table in (AUDIT_TABLE, APPROVALS_TABLE):
        cursor = await connection.execute("SELECT to_regclass(%s)", (f"{SCHEMA}.{table}",))
        row = await cursor.fetchone()
        if row is None or row[0] is None:
            continue
        for role in (GATEWAY_ROLE, APPROVER_ROLE):
            await connection.execute(
                sql.SQL("REVOKE ALL ON {}.{} FROM {}").format(
                    sql.Identifier(SCHEMA), sql.Identifier(table), sql.Identifier(role)
                )
            )


_MEMBERSHIPS = (
    "SELECT member.rolname, parent.rolname, grantor.rolname FROM pg_auth_members m"
    " JOIN pg_roles parent ON parent.oid = m.roleid"
    " JOIN pg_roles member ON member.oid = m.member"
    " JOIN pg_roles grantor ON grantor.oid = m.grantor WHERE parent.rolname = ANY(%s)"
    " AND member.rolname <> %s"  # the lab approver's membership is set up deliberately, below
)


async def _set_up_lab_role(connection: AsyncConnection, password: str | None) -> None:
    """The lab approver: a login role that is a member of the approver role, so it has exactly the
    approver's powers, and nothing else. Without a password it does not exist."""
    name = sql.Identifier(LAB_APPROVER_ROLE)
    if not password:
        if await _role_exists(connection, LAB_APPROVER_ROLE):
            await connection.execute(sql.SQL("DROP OWNED BY {}").format(name))
            await connection.execute(sql.SQL("DROP ROLE {}").format(name))
        return
    await ensure_role(connection, password, LAB_APPROVER_ROLE, POLICY_IDLE_IN_TRANSACTION_MS)
    await reset_role(connection, LAB_APPROVER_ROLE)
    await connection.execute(sql.SQL("GRANT {} TO {}").format(sql.Identifier(APPROVER_ROLE), name))
    logger.warning("the lab approver role exists: it decides requests as the approver role")


async def _role_exists(connection: AsyncConnection, role: str) -> bool:
    cursor = await connection.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
    return await cursor.fetchone() is not None


async def _memberships(connection: AsyncConnection) -> list[tuple[str, str, str]]:
    cursor = await connection.execute(_MEMBERSHIPS, (list(ROLES), LAB_APPROVER_ROLE))
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
