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

import json
import logging
import re
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID, uuid4

import anyio
from aox_agent_core.audit import AuditEvent, SQLAuditLog
from aox_agent_core.context import RunContext
from aox_agent_core.errors import AgentCoreError

from ai_gateway.pipeline.types import CallContext, ToolCall
from ai_gateway.seams.events import GatewayEvent
from ai_gateway.text import sha256_of_name

logger = logging.getLogger(__name__)

SPOOL_SIZE = 5_000
BATCH_SIZE = 100
FLUSH_INTERVAL_S = 0.5
WRITE_AHEAD_TIMEOUT_S = 2.0
BATCH_TIMEOUT_S = 15.0
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
    write_ahead_failed_total: int = 0


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


_SUBJECT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}")
"""agent-core's subject pattern. Matched with `fullmatch`: `$` also matches before a trailing
newline, which agent-core's pattern does not, and a name that passed here and failed there went
unaudited."""


def _subject(tool_name: str | None) -> tuple[str | None, dict[str, Any]]:
    """The audit subject for a tool name, and what to record when the name cannot be one.

    A client chooses the name of a tool that does not exist, and agent-core accepts only plain
    identifiers as subjects. Such a call is still recorded: with no subject, and the name's hash."""
    if tool_name is None or _SUBJECT_ID.fullmatch(tool_name):
        return tool_name, {}
    return None, {
        "tool_name_valid": False,
        "tool_name_sha256": sha256_of_name(tool_name),
    }


def call_event(event: GatewayEvent, record_id: str | None = None) -> AuditEvent:
    """The audit event for a `gateway.tool_call` decision record.

    Built from named fields only. Durations are integer microseconds (the audit log refuses
    floats), and nothing of the arguments or the result is in it: only their hash."""
    payload = event.payload
    if payload.get("blocked_by") == "catalog":
        # A tool no upstream offers: the name is the client's, so it is never the subject. Its
        # hash (of the whole name, as sent) says which name it was.
        subject_id, subject_extra = (
            None,
            {
                "tool_name_valid": False,
                "tool_name_sha256": payload.get("tool_name_sha256"),
            },
        )
    else:
        subject_id, subject_extra = _subject(event.subject_id)
    audit_payload: dict[str, Any] = {
        "record_id": record_id or str(uuid4()),
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
        **subject_extra,
    }
    return AuditEvent(
        action="gateway.tool_call",
        actor_id=event.actor_id,
        subject_id=subject_id,
        payload={key: value for key, value in audit_payload.items() if value is not None},
        context=_run_context(payload.get("request_id")),
    )


_LAYER_KEYS = ("layer", "hook", "mode", "verdict", "code")


def _layers(value: object) -> list[dict[str, Any]]:
    return [layer for layer in value if isinstance(layer, dict)] if isinstance(value, list) else []


