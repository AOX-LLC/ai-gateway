"""The approval layer and the pending disposition, with a gate that answers as told."""

from typing import Any

import pytest
from mcp.types import CallToolResult, TextContent

from ai_gateway.pipeline.config import PipelineConfigError, parse_pipeline_config
from ai_gateway.pipeline.layers.approval import ApprovalLayer
from ai_gateway.pipeline.layers.scope import ScopeLayer
from ai_gateway.pipeline.runner import Blocked, Forwarded, Pipeline, UpstreamOutcome, UpstreamStatus
from ai_gateway.pipeline.types import CallContext, DenyCode, Disposition, ToolCall
from ai_gateway.seams.approvals import ApprovalDecision, ApprovalOutcome
from ai_gateway.seams.events import MemoryEventSink
from tests.test_pipeline import _context

ORDER = (ScopeLayer, ApprovalLayer)
APPROVAL_ID = "5b3a7f0e-0000-4000-8000-000000000001"


class FakeGate:
    def __init__(self, outcome: ApprovalOutcome) -> None:
        self.outcome = outcome
        self.asked: list[ToolCall] = []

    async def decide(self, ctx: CallContext, call: ToolCall) -> ApprovalDecision:
        self.asked.append(call)
        return ApprovalDecision(self.outcome, APPROVAL_ID)


def _pipeline(
    gate: FakeGate | None, mode: str = "enforce"
) -> tuple[Pipeline, MemoryEventSink, list[str]]:
    config = parse_pipeline_config(
        {"layers": {"approval": mode}, "safety": {"allow_floor_override": mode != "enforce"}}, ORDER
    )
    events = MemoryEventSink()
    return Pipeline.build(config, events, ORDER, approvals=gate), events, []


def _write() -> ToolCall:
    return ToolCall.create(
        "tickets__create_ticket", "tickets", "create_ticket", {"subject": "x"}, "write"
    )


def _read() -> ToolCall:
    return ToolCall.create("tickets__get_ticket", "tickets", "get_ticket", {"id": 1}, "read")


def _forwarder(forwarded: list[str]) -> Any:
    async def forward(ctx: CallContext, call: ToolCall) -> UpstreamOutcome:
        forwarded.append(call.exposed_name)
        result = CallToolResult(content=[TextContent(type="text", text="done")])
        return UpstreamOutcome(result, UpstreamStatus.OK)

    return forward


CTX = _context({"tickets__create_ticket", "tickets__get_ticket"})


@pytest.mark.anyio
async def test_a_read_is_never_sent_for_approval() -> None:
    gate = FakeGate(ApprovalOutcome.REJECTED)
    pipeline, _, forwarded = _pipeline(gate)

    outcome = await pipeline.call_tool(CTX, _read(), _forwarder(forwarded))

    assert isinstance(outcome, Forwarded)
    assert gate.asked == []


@pytest.mark.anyio
async def test_an_approved_write_is_forwarded_and_names_its_approval_in_the_record() -> None:
    pipeline, events, forwarded = _pipeline(FakeGate(ApprovalOutcome.APPROVED))

    outcome = await pipeline.call_tool(CTX, _write(), _forwarder(forwarded))

    assert isinstance(outcome, Forwarded)
    assert forwarded == ["tickets__create_ticket"]
    payload = events.events[-1].payload
    assert payload["outcome"] == "forwarded"
    assert payload["approval_id"] == APPROVAL_ID


