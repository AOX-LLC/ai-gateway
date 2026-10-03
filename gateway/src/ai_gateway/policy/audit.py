"""The gateway's audit trail, on agent-core's append-only, hash-chained audit log.

Two paths, with different failure policies (docs/architecture.md, Audit):

- **Before a write is forwarded**, a `gateway.call_started` record is appended and waited for. If it
  cannot be written the write is not made: a write with no record of its attempt is worse than a
  refused write.
- **Every call's decision record** (reads, blocks, and the result of writes) is queued and written
  in batches by a background task. A read is never refused because the audit log is down: the queue
  holds 5 000 records, drops the oldest when full, counts what it dropped, and when the log is back
  the first thing written is an `audit.gap` record saying how many were lost.

agent-core appends one event per transaction and serialises appends: about 22 ms each, a few
dozen a second. A batch of 100 in one transaction takes about 150 ms, so batching is what lets
every call be audited.
"""

import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

import anyio
from aox_agent_core.audit import AuditEvent, SQLAuditLog
from aox_agent_core.context import RunContext

from ai_gateway.pipeline.types import CallContext, ToolCall
from ai_gateway.seams.events import GatewayEvent

logger = logging.getLogger(__name__)

SPOOL_SIZE = 5_000
BATCH_SIZE = 100
FLUSH_INTERVAL_S = 0.5
WRITE_AHEAD_TIMEOUT_S = 2.0
BATCH_TIMEOUT_S = 10.0
_BACKOFF_START_S = 1.0
_BACKOFF_MAX_S = 30.0
_LOG_EVERY_S = 60.0


class AuditUnavailableError(Exception):
    """The audit log cannot take a record now. The reason is for the log, never for a client."""


@dataclass(frozen=True)
class AuditStatus:
    status: str
    """`ok`, `degraded` while writes fail, or `disabled` when no audit database is configured."""
    queue_depth: int = 0
    dropped_total: int = 0
    rejected_total: int = 0
    written_total: int = 0


class AuditRecorder(Protocol):
    async def before_write(self, ctx: CallContext, call: ToolCall) -> None:
        """Append and wait for the record of a write about to be forwarded.

        Raises AuditUnavailableError if that is not possible."""
        ...

    def record(self, event: GatewayEvent) -> None:
        """Queue a call's decision record. Never raises and never waits."""
        ...

    def status(self) -> AuditStatus: ...


class DisabledAuditRecorder:
    """No audit database is configured. Writes are refused unless the configuration allows
    unaudited writes (`[safety] allow_unaudited_writes`)."""

    async def before_write(self, ctx: CallContext, call: ToolCall) -> None:
        raise AuditUnavailableError("no audit database is configured")

    def record(self, event: GatewayEvent) -> None:
        return None

    def status(self) -> AuditStatus:
        return AuditStatus(status="disabled")


def call_event(event: GatewayEvent) -> AuditEvent:
    """The audit event for a `gateway.tool_call` decision record.

    Built from named fields only. Durations are integer microseconds (the audit log refuses
    floats), and nothing of the arguments or the result is in it: only their hash."""
    payload = event.payload
    audit_payload: dict[str, Any] = {
        "request_id": payload.get("request_id"),
        "client_name": payload.get("client_name"),
        "namespace": payload.get("namespace"),
        "effect": payload.get("effect"),
        "effect_source": payload.get("effect_source"),
        "outcome": payload.get("outcome"),
        "blocked_by": payload.get("blocked_by"),
        "deny_code": payload.get("deny_code"),
        "upstream_status": payload.get("upstream_status"),
        "args_sha256": payload.get("arguments_sha256"),
        "config_sha256": payload.get("pipeline_config_sha256"),
        "protocol_version": payload.get("protocol_version"),
        "approval_id": payload.get("approval_id"),
        "duration_us": _microseconds(payload.get("duration_ms")),
        "upstream_duration_us": _microseconds(payload.get("upstream_duration_ms")),
        "layers": [
            {key: layer[key] for key in _LAYER_KEYS if layer.get(key) is not None}
            for layer in _layers(payload.get("layers"))
        ],
    }
    return AuditEvent(
        action="gateway.tool_call",
        actor_id=event.actor_id,
        subject_id=event.subject_id,
        payload={key: value for key, value in audit_payload.items() if value is not None},
        context=_run_context(payload.get("request_id")),
    )


_LAYER_KEYS = ("layer", "hook", "mode", "verdict", "code")


def _layers(value: object) -> list[dict[str, Any]]:
    return [layer for layer in value if isinstance(layer, dict)] if isinstance(value, list) else []


def write_ahead_event(ctx: CallContext, call: ToolCall) -> AuditEvent:
    """The record that a write is about to be forwarded: who, which tool, and the argument hash."""
    return AuditEvent(
        action="gateway.call_started",
        actor_id=ctx.client.actor_id,
        subject_id=call.exposed_name,
        payload={
            "request_id": str(ctx.request_id),
            "client_name": ctx.client.name,
            "namespace": call.namespace,
            "effect": call.effect,
            "args_sha256": call.arguments_sha256,
        },
        context=_run_context(str(ctx.request_id)),
    )


