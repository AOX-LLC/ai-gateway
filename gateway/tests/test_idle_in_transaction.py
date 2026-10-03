"""A session idle inside a transaction is ended by the server, so it cannot hold a lock for good.

Any role that can connect can take agent-core's one audit append lock, and a write that cannot be
audited is refused: one idle-in-transaction session could stop every write. Each role the
gateway's setup creates carries a limit, the policy roles a short one."""

import re
import time
from contextlib import suppress
from uuid import uuid4

import anyio
import psycopg
import pytest
from aox_agent_core.audit import AuditEvent
from aox_agent_core.errors import AgentCoreError
from pydantic import SecretStr

from ai_gateway.policy import POLICY_IDLE_IN_TRANSACTION_MS, audit_log_on, policy_url
from ai_gateway.policy.database import BoundedPostgresDatabase

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

APPEND_LOCK_KEY = 0x6167656E74636F72
"""agent-core's key for `pg_advisory_xact_lock` on appends (audit.sql.APPEND_LOCK_KEY)."""
POLICY_ROLES = ("policy_gateway", "policy_approver", "policy_auditor")
OTHER_ROLES = ("gateway_app", "telemetry_writer", "telemetry_reader", "telemetry_purger")
"""The roles this test's fixtures set up: the gateway's own (a migration), and the telemetry roles
(`ensure_role`, which every server's setup uses too, and which carries the default)."""


def _setting(config: list[str] | None) -> int | None:
    """The role's idle-in-transaction limit in milliseconds, or None if it has none."""
    for entry in config or []:
        name, _, value = entry.partition("=")
        if name == "idle_in_transaction_session_timeout":
            number, unit = re.fullmatch(r"(\d+)(ms|s|min)", value).groups()  # type: ignore[union-attr]
            return int(number) * {"ms": 1, "s": 1000, "min": 60_000}[unit]
    return None


async def test_every_role_has_a_limit_and_the_policy_roles_the_shortest(
    telemetry: None, policy: None, test_database_url: str
) -> None:
    async with await psycopg.AsyncConnection.connect(test_database_url) as connection:
        cursor = await connection.execute(
            "SELECT rolname, rolconfig FROM pg_roles WHERE rolname = ANY(%s)",
            ([*POLICY_ROLES, *OTHER_ROLES],),
        )
        configs = {name: _setting(config) for name, config in await cursor.fetchall()}

    assert set(POLICY_ROLES) <= set(configs), "the policy roles exist"
    for role in POLICY_ROLES:
        assert configs[role] == POLICY_IDLE_IN_TRANSACTION_MS, role
    assert [role for role, value in configs.items() if value is None] == [], "none without one"
    for role, value in configs.items():
        if role not in POLICY_ROLES:
            assert value is not None
            assert 0 < value <= 30_000, f"{role}: at most the default of 30 s"


@pytest.mark.parametrize("role", POLICY_ROLES)
async def test_an_idle_transaction_from_a_policy_role_is_ended_and_a_write_then_succeeds(
    role: str,
    policy: None,
    policy_gateway_url: str,
    policy_approver_url: str,
    policy_auditor_url: str,
    test_database_url: str,
) -> None:
    urls = {
        "policy_gateway": policy_gateway_url,
        "policy_approver": policy_approver_url,
        "policy_auditor": policy_auditor_url,
    }
    # The gateway's own write, with the waits it really uses for a write's audit record.
    write_ahead = audit_log_on(
        BoundedPostgresDatabase(
            SecretStr(
                policy_url(policy_gateway_url, lock_timeout_ms=1500, statement_timeout_ms=1800)
            ),
            concurrency=1,
        )
    )

    def event() -> AuditEvent:
        return AuditEvent(
            action="gateway.call_started",
            actor_id=f"client:{uuid4()}",
            subject_id="tickets__create_ticket",
            payload={"request_id": str(uuid4())},
        )

    stuck = await psycopg.AsyncConnection.connect(policy_url(urls[role]))
    try:
        await stuck.execute("SELECT pg_advisory_xact_lock(%s)", (APPEND_LOCK_KEY,))
        started = time.monotonic()  # now idle inside the transaction, holding the append lock

        # Blocked: the write's audit record cannot be written, so the write would be refused.
        with pytest.raises((psycopg.errors.LockNotAvailable, AgentCoreError)):
            await write_ahead.append(event())

        # Watch the session from outside: using it would be activity, which resets the timer.
        pid = stuck.info.backend_pid
        async with await psycopg.AsyncConnection.connect(
            test_database_url, autocommit=True
        ) as watcher:
            for _ in range(int((POLICY_IDLE_IN_TRANSACTION_MS / 1000 + 4) / 0.25)):
                cursor = await watcher.execute(
                    "SELECT 1 FROM pg_stat_activity WHERE pid = %s", (pid,)
                )
                if await cursor.fetchone() is None:
                    break
                await anyio.sleep(0.25)
            else:
                raise AssertionError("the idle transaction was never ended")
        with pytest.raises(psycopg.errors.IdleInTransactionSessionTimeout):
            await stuck.execute("SELECT 1")  # and the server says why the session is gone
    finally:
        with suppress(psycopg.Error):
            await stuck.close()
        ended_after = time.monotonic() - started

    limit = POLICY_IDLE_IN_TRANSACTION_MS / 1000
    assert ended_after >= limit - 1.0, "not ended at once: it is a limit, not a ban on transactions"
    assert ended_after <= limit + 2.5, "ended within the limit"
    await write_ahead.append(event())  # the lock is free: a write succeeds
