"""The audit recorder: what a record holds, and what happens when the log cannot be written."""

import json
import logging
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import anyio
import pytest
from aox_agent_core.audit import AuditEvent

from ai_gateway.pipeline.types import CallContext, ClientIdentity, ToolCall
from ai_gateway.policy import audit as audit_module
from ai_gateway.policy.audit import (
    AuditUnavailableError,
    PostgresAuditRecorder,
    call_event,
    write_ahead_event,
)
from ai_gateway.seams.events import GatewayEvent
from tests.test_upstreams import eventually

MARKER = "the customer's account number 4111-1111-1111-1111"
ARGUMENTS = {"text": MARKER}
CLIENT_ID = uuid4()
REQUEST_ID = uuid4()


def _ctx() -> CallContext:
    return CallContext(
        request_id=REQUEST_ID,
        client=ClientIdentity(id=CLIENT_ID, name="harborline-support-bot", scopes=frozenset()),
        session_id="s",
        protocol_version="2025-11-25",
    )


def _call() -> ToolCall:
    return ToolCall.create("tickets__create_ticket", "tickets", "create_ticket", ARGUMENTS, "write")


def _decision(**payload: Any) -> GatewayEvent:
    base: dict[str, Any] = {
        "request_id": str(REQUEST_ID),
        "client_name": "harborline-support-bot",
        "protocol_version": "2025-11-25",
        "pipeline_config_sha256": "c" * 64,
        "layers": [
            {"layer": "scope", "hook": "before_call", "mode": "enforce", "verdict": "allow",
             "duration_ms": 0.0123},
        ],
        "duration_ms": 12.5,
        "arguments_sha256": "a" * 64,
        "namespace": "tickets",
        "effect": "write",
        "effect_source": "policy",
        "upstream_status": "ok",
        "upstream_duration_ms": 10.25,
        "outcome": "forwarded",
        **payload,
    }  # fmt: skip
    return GatewayEvent(
        action="gateway.tool_call",
        actor_id=f"client:{CLIENT_ID}",
        subject_id="tickets__create_ticket",
        payload=base,
    )


class FakeSession:
    def __init__(self, log: "FakeLog") -> None:
        self.log = log
        self.pending: list[AuditEvent] = []

    def execute(self, sql: str, params: tuple[Any, ...]) -> list[tuple[Any, ...]]:
        """The recorder's one read: the payloads of the newest records after a sequence number."""
        (after_seq,) = params
        return [
            (json.dumps(event.payload),)
            for seq, event in reversed(list(enumerate(self.log.committed, start=1)))
            if seq > after_seq
        ]


class FakeDatabase:
    def __init__(self, log: "FakeLog") -> None:
        self.log = log
        self.busy = False

    async def run(self, work: Any, *, write: bool = False) -> Any:
        await self.log._maybe_fail()
        session = FakeSession(self.log)
        result = work(session)
        if write:
            self.log.committed.extend(session.pending)
            self.log.batches.append(list(session.pending))
            if self.log.mode == "fail_after_commit":
                raise ConnectionError("the connection was lost after COMMIT")
        return result


class FakeLog:
    """Stands in for SQLAuditLog: keeps what it is given, and fails or stalls on request."""

    def __init__(self) -> None:
        self.committed: list[AuditEvent] = []
        self.batches: list[list[AuditEvent]] = []
        self.mode = "ok"
        self.refuse_events = False
        self.database = FakeDatabase(self)

    @property
    def appended(self) -> list[AuditEvent]:
        return self.committed

    def checked_event(self, event: AuditEvent) -> AuditEvent:
        if self.refuse_events:
            raise ValueError("the event holds something that looks like a secret")
        return AuditEvent.model_validate(event.model_dump())

    async def append(self, event: AuditEvent) -> None:
        await self._maybe_fail()
        self.committed.append(event)

    def append_in(self, session: FakeSession, event: AuditEvent) -> Any:
        session.pending.append(event)
        return SimpleNamespace(seq=len(self.committed) + len(session.pending))

    async def _maybe_fail(self) -> None:
        if self.mode == "fail":
            raise RuntimeError("driver says: password for client_secret=hunter2 is wrong")
        if self.mode == "hang":
            await anyio.sleep_forever()


