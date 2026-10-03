"""The policy engine's storage: the audit log and the approval queue, in the `policy` schema.

Both are agent-core's (https://github.com/AOX-LLC/agent-core, pinned by tag): an append-only,
hash-chained audit log and a human-approval queue, on Postgres. This package creates their
schema and three roles, and adapts them to the gateway's needs.
"""

from urllib.parse import quote, unquote, urlencode, urlsplit, urlunsplit

SCHEMA = "policy"
GATEWAY_ROLE = "policy_gateway"
"""The gateway: appends audit records, asks for approvals and consumes approved ones."""
APPROVER_ROLE = "policy_approver"
"""A person's tool (gateway-admin approvals, Phase 6's lab approver): decides pending requests."""
AUDITOR_ROLE = "policy_auditor"
"""Reads the audit log, to verify it and to take anchors."""
ROLES = (GATEWAY_ROLE, APPROVER_ROLE, AUDITOR_ROLE)

AUDIT_TABLE = "agent_core_audit"
APPROVALS_TABLE = "agent_core_approvals"
ARGUMENTS_TABLE = "approval_arguments"
"""The full arguments of a write that awaits approval, for the approver only: the one place the
gateway keeps arguments, and the one exception to "never store arguments"."""
APPROVERS_TABLE = "approvers"
DASHBOARD_VIEW = "dash_approvals"
"""The approval requests as the dashboard may see them: no arguments, no reasons."""
ARGUMENTS_PURGE_FUNCTION = "purge_approval_arguments"
ARGUMENTS_RETENTION_DAYS = 7


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
