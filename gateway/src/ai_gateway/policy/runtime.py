"""Build the audit recorder for a running gateway."""

from aox_agent_core.audit import SQLAuditLog
from aox_agent_core.storage import open_database
from pydantic import SecretStr

from ai_gateway.policy import policy_url
from ai_gateway.policy.audit import AuditRecorder, DisabledAuditRecorder, PostgresAuditRecorder
from ai_gateway.settings import GatewaySettings


def build_audit(settings: GatewaySettings) -> AuditRecorder:
    """The recorder on the policy database, or a disabled one when none is configured."""
    if settings.policy_database_url is None:
        return DisabledAuditRecorder()
    url = policy_url(settings.policy_database_url.get_secret_value())
    return PostgresAuditRecorder(SQLAuditLog(open_database(SecretStr(url))))
