"""Each approver's own database login: provisioning, rotation, removal and upkeep.

A person decides approvals through a login of their own, a member of the approver role. agent-core's
audit trigger records the database role that wrote each row (`db_role`, set by the database from
`current_user`), so a decision carries the login of whoever made it, whatever the tool claims. The
login is made `INHERIT TRUE, SET FALSE`: it has the approver role's privileges (inheritance is how
it can connect and read the queue) but cannot `SET ROLE` to the group.

These functions run as the database owner, on an autocommit connection, and are safe to repeat. The
provisioning events are appended to the audit log by the gateway role (`audit`), because
agent-core refuses an append from a role that owns the audit table.
"""

import logging
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from aox_agent_core.audit import AuditEvent, SQLAuditLog
from psycopg import AsyncConnection, sql
from pydantic import JsonValue

from ai_gateway.policy import (
    APPROVER_ID_MAX_LENGTH,
    APPROVER_LOGIN_CONNECTION_LIMIT,
    APPROVER_LOGIN_VALID_DAYS,
    APPROVER_ROLE,
    APPROVERS_TABLE,
    LAB_APPROVER_ROLE,
    POLICY_IDLE_IN_TRANSACTION_MS,
    PROVISIONING_LOCK,
    RESERVED_APPROVER_IDS,
    SCHEMA,
    approver_login_name,
)
from mcp_common.roles import advisory_lock, ensure_role, existing_roles

logger = logging.getLogger(__name__)

_ID = re.compile(rf"[a-z][a-z0-9._-]{{0,{APPROVER_ID_MAX_LENGTH - 1}}}")
_ROLE = re.compile(r"[a-z][a-z0-9_.-]{0,63}")
_ACTOR = "admin"
"""Who the provisioning events say acted. The admin CLI is not authenticated beyond the owner's
credential, so this names the tool, not a person."""
_APPROVERS = sql.SQL("{}.{}").format(sql.Identifier(SCHEMA), sql.Identifier(APPROVERS_TABLE))


class ApproverLoginError(Exception):
    """Something the person running the command can put right; the message says what."""


@dataclass(frozen=True)
class Provisioned:
    approver_id: str
    login: str
    password: str | None
    """Shown once, to the person running the command, and kept nowhere. None when no login was
    made or changed."""


@dataclass(frozen=True)
class _Row:
    db_role: str | None
    active: bool
    removed: bool


def new_password() -> str:
    return secrets.token_urlsafe(32)


async def _add_approver(
    connection: AsyncConnection,
    audit: SQLAuditLog,
    approver_id: str,
    name: str,
    roles: list[str],
) -> Provisioned:
    """Register a person and make their login, or update the name and roles of one who has it.

    Nothing is made unless the audit log takes the record: a login that exists without a record of
    its creation is undone."""
    _check_id(approver_id)
    for role in roles:
        if not _ROLE.fullmatch(role):
            raise ApproverLoginError(
                f"{role!r} is not a role name (lowercase letters, digits, . _ -)"
            )
    row = await _row(connection, approver_id)
    if row and row.removed:
        raise ApproverLoginError(
            f"approver {approver_id} was removed, and an id is never reused: pick another"
        )
    login = approver_login_name(approver_id)
    if row and row.db_role and row.active:
        await _update(connection, approver_id, name, roles)
        await _record(audit, "approver.updated", approver_id, {"roles": list(roles)})
        return Provisioned(approver_id, login, None)

    await _refuse_name_clash(connection, approver_id, login, row)
    password = new_password()
    await _create_login(connection, login, password)
    # The record comes before the approver's row, so that a login nobody can account for is dropped
    # and no row ever has to be deleted: a row is never removed, which keeps an id and a login from
    # being given to anyone else.
    try:
        await _record(audit, "approver.added", approver_id, {"login": login, "roles": list(roles)})
        await connection.execute(
            sql.SQL(
                "INSERT INTO {} (id, display_name, roles, db_role) VALUES (%s, %s, %s, %s)"
                " ON CONFLICT (id) DO UPDATE SET display_name = EXCLUDED.display_name,"
                " roles = EXCLUDED.roles, db_role = EXCLUDED.db_role, active = true"
            ).format(_APPROVERS),
            (approver_id, name, roles, login),
        )
    except Exception as error:
        await _drop_login(connection, login)
        raise ApproverLoginError(
            f"approver {approver_id} was not added ({type(error).__name__}: the audit log or the"
            " approvers table refused it); no login was kept"
        ) from error
    return Provisioned(approver_id, login, password)


