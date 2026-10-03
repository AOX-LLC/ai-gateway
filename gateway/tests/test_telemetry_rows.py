"""Turning events and spans into telemetry rows, the buffer, and the sinks around them."""

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from ai_gateway.seams.events import EventSink, GatewayEvent
from ai_gateway.telemetry.buffer import Row, TelemetryBuffer
from ai_gateway.telemetry.rows import rows_for_event, span_row
from ai_gateway.telemetry.sinks import FanOutEventSink, PostgresEventSink, SafeEventSink
from ai_gateway.telemetry.spans import BufferSpanProcessor

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
REQUEST_ID = uuid4()
CLIENT_ID = uuid4()
SHA = "d" * 64
ARGUMENTS = "the customer's secret account number 4111-1111-1111-1111"


def _call_event(**payload: Any) -> GatewayEvent:
    base: dict[str, Any] = {
        "request_id": str(REQUEST_ID),
        "client_name": "harborline-support-bot",
        "protocol_version": "2025-11-25",
        "pipeline_config_sha256": SHA,
        "enabled_layers": ["scope"],
        "layers": [
            {"layer": "scope", "hook": "before_call", "mode": "enforce", "verdict": "allow",
             "duration_ms": 0.01},
            {"layer": "scope", "hook": "after_call", "mode": "enforce", "verdict": "allow",
             "duration_ms": 0.0},
        ],
        "duration_ms": 12.5,
        "arguments_sha256": SHA,
        "namespace": "tickets",
        "effect": "read",
        "effect_source": "policy",
        "upstream_status": "ok",
        "upstream_duration_ms": 10.0,
        "outcome": "forwarded",
        "trace_id": "f" * 32,
        **payload,
    }  # fmt: skip
    return GatewayEvent(
        action="gateway.tool_call",
        actor_id=f"client:{CLIENT_ID}",
        subject_id="tickets__get_ticket",
        payload=base,
        occurred_at=NOW,
    )


def _by_table(rows: list[Row]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row.table, []).append(row.values)
    return grouped


# --- rows ----------------------------------------------------------------------------------


def test_a_forwarded_call_becomes_a_request_row_and_one_row_per_layer_verdict() -> None:
    grouped = _by_table(rows_for_event(_call_event()))

    (request,) = grouped["requests"]
    assert request["request_id"] == REQUEST_ID
    assert request["client_id"] == CLIENT_ID
    assert request["client_name"] == "harborline-support-bot"
    assert (request["tool"], request["namespace"]) == ("tickets__get_ticket", "tickets")
    assert (request["outcome"], request["effect"], request["upstream_status"]) == (
        "forwarded",
        "read",
        "ok",
    )
    assert request["upstream_duration_ms"] == 10.0
    assert request["trace_id"] == "f" * 32
    assert [(v["ordinal"], v["layer"], v["hook"]) for v in grouped["layer_verdicts"]] == [
        (0, "scope", "before_call"),
        (1, "scope", "after_call"),
    ]


def test_a_blocked_call_keeps_the_layer_and_the_deny_code() -> None:
    event = _call_event(
        outcome="blocked",
        blocked_by="scope",
        deny_code="tool_unavailable",
        layers=[
            {"layer": "scope", "hook": "before_call", "mode": "enforce", "verdict": "deny",
             "code": "tool_unavailable"},
        ],
    )  # fmt: skip

    grouped = _by_table(rows_for_event(event))

    assert grouped["requests"][0]["blocked_by"] == "scope"
    assert grouped["requests"][0]["deny_code"] == "tool_unavailable"
    assert grouped["layer_verdicts"][0]["code"] == "tool_unavailable"


def test_a_listing_is_a_request_with_the_outcome_listed() -> None:
    event = GatewayEvent(
        action="gateway.tools_list",
        actor_id=f"client:{CLIENT_ID}",
        payload={
            "request_id": str(REQUEST_ID),
            "client_name": "harborline-ops-bot",
            "duration_ms": 1.0,
            "layers": [],
            "tools_available": 11,
            "tools_returned": 9,
        },
        occurred_at=NOW,
    )

    (request,) = _by_table(rows_for_event(event))["requests"]

    assert (request["kind"], request["outcome"], request["tool"]) == ("tools_list", "listed", None)
    assert (request["tools_available"], request["tools_returned"]) == (11, 9)


