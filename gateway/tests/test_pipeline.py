"""The request pipeline: configuration, ordering, modes, failing closed, and records."""

import logging
from collections.abc import Sequence
from dataclasses import replace
from typing import Any
from uuid import uuid4

import anyio
import pytest
from mcp.types import CallToolResult, TextContent, Tool

from ai_gateway.pipeline.config import PipelineConfigError, parse_pipeline_config
from ai_gateway.pipeline.layers.scope import ScopeLayer
from ai_gateway.pipeline.runner import (
    Blocked,
    Forwarded,
    Pipeline,
    UpstreamOutcome,
    UpstreamStatus,
)
from ai_gateway.pipeline.types import (
    ALLOW,
    BaseLayer,
    CallContext,
    CatalogTool,
    ClientIdentity,
    Deny,
    DenyCode,
    LayerMode,
    ToolCall,
    Verdict,
)
from ai_gateway.seams.events import GatewayEvent, MemoryEventSink


class TraceLayer(BaseLayer):
    """Records each hook call into a shared list and denies when told to."""

    name = "trace"
    calls: list[str]
    deny_before: bool = False
    deny_after: bool = False
    raise_before: bool = False

    async def before_call(self, ctx: CallContext, call: ToolCall) -> Verdict:
        self.calls.append(f"{self.name}.before")
        if self.raise_before:
            raise RuntimeError("layer bug")
        return Deny(DenyCode.LAYER_ERROR, "no") if self.deny_before else ALLOW

    async def after_call(self, ctx: CallContext, call: ToolCall, result: CallToolResult) -> Verdict:
        self.calls.append(f"{self.name}.after")
        return Deny(DenyCode.LAYER_ERROR, "no") if self.deny_after else ALLOW


class FirstLayer(TraceLayer):
    name = "first"


class SecondLayer(TraceLayer):
    name = "second"


class FloorLayer(BaseLayer):
    name = "floor"
    floor = True


ORDER: Sequence[type[BaseLayer]] = (FirstLayer, SecondLayer)


def _context(scopes: set[str]) -> CallContext:
    return CallContext(
        request_id=uuid4(),
        client=ClientIdentity(id=uuid4(), name="harborline-support-bot", scopes=frozenset(scopes)),
        session_id="session",
        protocol_version="2025-11-25",
    )


def _call(name: str = "echo__say", arguments: dict[str, Any] | None = None) -> ToolCall:
    return ToolCall.create(name, "echo", name.split("__")[1], arguments or {"text": "hi"})


def _catalog_tool(name: str) -> CatalogTool:
    return CatalogTool(
        "echo", name.split("__")[1], Tool(name=name, input_schema={"type": "object"})
    )


def _pipeline(
    modes: dict[str, str], calls: list[str], events: MemoryEventSink
) -> tuple[Pipeline, FirstLayer, SecondLayer]:
    config = parse_pipeline_config({"layers": modes}, ORDER)
    first, second = FirstLayer(), SecondLayer()
    first.calls = second.calls = calls
    return Pipeline([first, second], config, events), first, second


def _layer_decisions(events: MemoryEventSink) -> list[dict[str, Any]]:
    layers = events.events[-1].payload["layers"]
    assert isinstance(layers, list)
    return [decision for decision in layers if isinstance(decision, dict)]


def _forwarder(calls: list[str]) -> Any:
    async def forward(ctx: CallContext, call: ToolCall) -> UpstreamOutcome:
        calls.append("upstream")
        result = CallToolResult(content=[TextContent(type="text", text=call.arguments["text"])])
        return UpstreamOutcome(result, UpstreamStatus.OK)

    return forward


# Configuration


def test_missing_layers_default_to_enforce() -> None:
    config = parse_pipeline_config({}, ORDER)

    assert dict(config.modes) == {"first": LayerMode.ENFORCE, "second": LayerMode.ENFORCE}


def test_order_comes_from_code_not_from_the_file() -> None:
    config = parse_pipeline_config({"layers": {"second": "monitor", "first": "off"}}, ORDER)

    assert list(config.modes) == ["first", "second"]


