"""The policy engine's storage: the audit log and the approval queue, in the `policy` schema.

Both are agent-core's (https://github.com/AOX-LLC/agent-core, pinned by tag): an append-only,
hash-chained audit log and a human-approval queue, on Postgres. This package creates their
schema and three roles, and adapts them to the gateway's needs.
"""

from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

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


def policy_url(url: str) -> str:
    """The URL with the policy schema first on the search path: agent-core's table names are
    unqualified. It stays a URL, which is what agent-core's `open_database` takes."""
    parts = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parts.query) if key != "options"]
    query.append(("options", f"-c search_path={SCHEMA}"))
    # libpq decodes %20, not +, in the query of a URL.
    return urlunsplit(parts._replace(query=urlencode(query, quote_via=quote)))