def _microseconds(milliseconds: object) -> int | None:
    if isinstance(milliseconds, int | float) and not isinstance(milliseconds, bool):
        return round(milliseconds * 1000)
    return None


def _run_context(request_id: object) -> RunContext | None:
    try:
        return RunContext(run_id=str(UUID(str(request_id))))
    except ValueError:
        return None


class PostgresAuditRecorder:
    def __init__(
        self,
        log: SQLAuditLog,
        *,
        spool_size: int = SPOOL_SIZE,
        batch_size: int = BATCH_SIZE,
        flush_interval_s: float = FLUSH_INTERVAL_S,
        write_ahead_timeout_s: float = WRITE_AHEAD_TIMEOUT_S,
    ) -> None:
        self._log = log
        self._spool: deque[AuditEvent] = deque(maxlen=spool_size)
        self._spool_size = spool_size
        self._batch_size = batch_size
        self._flush_interval_s = flush_interval_s
        self._write_ahead_timeout_s = write_ahead_timeout_s
        self._pending: list[AuditEvent] = []
        self._dropped_total = 0
        self._unreported_drops = 0
        self._rejected_total = 0
        self._written_total = 0
        self._failing = False
        self._last_logged = 0.0

    def status(self) -> AuditStatus:
        return AuditStatus(
            status="degraded" if self._failing else "ok",
            queue_depth=len(self._spool) + len(self._pending),
            dropped_total=self._dropped_total,
            rejected_total=self._rejected_total,
            written_total=self._written_total,
        )

    async def before_write(self, ctx: CallContext, call: ToolCall) -> None:
        try:
            event = self._log.checked_event(write_ahead_event(ctx, call))
            with anyio.fail_after(self._write_ahead_timeout_s):
                await self._log.append(event)
        except Exception as error:
            # The class only: a driver's message can quote a value.
            raise AuditUnavailableError(type(error).__name__) from None

    def record(self, event: GatewayEvent) -> None:
        try:
            if event.action != "gateway.tool_call":
                return
            if len(self._spool) >= self._spool_size:
                self._dropped_total += 1
                self._unreported_drops += 1
            self._spool.append(self._log.checked_event(call_event(event)))
        except Exception as error:
            self._rejected_total += 1
            self._note("an audit record could not be built (%s)", type(error).__name__)

    async def run(
        self, *, task_status: anyio.abc.TaskStatus[None] = anyio.TASK_STATUS_IGNORED
    ) -> None:
        """Write queued records until cancelled, then make one last, time-boxed attempt."""
        task_status.started()
        backoff = _BACKOFF_START_S
        try:
            while True:
                full = await self._write_next_batch()
                if self._failing:
                    await anyio.sleep(backoff)
                    backoff = min(backoff * 2, _BACKOFF_MAX_S)
                    continue
                backoff = _BACKOFF_START_S
                if not full:
                    await anyio.sleep(self._flush_interval_s)
        finally:
            with anyio.CancelScope(shield=True):
                await self.close()

    async def close(self, timeout_s: float = 5.0) -> None:
        with anyio.move_on_after(timeout_s):
            while self._pending or self._spool:
                await self._write_next_batch()
                if self._failing:
                    break

    async def _write_next_batch(self) -> bool:
        if not self._pending:
            self._pending = self._take_batch()
        if not self._pending:
            return False
        batch = self._pending
        gap = self._gap_event() if self._unreported_drops else None
        to_write = [gap, *batch] if gap is not None else batch
        try:
            with anyio.fail_after(BATCH_TIMEOUT_S):
                await self._log.database.run(
                    lambda session: [self._log.append_in(session, event) for event in to_write],
                    write=True,
                )
        except Exception as error:
            self._failing = True
            self._note("audit writes are failing (%s); %d queued", type(error).__name__, len(self))
            return False
        if gap is not None:
            self._unreported_drops = 0
        self._written_total += len(batch)
        self._pending = []
        self._failing = False
        return len(batch) >= self._batch_size

    def _take_batch(self) -> list[AuditEvent]:
        return [self._spool.popleft() for _ in range(min(self._batch_size, len(self._spool)))]

    def _gap_event(self) -> AuditEvent:
        """Say in the log itself that records were lost, before the ones that follow."""
        return self._log.checked_event(
            AuditEvent(
                action="audit.gap",
                actor_id="gateway",
                payload={"dropped": self._unreported_drops, "reason": "audit_queue_full"},
            )
        )

    def __len__(self) -> int:
        return len(self._spool) + len(self._pending)

    def _note(self, message: str, *args: object) -> None:
        now = time.monotonic()
        if now - self._last_logged >= _LOG_EVERY_S:
            self._last_logged = now
            logger.warning(message, *args)