@pytest.mark.parametrize(
    "raw",
    [
        {"layers": {"unknown": "enforce"}},
        {"layers": {"first": "sometimes"}},
        {"layers": {"first": True}},
        {"extra": {}},
        {"safety": {"allow_floor_override": "yes"}},
        {"safety": {"something_else": True}},
    ],
)
def test_configuration_mistakes_stop_startup(raw: dict[str, Any]) -> None:
    with pytest.raises(PipelineConfigError):
        parse_pipeline_config(raw, ORDER)


def test_floor_layers_need_the_override_flag_to_be_weakened() -> None:
    with pytest.raises(PipelineConfigError, match="allow_floor_override"):
        parse_pipeline_config({"layers": {"floor": "monitor"}}, (FloorLayer,))

    config = parse_pipeline_config(
        {"layers": {"floor": "off"}, "safety": {"allow_floor_override": True}}, (FloorLayer,)
    )
    assert config.modes["floor"] is LayerMode.OFF


def test_scope_is_a_floor_layer() -> None:
    assert ScopeLayer.floor


def test_fingerprint_changes_with_any_mode() -> None:
    enforced = parse_pipeline_config({}, ORDER)
    monitored = parse_pipeline_config({"layers": {"second": "monitor"}}, ORDER)

    assert enforced.sha256 != monitored.sha256
    assert enforced.sha256 == parse_pipeline_config({"layers": {"first": "enforce"}}, ORDER).sha256


# Running


@pytest.mark.anyio
async def test_layers_run_in_order_around_the_upstream_call() -> None:
    calls: list[str] = []
    pipeline, _, _ = _pipeline({}, calls, MemoryEventSink())

    outcome = await pipeline.call_tool(_context({"echo__say"}), _call(), _forwarder(calls))

    assert isinstance(outcome, Forwarded)
    assert calls == ["first.before", "second.before", "upstream", "first.after", "second.after"]


@pytest.mark.anyio
async def test_an_enforcing_deny_stops_the_call_before_upstream() -> None:
    calls: list[str] = []
    events = MemoryEventSink()
    pipeline, first, _ = _pipeline({}, calls, events)
    first.deny_before = True

    outcome = await pipeline.call_tool(_context(set()), _call(), _forwarder(calls))

    assert isinstance(outcome, Blocked)
    assert calls == ["first.before"]
    payload = events.events[-1].payload
    assert payload["outcome"] == "blocked"
    assert payload["blocked_by"] == "first"


@pytest.mark.anyio
async def test_a_monitoring_deny_is_recorded_and_the_call_goes_on() -> None:
    calls: list[str] = []
    events = MemoryEventSink()
    pipeline, first, _ = _pipeline({"first": "monitor"}, calls, events)
    first.deny_before = True

    outcome = await pipeline.call_tool(_context(set()), _call(), _forwarder(calls))

    assert isinstance(outcome, Forwarded)
    assert "upstream" in calls
    decision = _layer_decisions(events)[0]
    assert decision["layer"] == "first"
    assert decision["mode"] == "monitor"
    assert decision["verdict"] == "would_block"


@pytest.mark.anyio
async def test_a_layer_that_is_off_never_runs() -> None:
    calls: list[str] = []
    events = MemoryEventSink()
    pipeline, first, _ = _pipeline({"first": "off"}, calls, events)
    first.deny_before = True

    outcome = await pipeline.call_tool(_context(set()), _call(), _forwarder(calls))

    assert isinstance(outcome, Forwarded)
    assert "first.before" not in calls
    assert events.events[-1].payload["enabled_layers"] == ["second"]


@pytest.mark.anyio
async def test_a_failing_layer_blocks_in_enforce_mode() -> None:
    calls: list[str] = []
    pipeline, _, second = _pipeline({}, calls, MemoryEventSink())
    second.raise_before = True

    outcome = await pipeline.call_tool(_context(set()), _call(), _forwarder(calls))

    assert isinstance(outcome, Blocked)
    assert outcome.deny.code is DenyCode.LAYER_ERROR
    assert "layer bug" not in outcome.deny.public_message
    assert "upstream" not in calls


@pytest.mark.anyio
async def test_a_failing_layer_only_records_in_monitor_mode() -> None:
    calls: list[str] = []
    events = MemoryEventSink()
    pipeline, _, second = _pipeline({"second": "monitor"}, calls, events)
    second.raise_before = True

    outcome = await pipeline.call_tool(_context(set()), _call(), _forwarder(calls))

    assert isinstance(outcome, Forwarded)
    assert _layer_decisions(events)[1]["verdict"] == "error"