def test_an_auth_failure_row_holds_the_reason_and_the_lookup_id_and_no_address() -> None:
    event = GatewayEvent(
        action="gateway.auth_failure",
        actor_id="anonymous",
        payload={"reason": "wrong_secret", "lookup_id": "abcd2345", "remote_addr": "203.0.113.9"},
        occurred_at=NOW,
    )

    (failure,) = _by_table(rows_for_event(event))["auth_failures"]

    assert (failure["reason"], failure["lookup_id"]) == ("wrong_secret", "abcd2345")
    assert "203.0.113.9" not in repr(failure)
    assert "remote_addr" not in failure
    assert isinstance(failure["event_id"], UUID)


def test_an_unknown_action_is_not_stored() -> None:
    event = GatewayEvent(action="gateway.something_else", actor_id="anonymous")

    assert rows_for_event(event) == []


def test_text_is_cut_to_the_column_and_a_hostile_tool_name_is_not_stored() -> None:
    event = _call_event(client_name="c" * 500, namespace="Not A Namespace!")
    hostile = GatewayEvent(
        action="gateway.tool_call",
        actor_id="client:" + str(CLIENT_ID),
        subject_id="'; DROP TABLE requests; --",
        payload={**event.payload, "namespace": "tickets"},
        occurred_at=NOW,
    )

    (request,) = _by_table(rows_for_event(event))["requests"]
    (hostile_request,) = _by_table(rows_for_event(hostile))["requests"]

    assert len(request["client_name"]) == 100
    assert request["namespace"] == "tickets"  # derived from the tool, the bad one dropped
    assert hostile_request["tool"] is None


def test_no_row_has_a_place_for_arguments_or_results() -> None:
    event = _call_event(arguments=ARGUMENTS, result=ARGUMENTS, text=ARGUMENTS)

    rows = rows_for_event(event)

    assert ARGUMENTS not in repr([row.values for row in rows])
    assert {"arguments", "result", "text"}.isdisjoint(key for row in rows for key in row.values)


# --- the buffer --------------------------------------------------------------------------------


def test_a_full_buffer_drops_the_oldest_row_and_counts_it() -> None:
    buffer = TelemetryBuffer(3)

    buffer.put(*(Row("requests", {"n": n}) for n in range(5)))

    assert [row.values["n"] for row in buffer.take(10)] == [2, 3, 4]
    assert buffer.dropped_total == 2


def test_a_flood_of_failed_logins_cannot_push_decision_records_out_of_the_buffer() -> None:
    """Anyone who can reach the gateway can fail authentication as fast as they like."""
    buffer = TelemetryBuffer(10, noisy_capacity=3)
    buffer.put(*(Row("requests", {"n": n}) for n in range(10)))

    buffer.put(*(Row("auth_failures", {"n": n}) for n in range(1000)))

    taken = buffer.take(100)
    assert [row.values["n"] for row in taken if row.table == "requests"] == list(range(10))
    assert [row.values["n"] for row in taken if row.table == "auth_failures"] == [997, 998, 999]
    assert buffer.dropped_total == 997


def test_decision_records_are_taken_before_auth_failures() -> None:
    buffer = TelemetryBuffer(10, noisy_capacity=10)
    buffer.put(Row("auth_failures", {}), Row("requests", {}), Row("spans", {}))

    assert [row.table for row in buffer.take(10)] == ["requests", "spans", "auth_failures"]


def test_failed_logins_are_not_starved_by_a_steady_stream_of_decision_records() -> None:
    buffer = TelemetryBuffer(5000, noisy_capacity=50)
    buffer.put(*(Row("requests", {"n": n}) for n in range(1000)))
    buffer.put(*(Row("auth_failures", {"n": n}) for n in range(5)))

    batch = buffer.take(100)

    assert len(batch) == 100
    assert 0 < sum(row.table == "auth_failures" for row in batch) <= 10


def test_take_returns_the_oldest_rows_first_and_removes_them() -> None:
    buffer = TelemetryBuffer(10)
    buffer.put(*(Row("requests", {"n": n}) for n in range(5)))

    assert [row.values["n"] for row in buffer.take(2)] == [0, 1]
    assert len(buffer) == 3


# --- the sinks ---------------------------------------------------------------------------------


class _RaisingSink:
    def __init__(self) -> None:
        self.calls = 0

    async def emit(self, event: GatewayEvent) -> None:
        self.calls += 1
        raise RuntimeError("sink is broken")


class _RecordingSink:
    def __init__(self) -> None:
        self.events: list[GatewayEvent] = []

    async def emit(self, event: GatewayEvent) -> None:
        self.events.append(event)


@pytest.mark.anyio
async def test_the_postgres_sink_queues_rows_without_waiting() -> None:
    buffer = TelemetryBuffer(100)

    await PostgresEventSink(buffer).emit(_call_event())

    assert len(buffer) == 3