def _recorder(log: FakeLog, **options: Any) -> PostgresAuditRecorder:
    return PostgresAuditRecorder(log, flush_interval_s=0.01, **options)  # type: ignore[arg-type]


# --- what a record holds -------------------------------------------------------------------------


def test_a_call_record_holds_ids_hashes_codes_and_integer_microseconds() -> None:
    event = call_event(_decision())

    assert event.action == "gateway.tool_call"
    assert event.actor_id == f"client:{CLIENT_ID}"
    assert event.subject_id == "tickets__create_ticket"
    assert event.context is not None
    assert event.context.run_id == str(REQUEST_ID)
    payload = event.payload
    assert payload["args_sha256"] == "a" * 64
    assert (payload["duration_us"], payload["upstream_duration_us"]) == (12500, 10250)
    assert payload["layers"] == [
        {"layer": "scope", "hook": "before_call", "mode": "enforce", "verdict": "allow"}
    ]
    assert not any(isinstance(value, float) for value in payload.values())


def test_a_record_has_no_place_for_arguments_or_results() -> None:
    event = call_event(_decision(arguments=ARGUMENTS, result="the result", text="4111"))

    assert MARKER not in event.model_dump_json()
    assert "the result" not in event.model_dump_json()


def test_a_blocked_call_keeps_the_layer_and_code_that_blocked_it() -> None:
    event = call_event(
        _decision(outcome="blocked", blocked_by="scope", deny_code="tool_unavailable")
    )

    assert event.payload["outcome"] == "blocked"
    assert (event.payload["blocked_by"], event.payload["deny_code"]) == (
        "scope",
        "tool_unavailable",
    )


def test_the_write_ahead_record_names_the_attempt_by_hash_only() -> None:
    event = write_ahead_event(_ctx(), _call())

    assert event.action == "gateway.call_started"
    assert event.payload["args_sha256"] == _call().arguments_sha256
    assert MARKER not in event.model_dump_json()


# --- before a write -----------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_write_waits_for_its_record_to_be_stored() -> None:
    log = FakeLog()

    await _recorder(log).before_write(_ctx(), _call())

    assert [event.action for event in log.appended] == ["gateway.call_started"]


@pytest.mark.anyio
async def test_a_failing_log_makes_the_write_unavailable_without_leaking_the_driver_text() -> None:
    log = FakeLog()
    log.mode = "fail"

    with pytest.raises(AuditUnavailableError) as raised:
        await _recorder(log).before_write(_ctx(), _call())

    assert str(raised.value) == "RuntimeError"
    assert "hunter2" not in repr(raised.value)


@pytest.mark.anyio
async def test_a_log_that_does_not_answer_makes_the_write_unavailable_within_the_bound() -> None:
    log = FakeLog()
    log.mode = "hang"

    with anyio.fail_after(3), pytest.raises(AuditUnavailableError) as raised:
        await _recorder(log, write_ahead_timeout_s=0.1).before_write(_ctx(), _call())

    assert str(raised.value) == "TimeoutError"


# --- after the fact -----------------------------------------------------------------------------


def test_recording_a_call_only_queues_it_and_ignores_other_actions() -> None:
    recorder = _recorder(FakeLog())

    recorder.record(_decision())
    recorder.record(GatewayEvent(action="gateway.tools_list", actor_id=f"client:{CLIENT_ID}"))

    assert recorder.status().queue_depth == 1


def test_a_record_that_cannot_be_built_is_counted_and_never_raises(
    caplog: pytest.LogCaptureFixture,
) -> None:
    log = FakeLog()
    log.refuse_events = True  # as the real log does for a payload string that looks like a secret
    recorder = _recorder(log)

    with caplog.at_level(logging.WARNING, logger="ai_gateway.policy.audit"):
        recorder.record(_decision())

    assert recorder.status().rejected_total == 1
    assert recorder.status().queue_depth == 0