@pytest.mark.anyio
async def test_an_after_call_deny_withholds_the_result() -> None:
    calls: list[str] = []
    pipeline, _, second = _pipeline({}, calls, MemoryEventSink())
    second.deny_after = True

    outcome = await pipeline.call_tool(_context(set()), _call(), _forwarder(calls))

    assert isinstance(outcome, Blocked)
    assert "upstream" in calls


@pytest.mark.anyio
async def test_records_carry_a_hash_of_the_arguments_never_the_arguments() -> None:
    events = MemoryEventSink()
    pipeline, _, _ = _pipeline({}, [], events)
    call = _call(arguments={"text": "ship order 4471 to Pier 9"})

    await pipeline.call_tool(_context(set()), call, _forwarder([]))

    event_json = events.events[-1].model_dump_json()
    assert "Pier 9" not in event_json
    assert call.arguments_sha256 in event_json


def test_tool_call_arguments_cannot_be_changed_in_place() -> None:
    call = _call(arguments={"text": "original"})

    call.arguments["text"] = "tampered"

    assert call.arguments == {"text": "original"}


# The scope layer


@pytest.mark.anyio
async def test_scope_layer_lists_and_allows_only_granted_tools() -> None:
    config = parse_pipeline_config({}, (ScopeLayer,))
    events = MemoryEventSink()
    pipeline = Pipeline.build(config, events, (ScopeLayer,))
    ctx = _context({"echo__say"})
    catalog = [_catalog_tool("echo__say"), _catalog_tool("echo__shout")]

    listed = await pipeline.list_tools(ctx, catalog)
    allowed = await pipeline.call_tool(ctx, _call("echo__say"), _forwarder([]))
    refused = await pipeline.call_tool(ctx, _call("echo__shout"), _forwarder([]))

    assert [tool.exposed_name for tool in listed] == ["echo__say"]
    assert isinstance(allowed, Forwarded)
    assert isinstance(refused, Blocked)
    assert refused.deny.public_message == "Tool 'echo__shout' is not available to this client."


@pytest.mark.anyio
async def test_scope_layer_in_monitor_mode_lists_everything_but_records_it() -> None:
    config = parse_pipeline_config(
        {"layers": {"scope": "monitor"}, "safety": {"allow_floor_override": True}}, (ScopeLayer,)
    )
    events = MemoryEventSink()
    pipeline = Pipeline.build(config, events, (ScopeLayer,))

    listed = await pipeline.list_tools(
        _context(set()), [_catalog_tool("echo__say"), _catalog_tool("echo__shout")]
    )

    assert len(listed) == 2
    decision = _layer_decisions(events)[0]
    assert decision["verdict"] == "would_block"
    assert decision["tools_removed"] == 2


@pytest.mark.anyio
async def test_unknown_tools_get_the_same_answer_as_out_of_scope_tools() -> None:
    pipeline = Pipeline.build(
        parse_pipeline_config({}, (ScopeLayer,)), MemoryEventSink(), (ScopeLayer,)
    )
    ctx = _context(set())

    unknown = await pipeline.reject_unknown_tool(ctx, "echo__nope")
    out_of_scope = await pipeline.call_tool(ctx, _call("echo__nope"), _forwarder([]))

    assert isinstance(out_of_scope, Blocked)
    assert unknown == out_of_scope.deny


@pytest.mark.anyio
async def test_absurdly_long_unknown_names_are_still_refused_and_recorded() -> None:
    events = MemoryEventSink()
    pipeline = Pipeline.build(parse_pipeline_config({}, (ScopeLayer,)), events, (ScopeLayer,))

    deny = await pipeline.reject_unknown_tool(_context(set()), "x" * 5000)

    assert deny.code is DenyCode.TOOL_UNAVAILABLE
    assert len(deny.public_message) < 120
    assert events.events[-1].subject_id == "x" * 61 + "..."


@pytest.mark.anyio
async def test_a_failing_filter_hides_every_tool_in_enforce_mode() -> None:
    class BrokenFilter(BaseLayer):
        name = "broken"

        async def filter_tools(
            self, ctx: CallContext, tools: Sequence[CatalogTool]
        ) -> Sequence[CatalogTool]:
            raise RuntimeError("filter bug")

    events = MemoryEventSink()
    pipeline = Pipeline.build(parse_pipeline_config({}, (BrokenFilter,)), events, (BrokenFilter,))

    listed = await pipeline.list_tools(_context({"echo__say"}), [_catalog_tool("echo__say")])

    assert listed == []
    assert _layer_decisions(events)[0]["verdict"] == "error"


