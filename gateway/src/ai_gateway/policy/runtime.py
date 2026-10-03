"""Build the audit recorder for a running gateway."""

from aox_agent_core.audit import SQLAuditLog
from pydantic import SecretStr

from ai_gateway.policy import policy_url
from ai_gateway.policy.audit import AuditRecorder, DisabledAuditRecorder, PostgresAuditRecorder
from ai_gateway.policy.database import BoundedPostgresDatabase
from ai_gateway.settings import GatewaySettings

# A request waits for the write-ahead record for 2 s at most (the recorder stops waiting for the
# worker thread, which cannot be interrupted). The database gives up a lock wait at 1.5 s, a
# statement at 1.8 s and the whole transaction at 1.9 s (PostgreSQL 17), so an abandoned thread ends
# soon after and does not commit late. Connecting is bounded separately. The queued batches have
# more time, and one worker.
_WRITE_AHEAD_LOCK_MS, _WRITE_AHEAD_STATEMENT_MS, _WRITE_AHEAD_WORKERS = 1500, 1800, 4
_WRITE_AHEAD_TRANSACTION_MS = 1900
_BATCH_LOCK_MS, _BATCH_STATEMENT_MS, _BATCH_TRANSACTION_MS = 5000, 8000, 12000
_CONNECT_TIMEOUT_S = 2


def build_audit(settings: GatewaySettings) -> AuditRecorder:
    """The recorder on the policy database, or a disabled one when none is configured."""
    if settings.policy_database_url is None:
        return DisabledAuditRecorder()
    base = settings.policy_database_url.get_secret_value()
    write_ahead = SQLAuditLog(
        BoundedPostgresDatabase(
            SecretStr(
                policy_url(
                    base,
                    lock_timeout_ms=_WRITE_AHEAD_LOCK_MS,
                    statement_timeout_ms=_WRITE_AHEAD_STATEMENT_MS,
                    transaction_timeout_ms=_WRITE_AHEAD_TRANSACTION_MS,
                    connect_timeout_s=_CONNECT_TIMEOUT_S,
                )
            ),
            concurrency=_WRITE_AHEAD_WORKERS,
        )
    )
    batches = SQLAuditLog(
        BoundedPostgresDatabase(
            SecretStr(
                policy_url(
                    base,
                    lock_timeout_ms=_BATCH_LOCK_MS,
                    statement_timeout_ms=_BATCH_STATEMENT_MS,
                    transaction_timeout_ms=_BATCH_TRANSACTION_MS,
                    connect_timeout_s=_CONNECT_TIMEOUT_S,
                )
            ),
            concurrency=1,
        )
    )
    return PostgresAuditRecorder(write_ahead, batch_log=batches)
