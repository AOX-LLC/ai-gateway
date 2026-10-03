"""The audit recorder: what a record holds, and what happens when the log cannot be written."""

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

ARGUMENTS = {"text": "the customer's account number 4111-1111-1111-1111"}
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


class FakeLog:
    """Stands in for SQLAuditLog: keeps what it is given, and fails or stalls on request."""

    def __init__(self) -> None:
        self.appended: list[AuditEvent] = []
        self.batches: list[list[AuditEvent]] = []
        self.mode = "ok"
        self.refuse_events = False
        self.database = SimpleNamespace(run=self._run)

    def checked_event(self, event: AuditEvent) -> AuditEvent:
        if self.refuse_events:
            raise ValueError("the event holds something that looks like a secret")
        return AuditEvent.model_validate(event.model_dump())

    async def append(self, event: AuditEvent) -> None:
        await self._maybe_fail()
        self.appended.append(event)

    async def _run(self, work: Any, *, write: bool = False) -> Any:
        await self._maybe_fail()
        batch: list[AuditEvent] = []
        work(SimpleNamespace(append_into=batch))
        self.batches.append(batch)

    def append_in(self, session: Any, event: AuditEvent) -> None:
        session.append_into.append(event)

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

    assert "4111" not in event.model_dump_json()
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
    assert "4111" not in event.model_dump_json()


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
async def test_a_down_log_keeps_the_batch_and_reports_degraded_then_recovers() -> None:
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
    assert batch[0].payload == {"dropped": 3, "reason": "audit_queue_full"}
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
