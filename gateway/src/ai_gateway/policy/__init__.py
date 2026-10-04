"""The policy engine's storage: the audit log and the approval queue, in the `policy` schema.

Both are agent-core's (https://github.com/AOX-LLC/agent-core, pinned by tag): an append-only,
hash-chained audit log and a human-approval queue, on Postgres. This package creates their
schema and three roles, and adapts them to the gateway's needs.
"""

import secrets
import string
from urllib.parse import quote, unquote, urlencode, urlsplit, urlunsplit

from aox_agent_core.approvals import ApproverPolicy, SQLApprovalQueue
from aox_agent_core.audit import SQLAuditLog
from aox_agent_core.storage import Database

SCHEMA = "policy"
GATEWAY_ROLE = "policy_gateway"
"""The gateway: appends audit records, asks for approvals and consumes approved ones."""
APPROVER_ROLE = "policy_approver"
"""The approver role: decides pending requests. A group role that cannot log in: each person
decides through a login of their own that is a member of it (`approver-add`), and Phase 6's lab
approver through the lab role below."""
APPROVER_LOGIN_PREFIX = "policy_approver_"
"""A person's database login is this and their approver id, with `.` and `-` made `_`."""
APPROVER_ID_PREFIX = "appr_"
APPROVER_ID_SUFFIX_LENGTH = 10
APPROVER_ID_PATTERN = rf"{APPROVER_ID_PREFIX}[a-z0-9]{{{APPROVER_ID_SUFFIX_LENGTH}}}"
"""An approver id is opaque: `appr_` and ten random characters, made by `approver-add`, never a
person's name. The id names the login (`policy_approver_<id>`) and the principal (`human:<id>`), and
both are written for good: the audit log and agent-core's login mapping are append-only, so a name
there could never be erased. The person's name lives only in `policy.approvers.display_name`."""
APPROVER_ID_MAX_LENGTH = len(APPROVER_ID_PREFIX) + APPROVER_ID_SUFFIX_LENGTH
"""A role name is at most 63 bytes, and the login prefix is 16."""
APPROVER_LOGIN_VALID_DAYS = 90
"""A login's password stops working this long after it was set; `approver-rotate` sets a new one."""
APPROVER_LOGIN_CONNECTION_LIMIT = 2
APPROVER_LOGINS_VIEW = "approver_logins"
"""Which login is which approver, for the auditor: `audit-verify` checks a decision's author
against it. Includes removed approvers, whose decisions stay in the log."""
PROVISIONING_LOCK = 7_165_201_002
"""The advisory lock that serialises policy setups and approver provisioning: they change the same
roles and the same table."""
LAB_APPROVER_ID = "lab-approver"
ACTIVE_APPROVERS_VIEW = "active_approvers"
"""The approvers who may still decide, as principals (`human:<id>`), for the gateway: it will not
use an approval whose approver has been removed since."""
RESERVED_APPROVER_IDS = frozenset({LAB_APPROVER_ID})
"""Ids `approver-add` refuses: the lab approver's login is set up by `policy-setup` alone."""
LAB_APPROVER_ROLE = "policy_lab_approver"
"""Phase 6's lab approver, which approves automatically. It exists only while `policy-setup` is
given its password (POLICY_LAB_APPROVER_DB_PASSWORD), as a member of the approver role; the next
setup without one shuts it (no login, no membership, no grant) but keeps the role, since a login
mapping is by the role's OID and is never made twice. Not one of ROLES: setup removes every
membership in those."""
AUDITOR_ROLE = "policy_auditor"
"""Reads the audit log, to verify it and to take anchors."""
ROLES = (GATEWAY_ROLE, APPROVER_ROLE, AUDITOR_ROLE)

POLICY_IDLE_IN_TRANSACTION_MS = 5_000
"""The policy roles' limit on a session idle inside a transaction. Short, because any of them can
take agent-core's one audit append lock, writes need that lock and fail closed without it, and the
gateway's own waits are 1.5 s: a stuck session must not hold it for long."""