def write_ahead_event(ctx: CallContext, call: ToolCall) -> AuditEvent:
    """The record that a write is about to be forwarded: who, which tool, and the argument hash."""
    subject_id, subject_extra = _subject(call.exposed_name)
    return AuditEvent(
        action="gateway.call_started",
        actor_id=ctx.client.actor_id,
        subject_id=subject_id,
        payload={
            "record_id": str(uuid4()),
            "request_id": str(ctx.request_id),
            "client_name": ctx.client.name,
            "namespace": call.namespace,
            "effect": call.effect,
            "args_sha256": call.arguments_sha256,
            **subject_extra,
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


def _reason(error: Exception) -> str:
    """Why the log failed, for the operator's log: agent-core's own errors say (its messages never
    echo input), a driver's error is reduced to its class, since a driver can quote a value."""
    if isinstance(error, AgentCoreError):
        return f"{type(error).__name__}: {error}"
    return type(error).__name__


class PostgresAuditRecorder:
    def __init__(
        self,
        log: SQLAuditLog,
        *,
        batch_log: SQLAuditLog | None = None,
        spool_size: int = SPOOL_SIZE,
        batch_size: int = BATCH_SIZE,
        flush_interval_s: float = FLUSH_INTERVAL_S,
        write_ahead_timeout_s: float = WRITE_AHEAD_TIMEOUT_S,
    ) -> None:
        self._log = log
        self._batch_log = batch_log or log
        self._spool: deque[AuditEvent] = deque(maxlen=spool_size)
        self._spool_size = spool_size
        self._batch_size = batch_size
        self._flush_interval_s = flush_interval_s
        self._write_ahead_timeout_s = write_ahead_timeout_s
        self._pending: list[AuditEvent] = []
        self._pending_gap: tuple[AuditEvent, int] | None = None
        """The gap record of the batch in flight and the drops it reports: built once, so a retry
        sends the same record_id and is deduplicated, not written twice."""
        self._dropped_total = 0
        self._unreported_drops = 0
        self._rejected_total = 0
        self._written_total = 0
        self._write_ahead_failed_total = 0
        self._write_ahead_failing = False
        self._failing = False
        self._maybe_committed = False
        """The last batch failed after it may have been sent: it may be in the log already."""
        self._last_seq = 0
        self._noted: dict[str, float] = {}

    def status(self) -> AuditStatus:
        return AuditStatus(
            status="degraded" if self._failing or self._write_ahead_failing else "ok",
            queue_depth=len(self._spool) + len(self._pending),
            dropped_total=self._dropped_total,
            rejected_total=self._rejected_total,
            written_total=self._written_total,
            write_ahead_failed_total=self._write_ahead_failed_total,
        )

    async def before_write(self, ctx: CallContext, call: ToolCall) -> None:
        if getattr(self._log.database, "busy", False):
            # Every audit worker is taken: a write would queue behind them. Refuse it now.
            self._write_ahead_failed("busy")
            raise AuditUnavailableError("the audit workers are busy")
        try:
            event = self._log.checked_event(write_ahead_event(ctx, call))
            with anyio.fail_after(self._write_ahead_timeout_s):
                await self._log.append(event)
        except Exception as error:
            reason = _reason(error)
            self._write_ahead_failed(reason)
            raise AuditUnavailableError(reason) from None
        self._write_ahead_failing = False

    def _write_ahead_failed(self, reason: str) -> None:
        self._write_ahead_failing = True
        self._write_ahead_failed_total += 1
        self._note("write-ahead", "a write-ahead audit record failed (%s)", reason)

    def record(self, event: GatewayEvent) -> None:
        try:
            if event.action != "gateway.tool_call":
                return
            checked = self._log.checked_event(call_event(event))
        except Exception as error:
            self._rejected_total += 1
            self._note("rejected", "an audit record could not be built (%s)", _reason(error))
            return
        if len(self._spool) >= self._spool_size:
            self._dropped_total += 1
            self._unreported_drops += 1
        self._spool.append(checked)

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
        if self._pending_gap is None and self._unreported_drops:
            self._pending_gap = (self._gap_event(self._unreported_drops), self._unreported_drops)
        gap, reported = self._pending_gap or (None, 0)
        try:
            with anyio.fail_after(BATCH_TIMEOUT_S):
                to_write = [gap, *self._pending] if gap is not None else list(self._pending)
                if self._maybe_committed:
                    to_write = await self._without_those_already_written(to_write)
                records = await self._batch_log.database.run(
                    lambda session: [self._batch_log.append_in(session, e) for e in to_write],
                    write=True,
                )
        except Exception as error:
            self._failing = True
            self._maybe_committed = True
            self._note(
                "batch", "audit writes are failing (%s); %d queued", _reason(error), len(self)
            )
            return False
        if records:
            self._last_seq = records[-1].seq
        if gap is not None:
            self._unreported_drops -= reported  # drops counted while this batch was in flight stay
            self._pending_gap = None
        self._written_total += len(self._pending)
        full = len(self._pending) >= self._batch_size
        self._pending = []
        self._failing = False
        self._maybe_committed = False
        return full

    async def _without_those_already_written(self, events: list[AuditEvent]) -> list[AuditEvent]:
        """After a failure whose outcome is unknown, drop the events that did reach the log.

        A timeout, or a connection lost after COMMIT, leaves it open whether the batch was stored;
        retrying blindly would store it twice. Each event carries a `record_id`, and the newest
        records are searched for them."""
        last_seq = self._last_seq

        def read(session: Any) -> list[tuple[Any, ...]]:
            rows: list[tuple[Any, ...]] = session.execute(
                "SELECT payload FROM policy.agent_core_audit WHERE seq > ? ORDER BY seq DESC",
                (last_seq,),
            )
            return rows

        rows = await self._batch_log.database.run(read)
        present = set()
        for (payload,) in rows:
            try:
                record_id = json.loads(payload).get("record_id")
            except (ValueError, AttributeError):
                continue
            if isinstance(record_id, str):
                present.add(record_id)
        return [event for event in events if event.payload.get("record_id") not in present]

    def _take_batch(self) -> list[AuditEvent]:
        return [self._spool.popleft() for _ in range(min(self._batch_size, len(self._spool)))]

    def _gap_event(self, dropped: int) -> AuditEvent:
        """Say in the log itself that records were lost, before the ones that follow."""
        return self._batch_log.checked_event(
            AuditEvent(
                action="audit.gap",
                actor_id="gateway",
                payload={
                    "record_id": str(uuid4()),
                    "dropped": dropped,
                    "reason": "audit_queue_full",
                },
            )
        )

    def __len__(self) -> int:
        return len(self._spool) + len(self._pending)

    def _note(self, key: str, message: str, *args: object) -> None:
        """Log at most once a minute per kind of message, so one failure cannot hide another."""
        now = time.monotonic()
        if now - self._noted.get(key, float("-inf")) >= _LOG_EVERY_S:
            self._noted[key] = now
            logger.warning(message, *args)
