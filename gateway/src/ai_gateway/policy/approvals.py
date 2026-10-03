"""The approval gate: a write waits for a person, and runs only on an approval made for it.

For a write the gateway finds this client's request for exactly this tool and these arguments, or
submits one; holds for a decision for 45 s; and answers "pending" if there is none yet, so the
client retries the same call. When a person has approved, the approval is consumed, once, right
before the call is forwarded, after the gateway has checked that this client is the one who asked
(agent-core a3's consume checks it too; the gate's own check is a second one, and fails the call
with a log line of its own).

The full arguments are stored for the approver in `policy.approval_arguments` (the gateway can
insert them and never read them back) so that a person approves what the call really says. That
is the one place the gateway keeps arguments; they are purged after 7 days and never go to the
audit log or telemetry.
"""

import asyncio
import json
import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import anyio
from aox_agent_core.approvals import (
    ApprovalRequest,
    ApprovalStatus,
    Principal,
    PrincipalKind,
    SQLApprovalQueue,
    approval_payload_hash,
)
from aox_agent_core.errors import AgentCoreError, ApprovalExpiredError
from psycopg.errors import UniqueViolation

from ai_gateway.pipeline.types import CallContext, ToolCall
from ai_gateway.policy import ARGUMENTS_PURGE_FUNCTION, SCHEMA
from ai_gateway.policy.database import BoundedPostgresDatabase
from ai_gateway.seams.approvals import ApprovalDecision, ApprovalOutcome
from ai_gateway.text import printable

logger = logging.getLogger(__name__)

TTL_S = 30 * 60
HOLD_S = 45.0
POLL_S = 1.0
MAX_HOLDS = 16
"""Calls held at once. Past it a call is answered "pending" at once and the client retries."""
MAX_HOLDS_PER_CLIENT = 4
"""Of those, how many one client may hold: one client cannot fill every wait slot."""
MAX_ARGUMENT_BYTES = 65_536
PURGE_EVERY_S = 3600.0
EXPIRE_EVERY_S = 60.0


def _now_text() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class ApprovalConflictError(Exception):
    """An open request matches this call's requester, tool and payload but not its role, lifetime
    or delegates: it is not reused, and nothing is approved."""


def _payload(call: ToolCall) -> dict[str, Any]:
    """What a person approves, and what is hashed: the arguments and which upstream they go to."""
    return {"arguments": call.arguments, "upstream": call.upstream_identity}


