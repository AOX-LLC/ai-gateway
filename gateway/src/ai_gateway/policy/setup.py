"""Set up the policy schema: roles, agent-core's tables, grants, and the approval guard.

Runs as the database owner (`gateway-admin policy-setup`) and is safe to repeat. agent-core's own
installer is not repeatable (it creates tables), so it runs only when the tables are missing; the
grants and the guard trigger are applied on every run, after revoking everything the roles hold.
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
    ensure_role,
    ensure_schema,
    reset_role,
    restrict_database_access,
    revoke_role_access,
)

logger = logging.getLogger(__name__)


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
    async with await AsyncConnection.connect(owner_url, autocommit=True) as connection:
        for role, password in role_passwords:
            await ensure_role(connection, password, role)
        await ensure_schema(connection, SCHEMA)
        installed = await _tables_exist(connection)
    if installed:
        logger.info("agent-core's audit and approval tables are already installed")
    else:
        # The installer is synchronous (agent-core's drivers are), so it runs on a thread.
        await anyio.to_thread.run_sync(
            lambda: install_postgres_schema(policy_url(owner_url), app_role=GATEWAY_ROLE)
        )
        logger.info("installed agent-core's audit and approval tables in schema %s", SCHEMA)
    async with await AsyncConnection.connect(owner_url, autocommit=True) as connection:
        await grant_policy_access(connection)
        await restrict_database_access(connection, list(ROLES))


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
        -- The gateway uses an approval: it may only consume one that was approved, and change
        -- nothing else about it. It can never decide a request, whatever its code does.
        IF NOT (OLD.status = 'approved' AND NEW.status = 'consumed' AND NEW.consumed_at IS NOT NULL
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
        -- An approver decides a pending request, once, and cannot use it.
        IF NOT (OLD.status = 'pending' AND NEW.status IN ('approved', 'rejected')
                AND NEW.consumed_at IS NULL) THEN
            RAISE EXCEPTION 'the approver role may only decide a pending request';
        END IF;
    END IF;
    RETURN NEW;
END $$
"""


async def grant_policy_access(connection: AsyncConnection) -> None:
    """Make the three roles' grants exactly these, and (re)install the approval guard.

    The tables and roles must exist. Everything each role holds in the schema is revoked first,
    and its attributes and memberships are reset, so a widened grant is narrowed again."""
    schema = sql.Identifier(SCHEMA)
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
    # The guard function and trigger belong to the policy schema, like the table they guard.
    await connection.execute(sql.SQL("SET search_path TO {}").format(schema))
    try:
        await connection.execute(_GUARD_FUNCTION.encode())
        await connection.execute(
            f"DROP TRIGGER IF EXISTS {APPROVALS_TABLE}_guard ON {APPROVALS_TABLE}".encode()
        )
        await connection.execute(
            f"CREATE TRIGGER {APPROVALS_TABLE}_guard BEFORE INSERT OR UPDATE ON {APPROVALS_TABLE}"
            f" FOR EACH ROW EXECUTE FUNCTION {APPROVALS_TABLE}_guard()".encode()
        )
    finally:
        await connection.execute("RESET search_path")