async def _rotate_approver(
    connection: AsyncConnection, audit: SQLAuditLog, approver_id: str
) -> Provisioned:
    """Give an approver a new password and end their open sessions. The old password stops working
    at once. The login is made again if the role is missing."""
    row = await _row(connection, approver_id)
    if row is None or not row.active or row.removed:
        raise ApproverLoginError(f"no active approver {approver_id!r}")
    if not row.db_role:
        raise ApproverLoginError(f"approver {approver_id} has no login yet: run approver-add")
    password = new_password()
    await _record(audit, "approver.rotated", approver_id, {"login": row.db_role})
    await _create_login(connection, row.db_role, password)
    await _end_sessions(connection, row.db_role)
    return Provisioned(approver_id, row.db_role, password)


async def _remove_approver(
    connection: AsyncConnection, audit: SQLAuditLog, approver_id: str
) -> None:
    """Take a person's login away and end their sessions. The row stays, inactive and marked
    removed, so the decisions they made still name them, and the id is never given to anyone else.

    A request the person approved and nobody has used yet is no longer approved by anyone who may
    approve: the next `policy-setup` cancels it (agent-core's installer does, on its own check)."""
    row = await _row(connection, approver_id)
    if row is None or row.removed:
        raise ApproverLoginError(f"no approver {approver_id!r}")
    await connection.execute(
        sql.SQL("UPDATE {} SET active = false, removed_at = now() WHERE id = %s").format(
            _APPROVERS
        ),
        (approver_id,),
    )
    if row.db_role and row.db_role != LAB_APPROVER_ROLE:
        await _drop_login(connection, row.db_role)
    try:
        await _record(audit, "approver.removed", approver_id, {"login": row.db_role or ""})
    except Exception as error:
        raise ApproverLoginError(
            f"approver {approver_id} was removed, but the audit log could not record it"
            f" ({type(error).__name__}): note it down and record it when the log is back"
        ) from error


async def add_approver(
    connection: AsyncConnection,
    audit: SQLAuditLog,
    approver_id: str,
    name: str,
    roles: list[str],
) -> Provisioned:
    """Register a person and make their login, or update the name and roles of one who has it.

    Nothing is kept unless the audit log takes the record. One at a time with setup and the other
    provisioning commands: two ids that make one login name must not overwrite each other."""
    async with advisory_lock(connection, PROVISIONING_LOCK):
        return await _add_approver(connection, audit, approver_id, name, roles)


async def rotate_approver(
    connection: AsyncConnection, audit: SQLAuditLog, approver_id: str
) -> Provisioned:
    """Give an approver a new password and end their open sessions. The old password stops working
    at once. The login is made again if the role is missing. The change is recorded first: if the
    audit log cannot take the record, nothing changes and nobody is locked out."""
    _check_not_reserved(approver_id)
    async with advisory_lock(connection, PROVISIONING_LOCK):
        return await _rotate_approver(connection, audit, approver_id)


async def remove_approver(
    connection: AsyncConnection, audit: SQLAuditLog, approver_id: str
) -> None:
    """Take a person's login away and end their sessions. The row stays, inactive and marked
    removed, so the decisions they made still name them, and the id is never given to anyone else.

    Removing goes first and is recorded after: a person who must lose access loses it even when the
    audit log is down, and the error then says the removal is not in the log.

    A request the person approved and nobody has used yet is no longer approved by anyone who may
    approve: the gate will not use it, and the next `policy-setup` cancels it."""
    _check_not_reserved(approver_id)
    async with advisory_lock(connection, PROVISIONING_LOCK):
        await _remove_approver(connection, audit, approver_id)


