"""The pipeline and the audit trail: writes are recorded before they are forwarded."""

import logging
from typing import Any

import pytest
from mcp.types import CallToolResult, TextContent

from ai_gateway.pipeline.config import PipelineConfigError, parse_pipeline_config
from ai_gateway.pipeline.runner import Blocked, Forwarded, Pipeline, UpstreamOutcome, UpstreamStatus
from ai_gateway.pipeline.types import CallContext, DenyCode, ToolCall
from ai_gateway.policy.audit import AuditStatus, AuditUnavailableError
from ai_gateway.policy.audit import call_event as audit_call_event
from ai_gateway.seams.events import GatewayEvent, MemoryEventSink
from ai_gateway.text import sha256_of_name
from tests.test_pipeline import ORDER, FirstLayer, SecondLayer, _context

POLICY_BLOCK = "Request blocked by gateway policy."


class FakeAudit:
    """Records what the pipeline asks of it, in order, and can refuse a write."""

    def __init__(self, order: list[str], *, refuse: bool = False) -> None:
        self.order = order
        self.refuse = refuse
        self.records: list[GatewayEvent] = []

    async def before_write(self, ctx: CallContext, call: ToolCall) -> None:
        self.order.append("audit")
        if self.refuse:
            raise AuditUnavailableError("the log is down")

    def record(self, event: GatewayEvent) -> None:
        self.records.append(event)

    def status(self) -> AuditStatus:
        return AuditStatus(status="ok")


def _pipeline(
    order: list[str], audit: FakeAudit, *, unaudited: bool = False
) -> tuple[Pipeline, MemoryEventSink]:
    config = parse_pipeline_config({"safety": {"allow_unaudited_writes": unaudited}}, ORDER)
    events = MemoryEventSink()
    first, second = FirstLayer(), SecondLayer()
    first.calls = second.calls = order
    return Pipeline([first, second], config, events, audit=audit), events


def _call(effect: str = "write") -> ToolCall:
    return ToolCall.create(
        "echo__say",
        "echo",
        "say",
        {"text": "the customer's number 4111"},
        effect,  # type: ignore[arg-type]
    )


def _forwarder(order: list[str]) -> Any:
    async def forward(ctx: CallContext, call: ToolCall) -> UpstreamOutcome:
        order.append("upstream")
        result = CallToolResult(content=[TextContent(type="text", text="done")])
        return UpstreamOutcome(result, UpstreamStatus.OK)

    return forward


@pytest.mark.anyio
async def test_a_write_is_recorded_after_every_layer_has_passed_it_and_before_it_is_forwarded() -> (
    None
):
    order: list[str] = []
    pipeline, _ = _pipeline(order, FakeAudit(order))

    outcome = await pipeline.call_tool(_context({"echo__say"}), _call(), _forwarder(order))

    assert isinstance(outcome, Forwarded)
    assert order == [
        "first.before",
        "second.before",
        "audit",
        "upstream",
        "first.after",
        "second.after",
    ]


@pytest.mark.anyio
async def test_a_write_whose_record_cannot_be_stored_is_not_forwarded(
    caplog: pytest.LogCaptureFixture,
) -> None:
    order: list[str] = []
    audit = FakeAudit(order, refuse=True)
    pipeline, events = _pipeline(order, audit)

    with caplog.at_level(logging.ERROR, logger="ai_gateway.pipeline.runner"):
        outcome = await pipeline.call_tool(_context({"echo__say"}), _call(), _forwarder(order))

    assert isinstance(outcome, Blocked)
    assert outcome.deny.code is DenyCode.AUDIT_UNAVAILABLE
    assert outcome.deny.public_message == POLICY_BLOCK, "the client learns nothing about the cause"
    assert "upstream" not in order
    payload = events.events[-1].payload
    assert (payload["outcome"], payload["blocked_by"], payload["deny_code"]) == (
        "blocked",
        "audit",
        "audit_unavailable",
    )
    assert "the log is down" in caplog.text
    assert len(audit.records) == 1, "the refused write is still recorded, best effort"


@pytest.mark.anyio
async def test_a_write_goes_ahead_without_a_record_only_when_the_configuration_says_so(
    caplog: pytest.LogCaptureFixture,
) -> None:
    order: list[str] = []
    pipeline, _ = _pipeline(order, FakeAudit(order, refuse=True), unaudited=True)

    with caplog.at_level(logging.WARNING, logger="ai_gateway.pipeline.runner"):
        outcome = await pipeline.call_tool(_context({"echo__say"}), _call(), _forwarder(order))

    assert isinstance(outcome, Forwarded)
    assert "goes unaudited" in caplog.text


@pytest.mark.anyio
async def test_a_read_is_never_held_up_by_the_audit_log_and_is_recorded_afterwards() -> None:
    order: list[str] = []
    audit = FakeAudit(order, refuse=True)  # a log that is down
    pipeline, _ = _pipeline(order, audit)

    outcome = await pipeline.call_tool(_context({"echo__say"}), _call("read"), _forwarder(order))

    assert isinstance(outcome, Forwarded)
    assert "audit" not in order
    assert [e.payload["outcome"] for e in audit.records] == ["forwarded"]


@pytest.mark.anyio
async def test_every_outcome_is_recorded_for_the_audit_log() -> None:
    order: list[str] = []
    audit = FakeAudit(order)
    pipeline, _ = _pipeline(order, audit)
    ctx = _context({"echo__say"})

    await pipeline.call_tool(ctx, _call("read"), _forwarder(order))
    first = pipeline._layers[0][0]
    first.deny_before = True  # type: ignore[attr-defined]
    await pipeline.call_tool(ctx, _call("read"), _forwarder(order))
    await pipeline.reject_unknown_tool(ctx, "echo__nope")

    outcomes = [(e.payload["outcome"], e.payload.get("blocked_by")) for e in audit.records]
    assert outcomes == [("forwarded", None), ("blocked", "first"), ("blocked", "catalog")]
    assert all(e.action == "gateway.tool_call" for e in audit.records)


@pytest.mark.anyio
async def test_an_unknown_tool_name_is_hashed_whole_and_recorded_cleaned_and_cut() -> None:
    order: list[str] = []
    audit = FakeAudit(order)
    pipeline, _ = _pipeline(order, audit)
    name = "echo__\x1b[2J" + "x" * 300

    await pipeline.reject_unknown_tool(_context({"echo__say"}), name)

    [record] = audit.records
    assert record.subject_id is not None
    assert "\x1b" not in record.subject_id
    assert len(record.subject_id) == 64
    assert record.payload["tool_name_sha256"] == sha256_of_name(name), "all of it, as sent"
    assert audit_call_event(record).subject_id is None, "the client's text is never a subject"


@pytest.mark.anyio
async def test_a_listing_is_not_audited() -> None:
    order: list[str] = []
    audit = FakeAudit(order)
    pipeline, _ = _pipeline(order, audit)

    await pipeline.list_tools(_context({"echo__say"}), [])

    assert audit.records == []


def test_the_unaudited_writes_flag_is_off_by_default_and_changes_the_fingerprint() -> None:
    default = parse_pipeline_config({}, ORDER)
    allowed = parse_pipeline_config({"safety": {"allow_unaudited_writes": True}}, ORDER)

    assert default.allow_unaudited_writes is False
    assert allowed.allow_unaudited_writes is True
    assert default.sha256 != allowed.sha256


def test_the_unaudited_writes_flag_must_be_a_boolean() -> None:
    with pytest.raises(PipelineConfigError, match="allow_unaudited_writes"):
        parse_pipeline_config({"safety": {"allow_unaudited_writes": "yes"}}, ORDER)
