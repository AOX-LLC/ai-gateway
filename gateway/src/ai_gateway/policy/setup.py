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
import sys
from dataclasses import dataclass

import anyio
from aox_agent_core.errors import ConfigError
from aox_agent_core.storage import InstallReport, install_postgres_schema
from psycopg import AsyncConnection, sql

from ai_gateway.policy import (
    ACTIVE_APPROVERS_VIEW,
    APPROVALS_TABLE,
    APPROVER_LOGINS_VIEW,
    APPROVER_ROLE,
    APPROVERS_TABLE,
    AUDIT_TABLE,
    AUDITOR_ROLE,
    DASHBOARD_VIEW,
    GATEWAY_ROLE,
    LAB_APPROVER_ID,
    LAB_APPROVER_ROLE,
    POLICY_IDLE_IN_TRANSACTION_MS,
    PROVISIONING_LOCK,
    PURGER_PRINCIPAL,
    PURGER_ROLE,
    ROLES,
    SCHEMA,
)
from ai_gateway.policy.approver_logins import (
    approver_principal,
    bind_active_approvers,
    bind_login,
    managed_logins,
    sync_approver_logins,
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

INSTALLER_NO_DECISIONS_MESSAGE = "close_unaudited_approvals needs"
"""The start of agent-core's error when it will not cancel unaudited approvals because no current
approver has decided anything (see `_install`)."""

_SETUP_LOCK = PROVISIONING_LOCK


@dataclass(frozen=True)
class PolicyPasswords:
    gateway: str
    auditor: str
    lab_approver: str | None = None
    """Set only for a lab stack: creates the lab approver role. Unset, the role cannot log in."""
    payload_purger: str | None = None
    """The password of the payload purge's login. Unset, the login cannot log in and old arguments
    are not removed (the purge service refuses to start without it)."""


async def setup_policy(owner_url: str, passwords: PolicyPasswords) -> None:
    """Create the roles and the schema, install or upgrade agent-core's tables, and grant."""
    role_passwords = (
        (GATEWAY_ROLE, passwords.gateway),
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
        await _ensure_approver_group(connection)
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
        await _drop_the_gateways_own_unique_index(connection)
        await grant_policy_access(connection)
        await sync_approver_logins(connection)
        await _set_up_lab_role(connection, passwords.lab_approver)
        await _set_up_purger(connection, passwords.payload_purger)
        # Binding goes on last: every active approver's login is mapped first, so nobody who could
        # decide a moment ago is locked out. From here the database refuses a decision whose
        # `resolved_by` is not the principal mapped to the login that made it.
        await bind_active_approvers(connection)
        await _install(owner_url, bind=True)
        await restrict_database_access(
            connection,
            [
                *ROLES,
                *([LAB_APPROVER_ROLE] if passwords.lab_approver else []),
                *([PURGER_ROLE] if passwords.payload_purger else []),
            ],
        )


_APPROVED_WITHOUT_A_DECISION = f"""
SELECT a.id FROM {SCHEMA}.{APPROVALS_TABLE} a
WHERE a.status = 'approved' AND NOT EXISTS (
    SELECT 1 FROM {SCHEMA}.{AUDIT_TABLE} e
    JOIN {SCHEMA}.{APPROVERS_TABLE} p ON p.db_role = e.db_role
    WHERE e.action = 'approval.resolved' AND e.subject_id = a.id
      AND e.payload LIKE '%"decision":"approve"%'
      AND (p.removed_at IS NOT NULL OR p.db_role = '{LAB_APPROVER_ROLE}')
)
LIMIT 1
"""  # noqa: S608 - fixed names and no input


async def _approved_without_an_explained_decision(owner_url: str) -> bool:
    """Whether an approved request lacks a decision event written by the login of a removed approver
    (or the lab approver's): one that nobody who ever held a login approved. Only asked in the
    all-removed case, where every request the installer would cancel is one a removed approver
    decided; anything else is plain SQL and stops setup."""
    async with await AsyncConnection.connect(owner_url, autocommit=True) as connection:
        cursor = await connection.execute(
            "SELECT count(*) FROM information_schema.columns WHERE table_schema = %s"
            " AND table_name = %s AND column_name = 'db_role'",
            (SCHEMA, APPROVERS_TABLE),
        )
        row = await cursor.fetchone()
        if row is None or row[0] == 0:
            return True  # no approver ever had a login of their own: nothing explains it
        cursor = await connection.execute(_APPROVED_WITHOUT_A_DECISION.encode())
        return await cursor.fetchone() is not None


async def _ensure_approver_group(connection: AsyncConnection) -> None:
    """The approver role is a group: it holds the approvers' privileges and nobody logs in as it.
    Each person's own login is a member (`approver-add`). Nothing can connect as the shared role,
    so no shared password exists to be passed around."""
    name = sql.Identifier(APPROVER_ROLE)
    if not await _role_exists(connection, APPROVER_ROLE):
        await connection.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(name))
    await connection.execute(sql.SQL("ALTER ROLE {} NOLOGIN").format(name))
    # Inert for the members, whose sessions use their own login's setting (`ensure_role` sets it),
    # but every role carries the limit, so a check that lists roles finds none without one.
    await connection.execute(
        sql.SQL("ALTER ROLE {} SET idle_in_transaction_session_timeout = {}").format(
            name, sql.Literal(f"{POLICY_IDLE_IN_TRANSACTION_MS}ms")
        )
    )
    await reset_role(connection, APPROVER_ROLE)


async def _install(owner_url: str, *, bind: bool | None = None) -> None:
    """Install or upgrade agent-core's tables and guard. The installer is synchronous (agent-core's
    drivers are), so it runs on a thread. An approved, unused request that no approver's decision
    approves (plain SQL could make one under a2, and a decision by an approver who has since been
    removed stops counting) is cancelled, and said so.

    The installer refuses to cancel anything when it finds no decision by a current approver at
    all, which is the case when everyone who ever decided has been removed. That must not stop the
    stack from starting, so when each such request does have a decision on record, by an approver
    since removed, it installs again without cancelling and names them: the gate will not consume
    an approval whose approver is no longer active, so they only wait to expire. A request with no
    decision on record at all (plain SQL made it) still stops setup, as before."""

    def install(*, close: bool) -> InstallReport:
        return install_postgres_schema(
            owner_url,
            schema=SCHEMA,
            requester_role=GATEWAY_ROLE,
            approver_role=APPROVER_ROLE,
            close_unaudited_approvals=close,
            bind_resolved_by=bind,
        )

    try:
        report = await anyio.to_thread.run_sync(lambda: install(close=True))
    except ConfigError as error:
        # agent-core raises a plain ConfigError for this, so the case is told by its message: a
        # test pins the text against the installed tag, so a change shows when the tag moves, not
        # in production (where the error simply stops setup, as any other would).
        if INSTALLER_NO_DECISIONS_MESSAGE not in str(error):
            raise
        if await _approved_without_an_explained_decision(owner_url):
            raise  # an approval no approver's decision made: that is what setup exists to stop
        report = await anyio.to_thread.run_sync(lambda: install(close=False))
        left = ", ".join(report.unaudited_approvals)
        if not report.unaudited_approvals:
            raise RuntimeError(
                "the installer refused to cancel approvals whose approvers were removed, but then"
                " listed none: not guessing what to leave as it is"
            ) from error
        # Loud: an error, not a warning, and on stderr as well as in the log, because this leaves
        # approved requests in place that nobody who may still approve ever approved.
        logger.error(
            "LEFT AS THEY ARE, NOT CANCELLED: approved requests whose only decisions are by"
            " approvers who have been removed (the gateway will not use them; they expire): %s",
            left,
        )
        print(
            f"policy-setup: left approved requests of removed approvers as they are: {left}",
            file=sys.stderr,
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
    if bind and report.unmapped_logins:
        logger.error(
            "login binding is on and these logins have no mapping, so they cannot decide: %s",
            ", ".join(report.unmapped_logins),
        )
    logger.info("agent-core's audit and approval tables are installed in schema %s", SCHEMA)


# What this gateway grants, on its own tables and the audit log for the auditor. The gateway's and
# the approver's rights on agent-core's tables are the installer's layout.
_GRANTS = {
    # It stores the arguments and can never read them back, nor choose when they are purged
    # (`created_at` is the database's): only these two columns.
    # And it may ask whether the person behind an approval is still an approver, and nothing more
    # about them: a removed approver's approval is not used.
    GATEWAY_ROLE: ((ACTIVE_APPROVERS_VIEW, "SELECT"),),
    APPROVER_ROLE: ((APPROVERS_TABLE, "SELECT"),),
    # Arguments are never in the audit trail. The auditor also sees which login is which approver
    # (not the approvers' names), so `audit-verify` can check who wrote a decision.
    AUDITOR_ROLE: ((AUDIT_TABLE, "SELECT"), (APPROVER_LOGINS_VIEW, "SELECT")),
}
_OWN_TABLES = (APPROVERS_TABLE, APPROVER_LOGINS_VIEW, ACTIVE_APPROVERS_VIEW)

_APPROVAL_TABLES = f"""
CREATE TABLE IF NOT EXISTS {APPROVERS_TABLE} (
    id TEXT PRIMARY KEY CHECK (id ~ '^[a-z][a-z0-9._-]{{0,62}}$'),
    display_name TEXT NOT NULL CHECK (length(display_name) BETWEEN 1 AND 100),
    roles TEXT[] NOT NULL CHECK (cardinality(roles) > 0),
    active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE {APPROVERS_TABLE} ADD COLUMN IF NOT EXISTS db_role TEXT
    CHECK (db_role ~ '^policy_approver_[a-z0-9_]{{1,40}}$' OR db_role = '{LAB_APPROVER_ROLE}');
ALTER TABLE {APPROVERS_TABLE} ADD COLUMN IF NOT EXISTS removed_at TIMESTAMPTZ;
CREATE UNIQUE INDEX IF NOT EXISTS {APPROVERS_TABLE}_db_role ON {APPROVERS_TABLE} (db_role);
CREATE OR REPLACE FUNCTION {APPROVERS_TABLE}_keep_identities() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF TG_OP IN ('DELETE', 'TRUNCATE') THEN
        RAISE EXCEPTION 'an approver is never deleted: remove it, so the id and login stay taken'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.id <> OLD.id
       OR (OLD.db_role IS NOT NULL AND NEW.db_role IS DISTINCT FROM OLD.db_role)
       OR (OLD.removed_at IS NOT NULL
           AND (NEW.removed_at IS DISTINCT FROM OLD.removed_at OR NEW.active)) THEN
        RAISE EXCEPTION 'an approver keeps its id and its login for good, and a removed one stays'
            ' removed' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS {APPROVERS_TABLE}_keep_identities ON {APPROVERS_TABLE};
CREATE TRIGGER {APPROVERS_TABLE}_keep_identities BEFORE UPDATE OR DELETE ON {APPROVERS_TABLE}
    FOR EACH ROW EXECUTE FUNCTION {APPROVERS_TABLE}_keep_identities();
DROP TRIGGER IF EXISTS {APPROVERS_TABLE}_no_truncate ON {APPROVERS_TABLE};
CREATE TRIGGER {APPROVERS_TABLE}_no_truncate BEFORE TRUNCATE ON {APPROVERS_TABLE}
    FOR EACH STATEMENT EXECUTE FUNCTION {APPROVERS_TABLE}_keep_identities();
"""

_RESOLVED_INDEX = f"""
CREATE INDEX IF NOT EXISTS policy_audit_resolved_subject
ON {AUDIT_TABLE} (subject_id) WHERE action = 'approval.resolved'
"""  # the gate asks who decided a request each time it consumes an approval; the log only grows

_FIND_INDEX = f"""
CREATE INDEX IF NOT EXISTS policy_approvals_find
ON {APPROVALS_TABLE} (requested_by, payload_sha256, created_at DESC)
"""  # the gate looks a request up by client and argument hash on every write

_RETIRE_THE_ARGUMENTS_TABLE = """
DROP FUNCTION IF EXISTS purge_approval_arguments();
DROP TABLE IF EXISTS approval_arguments
"""  # the arguments are stored with the request now (agent-core's include_payload) and purged by
# `approvals-purge`; the table and its hourly purge are gone. What it held was kept 7 days at most,
# and a request still open when it goes cannot be shown to an approver (it can only be rejected).

_DASHBOARD_VIEW = f"""
CREATE OR REPLACE VIEW {DASHBOARD_VIEW} AS
SELECT a.id, a.action, a.requested_by AS client_actor, a.status, a.decision, a.resolved_by,
       a.created_at::timestamptz AS created_at, a.expires_at::timestamptz AS expires_at,
       a.resolved_at::timestamptz AS resolved_at, a.consumed_at::timestamptz AS consumed_at,
       split_part(a.action, '__', 1) AS namespace,
       p.display_name AS resolved_by_name,
       c.name AS client_name
FROM {APPROVALS_TABLE} a
LEFT JOIN {APPROVERS_TABLE} p ON a.resolved_by = 'human:' || p.id
LEFT JOIN public.clients c ON a.requested_by = 'client:' || c.id::text
"""  # noqa: S608 - fixed names and no input
# Columns are only ever added at the end: a replaced view keeps the ones before. `namespace` is the
# upstream the tool belongs to (the gateway names tools <namespace>__<tool>); the approver's
# display name is the one place the dashboard sees a person's name, and `client_name` is the
# registry's name for the client that asked (null for one that has since been deleted).

_ACTIVE_APPROVERS_VIEW = f"""
CREATE OR REPLACE VIEW {ACTIVE_APPROVERS_VIEW} AS
SELECT 'human:' || id AS principal, db_role, roles FROM {APPROVERS_TABLE} WHERE active
"""  # noqa: S608 - fixed names and no input

_APPROVER_LOGINS_VIEW = f"""
CREATE OR REPLACE VIEW {APPROVER_LOGINS_VIEW} AS
SELECT db_role, id AS approver_id, removed_at IS NOT NULL AS removed
FROM {APPROVERS_TABLE}
WHERE db_role IS NOT NULL
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
        await connection.execute(_RETIRE_THE_ARGUMENTS_TABLE.encode())
        await connection.execute(_FIND_INDEX.encode())
        await connection.execute(_RESOLVED_INDEX.encode())
        await connection.execute(_DASHBOARD_VIEW.encode())
        await connection.execute(_APPROVER_LOGINS_VIEW.encode())
        await connection.execute(_ACTIVE_APPROVERS_VIEW.encode())
        # The dashboard's reader sees the view and nothing else in this schema. If its role does
        # not exist yet (telemetry-setup runs first in Compose), there is nothing to grant.
        await connection.execute(_DASHBOARD_GRANT.encode())


async def _drop_the_gateways_own_unique_index(connection: AsyncConnection) -> None:
    """One open request per client, tool and payload is agent-core's now (a unique index over open
    requests, which `submit` relies on). The gateway's earlier index over pending ones is redundant
    and is dropped. If the volume holds duplicate open requests the installer has already stopped
    and listed them (it never closes them silently)."""
    for name in ("policy_approvals_one_pending", "policy_approvals_one_open"):
        await connection.execute(
            sql.SQL("DROP INDEX IF EXISTS {}.{}").format(
                sql.Identifier(SCHEMA), sql.Identifier(name)
            )
        )


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
    " AND member.rolname <> %s"  # and so is the payload purger's
    # An active approver's login stays a member of the approver role (and only that): it is what
    # lets them decide, and agent-core's installer counts a decision only while its writer is one.
    # `sync_approver_logins` makes sure the membership is the right kind.
    " AND NOT (parent.rolname = %s AND member.rolname = ANY(%s))"
)


async def _set_up_lab_role(connection: AsyncConnection, password: str | None) -> None:
    """The lab approver: a login role that is a member of the approver role, so it has exactly the
    approver's powers, and nothing else. Without a password it cannot log in.

    The role is kept, not dropped and made again: agent-core maps a login to its principal for good
    (by the role's OID, and a login is never mapped twice), so a lab role that was made again
    could never decide. It is reset instead: its sessions end, what it owns or was granted goes,
    every membership is revoked, and it is made what it should be."""
    name = sql.Identifier(LAB_APPROVER_ROLE)
    if await _role_exists(connection, LAB_APPROVER_ROLE):
        await connection.execute(sql.SQL("ALTER ROLE {} NOLOGIN").format(name))
        await connection.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = %s",
            (LAB_APPROVER_ROLE,),
        )
        await connection.execute(sql.SQL("DROP OWNED BY {}").format(name))
        await reset_role(connection, LAB_APPROVER_ROLE)
        # And no role is a member of it: that member would inherit its powers (the role used to be
        # dropped, which took such memberships with it).
        cursor = await connection.execute(
            "SELECT member.rolname, grantor.rolname FROM pg_auth_members m"
            " JOIN pg_roles parent ON parent.oid = m.roleid"
            " JOIN pg_roles member ON member.oid = m.member"
            " JOIN pg_roles grantor ON grantor.oid = m.grantor WHERE parent.rolname = %s",
            (LAB_APPROVER_ROLE,),
        )
        for member, grantor in await cursor.fetchall():
            await connection.execute(
                sql.SQL("REVOKE {} FROM {} GRANTED BY {} CASCADE").format(
                    name, sql.Identifier(member), sql.Identifier(grantor)
                )
            )
    approvers = sql.SQL("{}.{}").format(sql.Identifier(SCHEMA), sql.Identifier(APPROVERS_TABLE))
    if not password:
        # Its decisions stay attributed to it; nobody can decide through a login that is shut.
        await connection.execute(
            sql.SQL("UPDATE {} SET active = false WHERE db_role = %s").format(approvers),
            (LAB_APPROVER_ROLE,),
        )
        return
    await ensure_role(connection, password, LAB_APPROVER_ROLE, POLICY_IDLE_IN_TRANSACTION_MS)
    await reset_role(connection, LAB_APPROVER_ROLE)
    # Inheritance is how it connects and decides; without SET it cannot `SET ROLE` to the group and
    # write records as the shared role.
    await connection.execute(
        sql.SQL("GRANT {} TO {} WITH INHERIT TRUE, SET FALSE").format(
            sql.Identifier(APPROVER_ROLE), name
        )
    )
    # The login is the identity: a decision it writes is recorded under this role, so the approver
    # record that names it is made here and `approver-add` refuses the id.
    await connection.execute(
        sql.SQL(
            "INSERT INTO {} (id, display_name, roles, db_role) VALUES (%s, %s, %s, %s)"
            " ON CONFLICT (id) DO UPDATE SET active = true, db_role = EXCLUDED.db_role,"
            " removed_at = NULL"
        ).format(approvers),
        (LAB_APPROVER_ID, "Lab approver (automatic)", ["approver"], LAB_APPROVER_ROLE),
    )
    await bind_login(connection, LAB_APPROVER_ROLE, approver_principal(LAB_APPROVER_ID))
    logger.warning("the lab approver role exists: it decides requests as the approver role")


async def _set_up_purger(connection: AsyncConnection, password: str | None) -> None:
    """The payload purge's login. agent-core lets only the approver side purge a stored payload, so
    it is a member of the approver role (inheriting, without `SET`), made one connection at a time.

    It is not an approver: it is mapped to a principal `policy.approvers` does not list, so the
    gateway will not use a decision it writes and `audit-verify` refuses one. Like the lab role it
    is kept, not dropped and made again (a mapping is for good, by the role's OID), and without the
    password it cannot log in."""
    name = sql.Identifier(PURGER_ROLE)
    if await _role_exists(connection, PURGER_ROLE):
        await connection.execute(sql.SQL("ALTER ROLE {} NOLOGIN").format(name))
        await connection.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = %s",
            (PURGER_ROLE,),
        )
        await connection.execute(sql.SQL("DROP OWNED BY {}").format(name))
        await reset_role(connection, PURGER_ROLE)
    if not password:
        return
    await ensure_role(connection, password, PURGER_ROLE, POLICY_IDLE_IN_TRANSACTION_MS)
    await reset_role(connection, PURGER_ROLE)
    await connection.execute(sql.SQL("ALTER ROLE {} CONNECTION LIMIT 1").format(name))
    await connection.execute(
        sql.SQL("GRANT {} TO {} WITH INHERIT TRUE, SET FALSE, ADMIN FALSE").format(
            sql.Identifier(APPROVER_ROLE), name
        )
    )
    await bind_login(connection, PURGER_ROLE, PURGER_PRINCIPAL)


async def _role_exists(connection: AsyncConnection, role: str) -> bool:
    cursor = await connection.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
    return await cursor.fetchone() is not None


async def _memberships(connection: AsyncConnection) -> list[tuple[str, str, str]]:
    cursor = await connection.execute(
        _MEMBERSHIPS,
        (
            list(ROLES),
            LAB_APPROVER_ROLE,
            PURGER_ROLE,
            APPROVER_ROLE,
            await managed_logins(connection),
        ),
    )
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