async def sync_approver_logins(connection: AsyncConnection) -> list[str]:
    """Make every login the table records as it should be, and return the active ones that exist.

    Run by `policy-setup`, which revokes every other membership of the policy roles. An active
    approver's login keeps exactly one membership, of the approver role, and no other privilege; a
    removed approver's login, if one is left, cannot log in. A recorded login whose role is missing
    is reported, not made: its password cannot be recovered, only replaced by `approver-rotate`."""
    rows = await _login_rows(connection)
    present = set(await existing_roles(connection, [role for _, role, _ in rows]))
    active: list[str] = []
    for approver_id, role, is_active in rows:
        if role not in present:
            if is_active:
                logger.warning(
                    "approver %s has no login role %s: run approver-rotate", approver_id, role
                )
            continue
        if is_active:
            await _normalise_login(connection, role)
            active.append(role)
        else:
            await connection.execute(sql.SQL("ALTER ROLE {} NOLOGIN").format(sql.Identifier(role)))
            await _end_sessions(connection, role)  # after NOLOGIN: it cannot reconnect in between
    return active


async def managed_logins(connection: AsyncConnection) -> list[str]:
    """The active approvers' login roles that exist: the members of the approver role that setup
    keeps rather than revokes."""
    present = {role for _, role, active in await _login_rows(connection) if active}
    return await existing_roles(connection, sorted(present))


async def _login_rows(connection: AsyncConnection) -> list[tuple[str, str, bool]]:
    """(approver id, login role, active) for every approver with a login. The lab approver's role
    is rebuilt by its own step, so it is not here. An older volume with no such column has none."""
    cursor = await connection.execute(
        "SELECT 1 FROM information_schema.columns WHERE table_schema = %s"
        " AND table_name = %s AND column_name = 'db_role'",
        (SCHEMA, APPROVERS_TABLE),
    )
    if await cursor.fetchone() is None:
        return []
    cursor = await connection.execute(
        sql.SQL(
            "SELECT id, db_role, active FROM {} WHERE db_role IS NOT NULL AND db_role <> %s"
            " ORDER BY id"
        ).format(_APPROVERS),
        (LAB_APPROVER_ROLE,),
    )
    return [(str(a), str(b), bool(c)) for a, b, c in await cursor.fetchall()]


def _check_id(approver_id: str) -> None:
    if not _ID.fullmatch(approver_id):
        raise ApproverLoginError(
            "an approver id is lowercase letters, digits, . _ -, starting with a letter,"
            f" at most {APPROVER_ID_MAX_LENGTH} characters"
        )
    _check_not_reserved(approver_id)


def _check_not_reserved(approver_id: str) -> None:
    if approver_id in RESERVED_APPROVER_IDS:
        raise ApproverLoginError(f"{approver_id} is reserved: policy-setup makes that login")


async def _row(connection: AsyncConnection, approver_id: str) -> _Row | None:
    cursor = await connection.execute(
        sql.SQL("SELECT db_role, active, removed_at IS NOT NULL FROM {} WHERE id = %s").format(
            _APPROVERS
        ),
        (approver_id,),
    )
    found = await cursor.fetchone()
    if found is None:
        return None
    return _Row(db_role=found[0], active=bool(found[1]), removed=bool(found[2]))


async def _update(
    connection: AsyncConnection, approver_id: str, name: str, roles: list[str]
) -> None:
    await connection.execute(
        sql.SQL("UPDATE {} SET display_name = %s, roles = %s WHERE id = %s").format(_APPROVERS),
        (name, roles, approver_id),
    )


async def _refuse_name_clash(
    connection: AsyncConnection, approver_id: str, login: str, row: _Row | None
) -> None:
    """The login name comes from the id with `.` and `-` made `_`, so two ids can want one name,
    and an unrelated database role may already have it. Neither is taken over."""
    cursor = await connection.execute(
        sql.SQL("SELECT id FROM {} WHERE db_role = %s AND id <> %s").format(_APPROVERS),
        (login, approver_id),
    )
    other = await cursor.fetchone()
    if other is not None:
        raise ApproverLoginError(f"the login {login} already belongs to approver {other[0]}")
    if (await existing_roles(connection, [login])) and not (row and row.db_role == login):
        raise ApproverLoginError(
            f"a database role named {login} already exists: not taking it over"
        )