@pytest.mark.anyio
async def test_a_failing_sink_is_logged_once_and_the_next_sink_still_gets_the_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    failing, recording = _RaisingSink(), _RecordingSink()
    fan_out = FanOutEventSink([("broken", failing)], recording)

    with caplog.at_level(logging.ERROR, logger="ai_gateway.telemetry.sinks"):
        for _ in range(5):
            await fan_out.emit(_call_event())

    assert failing.calls == 5
    assert len(recording.events) == 5
    assert caplog.text.count("event sink broken failed") == 1, "logged once, then rate limited"
    assert str(REQUEST_ID) in caplog.text


@pytest.mark.anyio
async def test_a_failing_primary_sink_still_reaches_the_pipeline_which_logs_the_request() -> None:
    """The primary sink is the audit trail in Phase 3: its failure must not be swallowed here."""
    extra = _RecordingSink()
    fan_out = FanOutEventSink([("recording", extra)], _RaisingSink())

    with pytest.raises(RuntimeError, match="sink is broken"):
        await fan_out.emit(_call_event())

    assert len(extra.events) == 1, "the extra sinks still got the event first"


@pytest.mark.anyio
async def test_a_sink_that_cannot_build_its_rows_does_not_raise() -> None:
    sink: EventSink = SafeEventSink(PostgresEventSink(TelemetryBuffer(10)), "postgres")
    broken = _call_event(request_id="not-a-uuid")

    await sink.emit(broken)


# --- spans -------------------------------------------------------------------------------------


@pytest.fixture
def provider_and_buffer() -> tuple[TracerProvider, InMemorySpanExporter, TelemetryBuffer]:
    exporter, buffer = InMemorySpanExporter(), TelemetryBuffer(100)
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    provider.add_span_processor(BufferSpanProcessor(buffer))
    return provider, exporter, buffer


def test_only_the_gateways_own_spans_are_stored_with_allowlisted_attributes(
    provider_and_buffer: tuple[TracerProvider, InMemorySpanExporter, TelemetryBuffer],
) -> None:
    provider, _, buffer = provider_and_buffer
    gateway_tracer, sdk_tracer = (
        provider.get_tracer("ai_gateway"),
        provider.get_tracer("mcp-python-sdk"),
    )

    with (
        sdk_tracer.start_as_current_span("tools/call echo__say"),
        gateway_tracer.start_as_current_span("gateway.tool_call") as span,
    ):
        span.set_attribute("gateway.tool", "echo__say")
        span.set_attribute("gateway.request_id", str(REQUEST_ID))
        span.set_attribute("gateway.arguments", ARGUMENTS)
        span.set_attribute("http.url", "http://upstream/secret")
        with gateway_tracer.start_as_current_span("gateway.layer.scope"):
            pass

    rows = buffer.take(10)
    assert [row.values["name"] for row in rows] == ["gateway.layer.scope", "gateway.tool_call"]
    call = rows[1].values
    assert call["attrs"].obj == {"gateway.tool": "echo__say", "gateway.request_id": str(REQUEST_ID)}
    assert call["request_id"] == REQUEST_ID
    assert ARGUMENTS not in repr(rows)
    assert "secret" not in repr(rows)
    assert rows[0].values["parent_span_id"] == call["span_id"]
    assert rows[0].values["trace_id"] == call["trace_id"]


def test_a_span_of_another_scope_or_with_another_name_has_no_row(
    provider_and_buffer: tuple[TracerProvider, InMemorySpanExporter, TelemetryBuffer],
) -> None:
    provider, exporter, _ = provider_and_buffer
    with provider.get_tracer("other").start_as_current_span("gateway.tool_call"):
        pass
    with provider.get_tracer("ai_gateway").start_as_current_span("Not A Gateway Span"):
        pass

    assert [span_row(span) for span in exporter.get_finished_spans()] == [None, None]


def test_a_span_that_cannot_be_converted_never_raises_into_the_request(
    provider_and_buffer: tuple[TracerProvider, InMemorySpanExporter, TelemetryBuffer],
    caplog: pytest.LogCaptureFixture,
) -> None:
    _, _, buffer = provider_and_buffer

    class Exploding:
        name = "gateway.tool_call"

        def __getattr__(self, name: str) -> Any:
            raise RuntimeError("boom")

    processor = BufferSpanProcessor(buffer)
    with caplog.at_level(logging.ERROR, logger="ai_gateway.telemetry.spans"):
        processor.on_end(Exploding())  # type: ignore[arg-type]

    assert len(buffer) == 0
    assert "could not be queued" in caplog.text
    trace.get_tracer_provider()  # the global provider is untouched by this test