class PostgresApprovalGate:
    def __init__(
        self,
        queue: SQLApprovalQueue,
        *,
        ttl_s: int = TTL_S,
        hold_s: float = HOLD_S,
        poll_s: float = POLL_S,
        max_holds: int = MAX_HOLDS,
        max_holds_per_client: int = MAX_HOLDS_PER_CLIENT,
        roles_by_action: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._queue = queue
        self._database = queue.database
        self._ttl_s = ttl_s
        self._hold_s = hold_s
        self._poll_s = poll_s
        self._max_holds = max_holds
        self._max_holds_per_client = max_holds_per_client
        self._clock = clock or (lambda: datetime.now(UTC))
        self._holding: dict[str, int] = {}
        self._roles_by_action = dict(roles_by_action or {})
        """The role a person must hold to decide each write. The gateway asks only for a write that
        is listed, and asks for exactly that role: the approver's side reads the same list."""

    async def decide(self, ctx: CallContext, call: ToolCall) -> ApprovalDecision:
        try:
            return await self._decide(ctx, call)
        except Exception as error:  # the queue must never turn into a 500 or an open door
            reason = (
                f"{type(error).__name__}: {error}"
                if isinstance(error, AgentCoreError)
                else (type(error).__name__)
            )
            logger.error("approval for request %s unavailable (%s)", ctx.request_id, reason)
            return ApprovalDecision(ApprovalOutcome.UNAVAILABLE)

    async def _decide(self, ctx: CallContext, call: ToolCall) -> ApprovalDecision:
        requester = Principal(id=ctx.client.actor_id, kind=PrincipalKind.SERVICE)
        payload = _payload(call)
        payload_hash = approval_payload_hash(call.exposed_name, payload)
        request = await self._find_or_submit(ctx, call, requester, payload, payload_hash)
        if request is None:
            return ApprovalDecision(ApprovalOutcome.UNAVAILABLE)
        approval_id = str(request.id)

        holding = (
            request.status is ApprovalStatus.PENDING
            and sum(self._holding.values()) < self._max_holds
            and self._holding.get(requester.id, 0) < self._max_holds_per_client
        )
        if holding:
            self._holding[requester.id] = self._holding.get(requester.id, 0) + 1
        try:
            if holding:
                request = await self._hold(request)
        finally:
            if holding:
                self._holding[requester.id] -= 1
                if not self._holding[requester.id]:
                    del self._holding[requester.id]

        if request.status is ApprovalStatus.REJECTED:
            return ApprovalDecision(ApprovalOutcome.REJECTED, approval_id)
        if request.status is ApprovalStatus.PENDING:
            if request.is_expired(self._clock()):
                return ApprovalDecision(ApprovalOutcome.EXPIRED, approval_id)
            return ApprovalDecision(ApprovalOutcome.PENDING, approval_id)
        if request.status is ApprovalStatus.EXPIRED:
            return ApprovalDecision(ApprovalOutcome.EXPIRED, approval_id)
        if request.status is not ApprovalStatus.APPROVED:
            return ApprovalDecision(ApprovalOutcome.UNAVAILABLE, approval_id)
        return await self._use(request, call, requester, payload)

    async def expire_due(self, principal: Principal) -> int:
        return await self._queue.expire_due(principal=principal)

    async def _hold(self, request: ApprovalRequest) -> ApprovalRequest:
        """Poll until the request is decided or the hold is over. There is no wait/notify."""
        with anyio.move_on_after(self._hold_s):
            while request.status is ApprovalStatus.PENDING:
                await anyio.sleep(self._poll_s)
                request = await self._queue.get(request.id)
                if request.is_expired(self._clock()):
                    break
        return request

    async def _use(
        self,
        request: ApprovalRequest,
        call: ToolCall,
        requester: Principal,
        payload: dict[str, Any],
    ) -> ApprovalDecision:
        approval_id = str(request.id)
        # The queue checks the tool, the arguments and the requester; checking whose request it was
        # here as well costs nothing and says which client tried.
        if request.requested_by != requester.id:
            logger.error("approval %s was asked for by another client; refused", approval_id)
            return ApprovalDecision(ApprovalOutcome.UNAVAILABLE, approval_id)
        try:
            await self._queue.consume(
                request.id, action=call.exposed_name, payload=payload, principal=requester
            )
        except ApprovalExpiredError:
            return ApprovalDecision(ApprovalOutcome.EXPIRED, approval_id)
        except AgentCoreError as error:
            # Already used (a retry raced this one), or the arguments differ: not approved.
            logger.warning("approval %s not used: %s", approval_id, type(error).__name__)
            return ApprovalDecision(ApprovalOutcome.UNAVAILABLE, approval_id)
        return ApprovalDecision(ApprovalOutcome.APPROVED, approval_id)

    async def _find_or_submit(
        self,
        ctx: CallContext,
        call: ToolCall,
        requester: Principal,
        payload: dict[str, Any],
        payload_hash: str,
    ) -> ApprovalRequest | None:
        """This call's open request, or a new one; identical concurrent calls share one.

        The sharing is the database's, not a check followed by an insert: a partial unique index on
        (requester, action, payload hash) over pending and approved requests (`setup.py`) makes the
        second of two simultaneous submissions fail, and it then finds the first's request.
        REPLACE WITH AGENT-CORE A5'S INDEX, which enforces the same rule in the library.

        A repeat that matches the requester, tool and payload but differs in role, lifetime or
        delegates is a conflict, not a reuse (ApprovalConflictError)."""
        for _ in range(3):
            request = await self._find(requester.id, payload_hash)
            if request is not None:
                self._same_intent(call, request)
                return request
            try:
                return await self._submit(ctx, call, requester, payload)
            except UniqueViolation:
                # An open request for this intent exists: the next find returns it. If it is only
                # past its lifetime (stored as pending until the sweep), store that and try again.
                await self.expire_due(Principal(id="service:gateway", kind=PrincipalKind.SERVICE))
        raise ApprovalConflictError("an open request for this call could be neither found nor made")

    def _same_intent(self, call: ToolCall, request: ApprovalRequest) -> None:
        lifetime = (request.expires_at - request.created_at).total_seconds()
        if (
            request.required_role != self._roles_by_action.get(call.exposed_name)
            or request.delegates
            or lifetime != self._ttl_s
        ):
            raise ApprovalConflictError(
                f"request {request.id} is for this call but differs in role, lifetime or delegates"
            )

    async def _find(self, requested_by: str, payload_hash: str) -> ApprovalRequest | None:
        """This client's newest unexpired request for this tool and these arguments that is still
        pending, approved or rejected. A rejection stands until it expires, so a retry of a
        rejected call does not ask again."""

        def read(session: Any) -> list[tuple[Any, ...]]:
            rows: list[tuple[Any, ...]] = session.execute(
                "SELECT id FROM policy.agent_core_approvals"
                " WHERE requested_by = ? AND payload_sha256 = ?"
                " AND status IN ('pending', 'approved', 'rejected') AND expires_at > ?"
                " ORDER BY (status = 'approved') DESC, created_at DESC LIMIT 1",
                (requested_by, payload_hash, _now_text()),
            )
            return rows

        rows = await self._database.run(read)
        if not rows:
            return None
        return await self._queue.get(UUID(str(rows[0][0])))

    async def _submit(
        self, ctx: CallContext, call: ToolCall, requester: Principal, payload: dict[str, Any]
    ) -> ApprovalRequest | None:
        text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        if len(text.encode()) > MAX_ARGUMENT_BYTES:
            logger.error("approval for request %s refused: arguments too large", ctx.request_id)
            return None
        role = self._roles_by_action.get(call.exposed_name)
        if role is None:
            logger.error(
                "no approver role is set for %s: a write nobody may approve", call.exposed_name
            )
            return None
        # No delegates: only this client may use the approval it asked for.
        request = await self._queue.submit(
            action=call.exposed_name,
            summary=printable(f"{call.exposed_name} for client {ctx.client.name}", 500),
            payload=payload,
            requested_by=requester,
            required_role=role,
            ttl_seconds=self._ttl_s,
        )
        # Without its arguments nobody can be shown what to approve, and the approver's tool
        # refuses such a request: an orphan stays pending until it expires.
        await self._database.run(
            lambda session: session.execute(
                "INSERT INTO policy.approval_arguments (request_id, arguments_json) VALUES (?, ?)",
                (str(request.id), text),
            ),
            write=True,
        )
        return request


async def purge_arguments_forever(
    database: BoundedPostgresDatabase, interval_s: float = PURGE_EVERY_S
) -> None:
    """Delete stored arguments past their retention, now and then. Failing to is logged: the
    next round tries again."""
    while True:
        try:
            await database.run(
                lambda session: session.execute(
                    f"SELECT {SCHEMA}.{ARGUMENTS_PURGE_FUNCTION}()", ()
                ),
                write=True,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning("purging approval arguments failed (%s)", type(error).__name__)
        await anyio.sleep(interval_s)


async def expire_due_forever(
    gate: "PostgresApprovalGate", interval_s: float = EXPIRE_EVERY_S
) -> None:
    """Store `expired` on requests past their lifetime, now and then. Reads treat such a request as
    expired whether or not this has run, so a failure here is only logged."""
    principal = Principal(id="service:gateway", kind=PrincipalKind.SERVICE)
    while True:
        try:
            count = await gate.expire_due(principal)
            if count:
                logger.info("%d approval request(s) expired", count)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning("expiring approval requests failed (%s)", type(error).__name__)
        await anyio.sleep(interval_s)