# A sink that fails or stalls must never change what the client gets


class RaisingSink:
    async def emit(self, event: GatewayEvent) -> None:
        raise RuntimeError("sink is broken")


class HangingSink:
    async def emit(self, event: GatewayEvent) -> None:
        await anyio.sleep_forever()


def _pipeline_with_sink(sink: Any, calls: list[str], emit_timeout_s: float = 2.0) -> Pipeline:
    config = parse_pipeline_config({}, ORDER)
    first, second = FirstLayer(), SecondLayer()
    first.calls = second.calls = calls
    return Pipeline([first, second], config, sink, emit_timeout_s=emit_timeout_s)


@pytest.mark.anyio
@pytest.mark.parametrize("sink", [RaisingSink(), HangingSink()], ids=["raising", "hanging"])
async def test_a_failing_sink_leaves_a_forwarded_result_unchanged(sink: Any) -> None:
    calls: list[str] = []
    pipeline = _pipeline_with_sink(sink, calls, emit_timeout_s=0.05)

    with anyio.fail_after(2):
        outcome = await pipeline.call_tool(_context({"echo__say"}), _call(), _forwarder(calls))

    assert isinstance(outcome, Forwarded)
    assert outcome.result.content == [TextContent(type="text", text="hi")]
    assert calls == ["first.before", "second.before", "upstream", "first.after", "second.after"]


@pytest.mark.anyio
@pytest.mark.parametrize("sink", [RaisingSink(), HangingSink()], ids=["raising", "hanging"])
async def test_a_failing_sink_leaves_a_block_unchanged(sink: Any) -> None:
    calls: list[str] = []
    pipeline = _pipeline_with_sink(sink, calls, emit_timeout_s=0.05)
    pipeline._layers[0][0].deny_before = True  # type: ignore[attr-defined]

    with anyio.fail_after(2):
        outcome = await pipeline.call_tool(_context(set()), _call(), _forwarder(calls))

    assert isinstance(outcome, Blocked)
    assert calls == ["first.before"]


@pytest.mark.anyio
@pytest.mark.parametrize("sink", [RaisingSink(), HangingSink()], ids=["raising", "hanging"])
async def test_a_failing_sink_leaves_listing_and_unknown_tool_answers_unchanged(
    sink: Any,
) -> None:
    pipeline = _pipeline_with_sink(sink, [], emit_timeout_s=0.05)
    ctx = _context({"echo__say"})

    with anyio.fail_after(2):
        listed = await pipeline.list_tools(ctx, [_catalog_tool("echo__say")])
        deny = await pipeline.reject_unknown_tool(ctx, "echo__nope")

    assert [tool.exposed_name for tool in listed] == ["echo__say"]
    assert deny.code is DenyCode.TOOL_UNAVAILABLE


@pytest.mark.anyio
async def test_a_record_that_cannot_be_built_does_not_fail_the_call(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An oversized payload fails GatewayEvent's own validation, before any sink runs."""
    calls: list[str] = []
    events = MemoryEventSink()
    pipeline = _pipeline_with_sink(events, calls)
    ctx = replace(_context({"echo__say"}), protocol_version="v" * 20_000)

    with caplog.at_level(logging.ERROR, logger="ai_gateway.pipeline.runner"):
        outcome = await pipeline.call_tool(ctx, _call(), _forwarder(calls))
        listed = await pipeline.list_tools(ctx, [_catalog_tool("echo__say")])

    assert isinstance(outcome, Forwarded)
    assert [tool.exposed_name for tool in listed] == ["echo__say"]
    assert events.events == []
    assert "v" * 100 not in caplog.text


@pytest.mark.anyio
async def test_a_failing_sink_is_logged_without_the_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls: list[str] = []
    pipeline = _pipeline_with_sink(RaisingSink(), calls)

    with caplog.at_level(logging.ERROR, logger="ai_gateway.pipeline.runner"):
        await pipeline.call_tool(_context({"echo__say"}), _call(), _forwarder(calls))

    assert "gateway.tool_call" in caplog.text
    assert "arguments_sha256" not in caplog.text