@pytest.mark.anyio
async def test_queued_records_are_written_in_batches_in_one_transaction_each() -> None:
    log = FakeLog()
    recorder = _recorder(log, batch_size=10)
    for _ in range(25):
        recorder.record(_decision())

    async with anyio.create_task_group() as tasks:
        await tasks.start(recorder.run)
        await eventually(lambda: recorder.status().written_total == 25, timeout_s=5)
        tasks.cancel_scope.cancel()

    assert [len(batch) for batch in log.batches] == [10, 10, 5]
    assert recorder.status().status == "ok"


@pytest.mark.anyio
async def test_a_down_log_keeps_the_batch_and_reports_degraded_then_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = FakeLog()
    log.mode = "fail"
    recorder = _recorder(log)
    for _ in range(3):
        recorder.record(_decision())
    audit_module._BACKOFF_START_S = 0.05

    async with anyio.create_task_group() as tasks:
        await tasks.start(recorder.run)
        await eventually(lambda: recorder.status().status == "degraded", timeout_s=5)
        assert recorder.status().queue_depth == 3
        log.mode = "ok"
        await eventually(lambda: recorder.status().written_total == 3, timeout_s=5)
        tasks.cancel_scope.cancel()

    assert recorder.status().status == "ok"
    assert sum(len(batch) for batch in log.batches) == 3


@pytest.mark.anyio
async def test_when_the_queue_overflows_the_log_says_how_many_records_were_lost() -> None:
    log = FakeLog()
    recorder = _recorder(log, spool_size=5, batch_size=100)
    for number in range(8):
        recorder.record(_decision(request_id=str(uuid4()), duration_ms=float(number)))

    await recorder.close()

    assert recorder.status().dropped_total == 3
    (batch,) = log.batches
    assert batch[0].action == "audit.gap"
    assert (batch[0].payload["dropped"], batch[0].payload["reason"]) == (3, "audit_queue_full")
    assert [event.action for event in batch[1:]] == ["gateway.tool_call"] * 5


@pytest.mark.anyio
async def test_closing_flushes_what_is_left_and_gives_up_on_a_dead_log() -> None:
    log = FakeLog()
    recorder = _recorder(log)
    recorder.record(_decision())

    await recorder.close()
    assert recorder.status().written_total == 1

    log.mode = "hang"
    recorder.record(_decision())
    with anyio.fail_after(5):
        await recorder.close(timeout_s=0.2)


# --- review findings -----------------------------------------------------------------------------


@pytest.mark.anyio
async def test_drops_counted_while_a_batch_is_in_flight_are_reported_by_a_later_gap() -> None:
    log = FakeLog()
    recorder = _recorder(log, spool_size=3, batch_size=100)
    for _ in range(5):
        recorder.record(_decision(request_id=str(uuid4())))  # 2 dropped
    real_run = log.database.run

    arrived = False

    async def run_and_drop_meanwhile(work: Any, *, write: bool = False) -> Any:
        nonlocal arrived
        if not arrived:
            arrived = True
            for _ in range(3):  # new calls arrive while the first batch is being written
                recorder.record(_decision(request_id=str(uuid4())))
        return await real_run(work, write=write)

    log.database.run = run_and_drop_meanwhile  # type: ignore[method-assign]
    await recorder.close()
    log.database.run = real_run  # type: ignore[method-assign]
    await recorder.close()

    gaps = [e for e in log.committed if e.action == "audit.gap"]
    assert recorder.status().dropped_total == sum(g.payload["dropped"] for g in gaps)  # type: ignore[misc]


@pytest.mark.anyio
async def test_a_batch_that_may_have_committed_is_not_written_twice_on_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = FakeLog()
    log.mode = "fail_after_commit"  # stored, then the connection dies before the answer
    recorder = _recorder(log, batch_size=100)
    for _ in range(3):
        recorder.record(_decision(request_id=str(uuid4())))
    monkeypatch.setattr(audit_module, "_BACKOFF_START_S", 0.01)

    await recorder._write_next_batch()
    assert recorder.status().status == "degraded"
    log.mode = "ok"
    await recorder._write_next_batch()

    assert [e.action for e in log.committed] == ["gateway.tool_call"] * 3, "stored exactly once"
    assert recorder.status().queue_depth == 0