async def _create_login(connection: AsyncConnection, login: str, password: str) -> None:
    await ensure_role(connection, password, login, POLICY_IDLE_IN_TRANSACTION_MS)
    expires = (datetime.now(UTC) + timedelta(days=APPROVER_LOGIN_VALID_DAYS)).isoformat()
    await connection.execute(
        sql.SQL("ALTER ROLE {} VALID UNTIL {}").format(sql.Identifier(login), sql.Literal(expires))
    )
    await _normalise_login(connection, login)


async def _normalise_login(connection: AsyncConnection, login: str) -> None:
    """The login's attributes, its one membership and nothing else: no attribute that bypasses a
    check, a connection limit, membership of the approver role with inheritance, without `SET ROLE`
    and without the right to grant it on, no other role, no role that is a member of it (that
    member could `SET ROLE` to it and write as it), and no table privilege in the policy schema
    granted to it directly. It keeps the CONNECT that `ensure_role` gave it."""
    name = sql.Identifier(login)
    await connection.execute(
        sql.SQL(
            "ALTER ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
            " CONNECTION LIMIT {}"
        ).format(name, sql.Literal(APPROVER_LOGIN_CONNECTION_LIMIT))
    )
    await connection.execute(
        sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA {} FROM {}").format(
            sql.Identifier(SCHEMA), name
        )
    )
    kept = False
    for parent, member, grantor, inherit, can_set, admin in await _grants(connection, login):
        if (
            member == login
            and parent == APPROVER_ROLE
            and (inherit, can_set, admin) == (True, False, False)
            and not kept
        ):
            kept = True
            continue
        await connection.execute(
            sql.SQL("REVOKE {} FROM {} GRANTED BY {} CASCADE").format(
                sql.Identifier(parent), sql.Identifier(member), sql.Identifier(grantor)
            )
        )
    if not kept:
        await connection.execute(
            sql.SQL("GRANT {} TO {} WITH INHERIT TRUE, SET FALSE, ADMIN FALSE").format(
                sql.Identifier(APPROVER_ROLE), name
            )
        )


async def _grants(
    connection: AsyncConnection, login: str
) -> list[tuple[str, str, str, bool, bool, bool]]:
    """Every membership the login is part of, as a member or as the role granted:
    (role, member, grantor, inherit, set, admin)."""
    cursor = await connection.execute(
        "SELECT parent.rolname, member.rolname, grantor.rolname,"
        " m.inherit_option, m.set_option, m.admin_option FROM pg_auth_members m"
        " JOIN pg_roles parent ON parent.oid = m.roleid"
        " JOIN pg_roles member ON member.oid = m.member"
        " JOIN pg_roles grantor ON grantor.oid = m.grantor"
        " WHERE member.rolname = %s OR parent.rolname = %s"
        " ORDER BY parent.rolname, member.rolname, grantor.rolname",
        (login, login),
    )
    return [
        (str(a), str(b), str(c), bool(d), bool(e), bool(f))
        for a, b, c, d, e, f in await cursor.fetchall()
    ]


async def _end_sessions(connection: AsyncConnection, login: str) -> None:
    await connection.execute(
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = %s", (login,)
    )


async def _drop_login(connection: AsyncConnection, login: str) -> None:
    if not await existing_roles(connection, [login]):
        return
    name = sql.Identifier(login)
    await connection.execute(sql.SQL("ALTER ROLE {} NOLOGIN").format(name))
    await _end_sessions(connection, login)
    await connection.execute(sql.SQL("DROP OWNED BY {}").format(name))
    await connection.execute(sql.SQL("DROP ROLE {}").format(name))


async def _record(
    audit: SQLAuditLog, action: str, approver_id: str, payload: dict[str, JsonValue]
) -> None:
    await audit.append(
        AuditEvent(action=action, actor_id=_ACTOR, subject_id=approver_id, payload=payload)
    )