@pytest.mark.anyio
async def test_a_write_still_waiting_is_pending_not_forwarded_and_recorded_as_a_block() -> None:
    pipeline, events, forwarded = _pipeline(FakeGate(ApprovalOutcome.PENDING))

    outcome = await pipeline.call_tool(CTX, _write(), _forwarder(forwarded))

    assert isinstance(outcome, Blocked)
    assert outcome.deny.disposition is Disposition.PENDING
    assert outcome.deny.code is DenyCode.APPROVAL_PENDING
    assert outcome.deny.approval_id == APPROVAL_ID
    assert forwarded == []
    payload = events.events[-1].payload
    assert (payload["outcome"], payload["blocked_by"], payload["deny_code"]) == (
        "blocked",
        "approval",
        "approval_pending",
    )
    assert payload["approval_id"] == APPROVAL_ID


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("outcome", "code"),
    [
        (ApprovalOutcome.REJECTED, DenyCode.APPROVAL_REJECTED),
        (ApprovalOutcome.EXPIRED, DenyCode.APPROVAL_EXPIRED),
        (ApprovalOutcome.UNAVAILABLE, DenyCode.APPROVAL_UNAVAILABLE),
    ],
)
async def test_a_rejected_expired_or_unobtainable_approval_blocks_the_write(
    outcome: ApprovalOutcome, code: DenyCode
) -> None:
    pipeline, _, forwarded = _pipeline(FakeGate(outcome))

    result = await pipeline.call_tool(CTX, _write(), _forwarder(forwarded))

    assert isinstance(result, Blocked)
    assert result.deny.code is code
    assert result.deny.disposition is Disposition.BLOCK
    assert forwarded == []


@pytest.mark.anyio
async def test_with_no_gate_a_write_is_refused_and_a_read_still_runs() -> None:
    pipeline, _, forwarded = _pipeline(None)

    refused = await pipeline.call_tool(CTX, _write(), _forwarder(forwarded))
    ran = await pipeline.call_tool(CTX, _read(), _forwarder(forwarded))

    assert isinstance(refused, Blocked)
    assert refused.deny.code is DenyCode.APPROVAL_UNAVAILABLE
    assert isinstance(ran, Forwarded)
    assert forwarded == ["tickets__get_ticket"]


@pytest.mark.anyio
async def test_in_monitor_mode_nobody_is_asked_and_the_write_goes_on_as_a_would_block() -> None:
    gate = FakeGate(ApprovalOutcome.APPROVED)
    pipeline, events, forwarded = _pipeline(gate, mode="monitor")

    outcome = await pipeline.call_tool(CTX, _write(), _forwarder(forwarded))

    assert isinstance(outcome, Forwarded)
    assert gate.asked == [], "asking is a side effect"
    layers: Any = events.events[-1].payload["layers"]
    verdicts = {d["layer"]: d["verdict"] for d in layers if d["hook"] == "before_call"}
    assert verdicts["approval"] == "would_block"


def test_the_approval_layer_is_a_floor_it_takes_the_override_flag_to_weaken() -> None:
    with pytest.raises(PipelineConfigError, match="floor layers"):
        parse_pipeline_config({"layers": {"approval": "off"}}, ORDER)

    parse_pipeline_config(
        {
            "layers": {"approval": "off", "scope": "enforce"},
            "safety": {"allow_floor_override": True},
        },
        ORDER,
    )


def test_the_approval_layer_comes_last() -> None:
    from ai_gateway.pipeline.registry import LAYER_ORDER

    assert [layer.name for layer in LAYER_ORDER][-1] == "approval"


def test_an_upstream_identity_changes_with_what_it_points_at_and_not_otherwise() -> None:
    from uuid import uuid4

    from ai_gateway.registry.models import UpstreamServer

    upstream_id = uuid4()

    def make(**changes: object) -> UpstreamServer:
        values: dict[str, Any] = {
            "id": upstream_id,
            "namespace": "tickets",
            "url": "http://ticketing:4412/mcp",
            "connect_timeout_s": 5.0,
            "call_timeout_s": 10.0,
            "credential_env": "TICKETING_SERVICE_TOKEN",
            **changes,
        }
        return UpstreamServer(**values)

    base = make().identity
    assert make(call_timeout_s=99.0).identity == base, "a timeout is not what an approval is about"
    assert make(url="http://elsewhere:4412/mcp").identity != base
    assert make(credential_env="OTHER_TOKEN").identity != base
    assert make(credential_env=None).identity != base
    assert make(namespace="crm").identity != base
    assert make(id=uuid4()).identity != base