@pytest.mark.anyio
async def test_a_gap_record_is_not_written_twice_when_its_batch_is_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = FakeLog()
    recorder = _recorder(log, spool_size=3, batch_size=100)
    for _ in range(5):
        recorder.record(_decision(request_id=str(uuid4())))  # 2 dropped
    log.mode = "fail_after_commit"
    monkeypatch.setattr(audit_module, "_BACKOFF_START_S", 0.01)

    await recorder._write_next_batch()
    log.mode = "ok"
    await recorder._write_next_batch()

    gaps = [e for e in log.committed if e.action == "audit.gap"]
    assert len(gaps) == 1
    assert gaps[0].payload["dropped"] == 2
    assert recorder.status().dropped_total == 2


@pytest.mark.parametrize(
    "name",
    ["foo bar", "_x", "x/y", "ünicode", "__nope", "a b" * 20, "echo__nope\n", "echo__nope\n\n"],
    # A trailing newline satisfies `$` in a Python pattern, and agent-core's pattern refuses it.
)
def test_a_call_to_a_tool_whose_name_cannot_be_a_subject_is_still_recorded(name: str) -> None:
    event = GatewayEvent(
        action="gateway.tool_call",
        actor_id=f"client:{CLIENT_ID}",
        subject_id=name,
        payload={"request_id": str(REQUEST_ID), "outcome": "blocked", "blocked_by": "catalog"},
    )

    audit_event = call_event(event)

    assert audit_event.subject_id is None
    assert audit_event.payload["tool_name_valid"] is False
    assert len(audit_event.payload["tool_name_sha256"]) == 64  # type: ignore[arg-type]
    assert name not in audit_event.model_dump_json(), "the client's text is stored only as a hash"


def test_a_drop_is_counted_only_for_a_record_that_is_actually_queued() -> None:
    log = FakeLog()
    recorder = _recorder(log, spool_size=1)
    recorder.record(_decision())
    log.refuse_events = True

    recorder.record(_decision())  # refused by the log: rejected, not a drop

    assert (recorder.status().dropped_total, recorder.status().rejected_total) == (0, 1)
    assert recorder.status().queue_depth == 1


@pytest.mark.anyio
async def test_a_failed_write_ahead_record_makes_the_status_degraded_until_one_succeeds() -> None:
    log = FakeLog()
    recorder = _recorder(log)
    log.mode = "fail"
    with pytest.raises(AuditUnavailableError):
        await recorder.before_write(_ctx(), _call())

    assert recorder.status().status == "degraded"
    assert recorder.status().write_ahead_failed_total == 1
    log.mode = "ok"
    await recorder.before_write(_ctx(), _call())
    assert recorder.status().status == "ok"


@pytest.mark.anyio
async def test_a_write_is_refused_at_once_when_every_audit_worker_is_taken() -> None:
    log = FakeLog()
    log.database.busy = True

    with anyio.fail_after(0.5), pytest.raises(AuditUnavailableError, match="busy"):
        await _recorder(log).before_write(_ctx(), _call())

    assert log.committed == []


@pytest.mark.anyio
async def test_the_log_says_why_when_agent_core_does_and_hides_a_drivers_text() -> None:
    from aox_agent_core.errors import ConfigError

    class RefusingLog(FakeLog):
        async def append(self, event: AuditEvent) -> None:
            raise ConfigError("the role can UPDATE the audit table")

    with pytest.raises(AuditUnavailableError) as raised:
        await _recorder(RefusingLog()).before_write(_ctx(), _call())

    assert str(raised.value) == "ConfigError: the role can UPDATE the audit table"


def test_each_kind_of_failure_is_logged_on_its_own_schedule(
    caplog: pytest.LogCaptureFixture,
) -> None:
    log = FakeLog()
    log.refuse_events = True
    recorder = _recorder(log)

    with caplog.at_level(logging.WARNING, logger="ai_gateway.policy.audit"):
        recorder.record(_decision())
        recorder.record(_decision())  # same kind within a minute: not logged again
        recorder._write_ahead_failed("busy")

    assert caplog.text.count("could not be built") == 1
    assert caplog.text.count("write-ahead audit record failed") == 1