AUDIT_TABLE = "agent_core_audit"
APPROVALS_TABLE = "agent_core_approvals"
APPROVERS_TABLE = "approvers"
DASHBOARD_VIEW = "dash_approvals"
"""The approval requests as the dashboard may see them: no arguments, no reasons."""
PAYLOAD_RETENTION_DAYS = 7
"""How long a finished request keeps the arguments a person was shown. After it, `approvals-purge`
removes them (agent-core's `purge_payloads`); the request keeps its hash."""
PURGER_ROLE = "policy_payload_purger"
"""The login `approvals-purge` connects as. agent-core lets only the approver side purge a payload,
so it is a member of the approver role, and it is mapped to a principal that is not an approver
(`policy.approvers` does not list it). The gate and `audit-verify` use or accept no decision it
writes, approval or rejection. The guard itself still lets any approver-role login write one (a
purge-only role in agent-core would end that), so the purge credential is as guarded as an
approver's, and the gate is what keeps it from deciding anything."""
PURGER_PRINCIPAL = "service:payload-purger"


def new_approver_id() -> str:
    """A fresh opaque approver id."""
    alphabet = string.ascii_lowercase + string.digits
    suffix = "".join(secrets.choice(alphabet) for _ in range(APPROVER_ID_SUFFIX_LENGTH))
    return APPROVER_ID_PREFIX + suffix


def approver_login_name(approver_id: str) -> str:
    """The database login that is this approver: the same id always gives the same name."""
    return APPROVER_LOGIN_PREFIX + approver_id.replace(".", "_").replace("-", "_")


def policy_url(
    url: str,
    *,
    lock_timeout_ms: int | None = None,
    statement_timeout_ms: int | None = None,
    transaction_timeout_ms: int | None = None,
    connect_timeout_s: int | None = None,
) -> str:
    """The URL with the policy schema first on the search path: agent-core's table names are
    unqualified. It stays a URL, which is what agent-core's `open_database` takes.

    The timeouts are set on the connection, so the *database* gives up a wait before the caller
    stops waiting for it: cancelling the awaiting task does not stop agent-core's worker thread, and
    without a server-side limit a thread blocked on the audit lock holds on for as long as the lock
    does. Options and other settings already in the URL are kept."""
    parts = urlsplit(url)
    # libpq decodes %XX but not "+", and keeps a parameter whose value is blank.
    settings = [
        (unquote(key), unquote(value))
        for key, _, value in (pair.partition("=") for pair in parts.query.split("&") if pair)
    ]
    existing = " ".join(value for key, value in settings if key == "options")
    options = [existing, f"-c search_path={SCHEMA}"] if existing else [f"-c search_path={SCHEMA}"]
    if lock_timeout_ms is not None:
        options.append(f"-c lock_timeout={lock_timeout_ms}")
    if statement_timeout_ms is not None:
        options.append(f"-c statement_timeout={statement_timeout_ms}")
    if transaction_timeout_ms is not None:
        options.append(f"-c transaction_timeout={transaction_timeout_ms}")
    replaced = {"options"} | ({"connect_timeout"} if connect_timeout_s is not None else set())
    query = [(key, value) for key, value in settings if key not in replaced]
    if connect_timeout_s is not None:
        query.append(("connect_timeout", str(connect_timeout_s)))
    query.append(("options", " ".join(options)))
    # libpq decodes %20, not +, in the query of a URL.
    return urlunsplit(parts._replace(query=urlencode(query, quote_via=quote)))


def audit_log_on(database: Database) -> SQLAuditLog:
    """The audit log in the policy schema. agent-core checks the roles of the connection against the
    schema it is told, which is `public` unless it is named."""
    return SQLAuditLog(database, schema=SCHEMA)


def approval_queue_on(
    database: Database, *, policy: ApproverPolicy | None = None
) -> SQLApprovalQueue:
    """The approval queue in the policy schema, with its audit events in the same database."""
    return SQLApprovalQueue(
        database, audit_log=audit_log_on(database), policy=policy, schema=SCHEMA
    )
