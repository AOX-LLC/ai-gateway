# mypy: ignore-errors
# (written against agent-core v0.1.0a2, whose API differs from the pinned a3)
"""Builds gateway/tests/fixtures/policy_a2.sql: a `policy` schema as agent-core v0.1.0a2 left it.

Not run by the tests. Run it once with agent-core v0.1.0a2 installed (not the pinned a3):

    uv venv /tmp/a2 && uv pip install --python /tmp/a2/bin/python \
        "aox-agent-core[postgres] @ git+https://github.com/AOX-LLC/agent-core@v0.1.0a2"
    OWNER_URL=postgresql://owner:pw@127.0.0.1:4402/postgres /tmp/a2/bin/python \
        gateway/tests/fixtures/make_policy_a2_fixture.py

It creates a scratch database, installs a2's tables, writes audit records and approval requests in
each state through a2's own code, prints the audit head (seq and hash) as it was before any upgrade,
and leaves the database for `pg_dump --inserts --no-owner --no-privileges -n policy`. Everything in
it is made up.
"""

import asyncio
import os
import re
from urllib.parse import quote, urlsplit, urlunsplit
from uuid import uuid4

import psycopg
from aox_agent_core.approvals import Decision, Principal, PrincipalKind, SQLApprovalQueue
from aox_agent_core.audit import AuditEvent, SQLAuditLog
from aox_agent_core.storage import install_postgres_schema, open_database
from pydantic import SecretStr

DATABASE = "a2fixture"
HUMAN = Principal(id="human:fixture", kind=PrincipalKind.HUMAN, roles=frozenset({"approver"}))
CLIENT = Principal(id="client:00000000-0000-4000-8000-000000000001", kind=PrincipalKind.AGENT)


def _with(url: str, database: str, user: str | None = None, password: str | None = None) -> str:
    parts = urlsplit(url)
    netloc = parts.hostname + (f":{parts.port}" if parts.port else "")  # type: ignore[operator]
    if user:
        netloc = f"{user}:{quote(password or '')}@{netloc}"
    elif parts.username:
        netloc = f"{parts.username}:{quote(parts.password or '')}@{netloc}"
    return urlunsplit(parts._replace(netloc=netloc, path=f"/{database}"))


def _searching(url: str) -> str:
    return url + ("&" if "?" in url else "?") + "options=-c%20search_path%3Dpolicy"


async def main() -> None:
    owner = os.environ["OWNER_URL"]
    async with await psycopg.AsyncConnection.connect(owner, autocommit=True) as c:
        await c.execute(f"DROP DATABASE IF EXISTS {DATABASE}")
        await c.execute(f"CREATE DATABASE {DATABASE}")
        for role in ("fixture_gateway", "fixture_approver"):
            await c.execute(f"DROP ROLE IF EXISTS {role}")  # type: ignore[arg-type]
            await c.execute(f"CREATE ROLE {role} LOGIN PASSWORD 'x'")  # type: ignore[arg-type]
    url = _with(owner, DATABASE)
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as c:
        await c.execute("CREATE SCHEMA policy")
        await c.execute("GRANT USAGE ON SCHEMA policy TO fixture_gateway, fixture_approver")
    install_postgres_schema(_searching(url), app_role="fixture_gateway")
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as c:
        await c.execute(
            "GRANT SELECT, INSERT, UPDATE ON policy.agent_core_approvals TO fixture_approver"
        )
        await c.execute("GRANT SELECT, INSERT ON policy.agent_core_audit TO fixture_approver")

    gw = open_database(SecretStr(_searching(_with(owner, DATABASE, "fixture_gateway", "x"))))
    ap = open_database(SecretStr(_searching(_with(owner, DATABASE, "fixture_approver", "x"))))
    log = SQLAuditLog(gw)
    for number in range(1, 6):
        await log.append(
            AuditEvent(
                action="gateway.tool_call",
                actor_id=CLIENT.id,
                subject_id="tickets__get_ticket",
                payload={"request_id": str(uuid4()), "outcome": "forwarded", "n": number},
            )
        )
    queue = SQLApprovalQueue(gw, audit_log=SQLAuditLog(gw))
    approver = SQLApprovalQueue(ap, audit_log=SQLAuditLog(ap))
    made = {}
    for state in ("pending", "approved", "rejected", "consumed"):
        payload = {"ticket_id": f"TKT-{state}", "status": "closed"}
        request = await queue.submit(
            action="tickets__change_status",
            summary=f"a {state} request (fixture)",
            payload=payload,
            requested_by=CLIENT,
            required_role="approver",
            ttl_seconds=7 * 24 * 3600,
        )
        if state != "pending":
            decision = Decision.REJECT if state == "rejected" else Decision.APPROVE
            await approver.resolve(request.id, decision=decision, principal=HUMAN)
        if state == "consumed":
            await queue.consume(
                request.id, action="tickets__change_status", payload=payload, principal=CLIENT
            )
        made[state] = str(request.id)
    head = await log.head()
    print("HEAD", head.seq, head.record_hash)
    print("REQUESTS", made)
    assert re.fullmatch(r"[0-9a-f]{64}", head.record_hash)


asyncio.run(main())
