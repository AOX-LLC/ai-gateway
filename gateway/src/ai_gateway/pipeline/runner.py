"""Run every request through the configured layers, in canonical order, and record what
each layer decided.

One DecisionRecord is emitted per request. The red-team scorecard is computed from these
records, not from the errors clients see, so every layer's verdict is kept, including
the would-block verdicts of layers in monitor mode.
"""

import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from functools import partial
from typing import Literal

import anyio
from mcp.types import CallToolResult
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.trace import Span
from pydantic import JsonValue

from ai_gateway.classifier.judge import Judge
from ai_gateway.pipeline.alerts import Alerts
from ai_gateway.pipeline.config import PipelineConfig
from ai_gateway.pipeline.layers.allowlist import AllowlistLayer, AllowlistRule
from ai_gateway.pipeline.layers.approval import ApprovalLayer
from ai_gateway.pipeline.layers.canary import CanaryConfig, CanaryLayer
from ai_gateway.pipeline.layers.classifier import ClassifierLayer
from ai_gateway.pipeline.layers.egress import EgressConfig, EgressLayer
from ai_gateway.pipeline.layers.pinned import PinnedDescriptionsLayer
from ai_gateway.pipeline.layers.rate_limit import RateLimitLayer, RateLimits
from ai_gateway.pipeline.layers.schema import SchemaLayer
from ai_gateway.pipeline.pins import ToolPins
from ai_gateway.pipeline.registry import LAYER_ORDER
from ai_gateway.pipeline.types import (
    POLICY_BLOCK_MESSAGE,
    Allow,
    BaseLayer,
    CallContext,
    CatalogTool,
    Deny,
    DenyCode,
    LayerMode,
    ToolCall,
    Verdict,
    displayable_tool_name,
    tool_unavailable_message,
)
from ai_gateway.policy.audit import AuditRecorder, AuditUnavailableError
from ai_gateway.seams.approvals import ApprovalGate
from ai_gateway.seams.events import DEFAULT_EMIT_TIMEOUT_S, EventSink, GatewayEvent
from ai_gateway.telemetry import attributes
from ai_gateway.text import sha256_of_name

logger = logging.getLogger(__name__)
_tracer = trace.get_tracer("ai_gateway")
# Every request starts a trace of its own. The MCP SDK parents its server span under a
# `traceparent` the client sent in `_meta`, and the call to the upstream carries the trace
# context that is current when it is made; starting the gateway's spans in an empty context,
# not under the SDK's, means a client cannot choose which trace an upstream call joins.


UNRECORDED_CODE = "classifier_unrecorded"
"""The code on an `unclassified` verdict: replay mode had no recording for a unit of text."""


class UpstreamStatus(StrEnum):
    OK = "ok"
    TOOL_ERROR = "tool_error"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"
    REJECTED = "rejected"
    INVALID_RESPONSE = "invalid_response"


@dataclass(frozen=True)
class UpstreamOutcome:
    result: CallToolResult
    status: UpstreamStatus


Forwarder = Callable[[CallContext, ToolCall], Awaitable[UpstreamOutcome]]


@dataclass(frozen=True)
class Forwarded:
    result: CallToolResult


@dataclass(frozen=True)
class Blocked:
    deny: Deny


CallOutcome = Forwarded | Blocked

LayerVerdict = Literal["allow", "deny", "would_block", "off", "error", "unclassified"]
Hook = Literal["filter", "before_call", "after_call"]
"""Which step of the pipeline a verdict came from."""


@dataclass(frozen=True)
class LayerDecision:
    layer: str
    hook: Hook
    mode: LayerMode
    verdict: LayerVerdict
    code: str | None = None
    tools_removed: int | None = None
    duration_ms: float = 0.0
    score: int | None = None

    def to_payload(self) -> dict[str, JsonValue]:
        payload: dict[str, JsonValue] = {
            "layer": self.layer,
            "hook": self.hook,
            "mode": self.mode.value,
            "verdict": self.verdict,
            "duration_ms": round(self.duration_ms, 3),
        }
        if self.code is not None:
            payload["code"] = self.code
        if self.tools_removed is not None:
            payload["tools_removed"] = self.tools_removed
        if self.score is not None:
            payload["score"] = self.score
        return payload


def _describe_request(span: Span, ctx: CallContext, details: dict[str, JsonValue]) -> None:
    """Name the request on its span and, when a tracer is recording, put the trace id in the
    decision record so the two can be joined."""
    span.set_attribute(attributes.GATEWAY_REQUEST_ID, str(ctx.request_id))
    span.set_attribute(attributes.GATEWAY_CLIENT, ctx.client.name)
    trace_id = span.get_span_context().trace_id
    if trace_id:
        details["trace_id"] = format(trace_id, "032x")


class Pipeline:
    def __init__(
        self,
        layers: Sequence[BaseLayer],
        config: PipelineConfig,
        events: EventSink,
        emit_timeout_s: float = DEFAULT_EMIT_TIMEOUT_S,
        audit: AuditRecorder | None = None,
    ) -> None:
        self._layers = [(layer, config.modes[layer.name]) for layer in layers]
        for layer, mode in self._layers:
            if isinstance(layer, ApprovalLayer):
                layer.observe_only = mode is LayerMode.MONITOR
        self._config = config
        self._events = events
        self._emit_timeout_s = emit_timeout_s
        self._audit = audit
        """The audit trail, or None when this pipeline has none (a unit test's, say)."""

    def describe(self) -> list[dict[str, str]]:
        """The layers and their modes, in pipeline order, for the telemetry store."""
        return [{"name": layer.name, "mode": mode.value} for layer, mode in self._layers]

    @classmethod
    def build(
        cls,
        config: PipelineConfig,
        events: EventSink,
        layer_order: Sequence[type[BaseLayer]] = LAYER_ORDER,
        audit: AuditRecorder | None = None,
        approvals: ApprovalGate | None = None,
        allowlist: Sequence[AllowlistRule] = (),
        rate_limits: RateLimits | None = None,
        pins: ToolPins | None = None,
        egress: EgressConfig | None = None,
        canaries: CanaryConfig | None = None,
        judge: Judge | None = None,
    ) -> "Pipeline":
        alerts = Alerts(audit)

        def make(layer_class: type[BaseLayer]) -> BaseLayer:
            if issubclass(layer_class, ApprovalLayer):
                return layer_class(approvals)
            if issubclass(layer_class, AllowlistLayer):
                return layer_class(allowlist)
            if issubclass(layer_class, RateLimitLayer):
                return layer_class(rate_limits)
            if issubclass(layer_class, SchemaLayer):
                return layer_class(pins, config.validate_results)
            if issubclass(layer_class, PinnedDescriptionsLayer):
                return layer_class(pins, alerts)
            if issubclass(layer_class, EgressLayer):
                return layer_class(egress)
            if issubclass(layer_class, CanaryLayer):
                return layer_class(canaries, alerts)
            if issubclass(layer_class, ClassifierLayer):
                return layer_class(judge)
            return layer_class()

        layers = [make(layer_class) for layer_class in layer_order]
        return cls(layers, config, events, audit=audit)

    async def list_tools(self, ctx: CallContext, tools: Sequence[CatalogTool]) -> list[CatalogTool]:
        started = time.perf_counter()
        visible = list(tools)
        decisions = []
        details: dict[str, JsonValue] = {}
        with _tracer.start_as_current_span(attributes.SPAN_TOOLS_LIST, context=Context()) as span:
            _describe_request(span, ctx, details)
            for layer, mode in self._layers:
                decision, visible = await self._filter_with(layer, mode, ctx, visible)
                decisions.append(decision)

        await self._emit(
            "gateway.tools_list",
            ctx,
            subject_id=None,
            decisions=decisions,
            started=started,
            details={**details, "tools_available": len(tools), "tools_returned": len(visible)},
        )
        return visible

    async def call_tool(self, ctx: CallContext, call: ToolCall, forward: Forwarder) -> CallOutcome:
        started = time.perf_counter()
        decisions: list[LayerDecision] = []
        details: dict[str, JsonValue] = {
            "arguments_sha256": call.arguments_sha256,
            "namespace": call.namespace,
            "effect": call.effect,
            "effect_source": call.effect_source,
        }

        with _tracer.start_as_current_span(attributes.SPAN_TOOL_CALL, context=Context()) as span:
            span.set_attribute(attributes.GATEWAY_TOOL, call.exposed_name)
            _describe_request(span, ctx, details)

            for layer, mode in self._layers:
                block = await self._check_with(
                    layer,
                    mode,
                    decisions,
                    "before_call",
                    partial(layer.before_call, ctx, call),
                    details,
                )
                if block is not None:
                    return await self._finish_blocked(ctx, call, block, decisions, started, details)

            if call.effect == "write":
                block = await self._write_ahead(ctx, call)
                if block is not None:
                    return await self._finish_blocked(ctx, call, block, decisions, started, details)

            upstream_started = time.perf_counter()
            with _tracer.start_as_current_span(attributes.SPAN_UPSTREAM_CALL):
                upstream = await forward(ctx, call)
            details["upstream_duration_ms"] = round(
                (time.perf_counter() - upstream_started) * 1000, 3
            )
            details["upstream_status"] = upstream.status.value

            for layer, mode in self._layers:
                block = await self._check_with(
                    layer,
                    mode,
                    decisions,
                    "after_call",
                    partial(layer.after_call, ctx, call, upstream.result),
                )
                if block is not None:
                    return await self._finish_blocked(ctx, call, block, decisions, started, details)

        details["outcome"] = "forwarded"
        await self._emit("gateway.tool_call", ctx, call.exposed_name, decisions, started, details)
        return Forwarded(upstream.result)

    async def _write_ahead(self, ctx: CallContext, call: ToolCall) -> tuple[str, Deny] | None:
        """Record that a write is about to be forwarded, and wait for the record to be stored.

        A write whose attempt cannot be recorded is refused (as `audit`, with `audit_unavailable`),
        unless the configuration allows unaudited writes. Reads are never held up by the audit log;
        their records are queued after the fact."""
        if self._audit is None:
            return None
        try:
            await self._audit.before_write(ctx, call)
        except AuditUnavailableError as error:
            if self._config.allow_unaudited_writes:
                logger.warning("write %s goes unaudited: %s", ctx.request_id, error)
                return None
            logger.error(
                "write %s refused: the audit log is unavailable (%s)", ctx.request_id, error
            )
            return "audit", Deny(DenyCode.AUDIT_UNAVAILABLE, POLICY_BLOCK_MESSAGE)
        return None

    async def reject_unknown_tool(self, ctx: CallContext, exposed_name: str) -> Deny:
        """Record a call to a tool no upstream offers. The client gets the same answer as
        for a tool outside its scope."""
        deny = Deny(DenyCode.TOOL_UNAVAILABLE, tool_unavailable_message(exposed_name))
        await self._emit(
            "gateway.tool_call",
            ctx,
            displayable_tool_name(exposed_name),
            decisions=[],
            started=time.perf_counter(),
            details={
                "outcome": "blocked",
                "blocked_by": "catalog",
                "deny_code": deny.code.value,
                # The name is the client's choice: its hash is exact, the subject above is cleaned.
                "tool_name_sha256": sha256_of_name(exposed_name),
            },
        )
        return deny

    async def _filter_with(
        self, layer: BaseLayer, mode: LayerMode, ctx: CallContext, visible: list[CatalogTool]
    ) -> tuple[LayerDecision, list[CatalogTool]]:
        hook: Hook = "filter"
        if mode is LayerMode.OFF:
            return LayerDecision(layer.name, hook, mode, "off"), visible

        started = time.perf_counter()
        try:
            kept_names = {tool.exposed_name for tool in await layer.filter_tools(ctx, visible)}
        except Exception:
            # Fail closed: in enforce mode a layer that cannot decide hides every tool.
            # Catching broadly is deliberate here; the traceback is logged.
            logger.exception("layer %s failed in filter_tools", layer.name)
            decision = LayerDecision(layer.name, hook, mode, "error", DenyCode.LAYER_ERROR.value)
            return decision, (visible if mode is LayerMode.MONITOR else [])

        # Layers may only remove tools, never add or alter them.
        kept = [tool for tool in visible if tool.exposed_name in kept_names]
        removed = len(visible) - len(kept)
        elapsed_ms = (time.perf_counter() - started) * 1000
        if mode is LayerMode.MONITOR:
            verdict: LayerVerdict = "would_block" if removed else "allow"
            return LayerDecision(
                layer.name, hook, mode, verdict, None, removed, elapsed_ms
            ), visible
        verdict = "deny" if removed else "allow"
        return LayerDecision(layer.name, hook, mode, verdict, None, removed, elapsed_ms), kept

    async def _check_with(
        self,
        layer: BaseLayer,
        mode: LayerMode,
        decisions: list[LayerDecision],
        hook: Hook,
        run_hook: Callable[[], Awaitable[Verdict]],
        details: dict[str, JsonValue] | None = None,
    ) -> tuple[str, Deny] | None:
        """Run one layer hook, record its verdict, and return (layer, deny) if it blocks."""
        if mode is LayerMode.OFF:
            decisions.append(LayerDecision(layer.name, hook, mode, "off"))
            return None

        started = time.perf_counter()
        with _tracer.start_as_current_span(f"{attributes.SPAN_LAYER_PREFIX}{layer.name}"):
            try:
                verdict = await run_hook()
            except Exception:
                # Fail closed: in enforce mode a layer that cannot decide blocks the call.
                # Catching broadly is deliberate here; the traceback is logged.
                logger.exception("layer %s failed", layer.name)
                code = DenyCode.LAYER_ERROR
                decisions.append(LayerDecision(layer.name, hook, mode, "error", code.value))
                if mode is LayerMode.MONITOR:
                    return None
                return layer.name, Deny(code, POLICY_BLOCK_MESSAGE)
        elapsed_ms = (time.perf_counter() - started) * 1000
        if details is not None and verdict.approval_id is not None:
            details["approval_id"] = verdict.approval_id

        if isinstance(verdict, Allow) and verdict.unclassified:
            decisions.append(
                LayerDecision(
                    layer.name,
                    hook,
                    mode,
                    "unclassified",
                    UNRECORDED_CODE,
                    None,
                    elapsed_ms,
                    verdict.score,
                )
            )
            return None
        if not isinstance(verdict, Deny):
            decisions.append(
                LayerDecision(
                    layer.name, hook, mode, "allow", duration_ms=elapsed_ms, score=verdict.score
                )
            )
            return None
        code_value = verdict.code.value
        if mode is LayerMode.MONITOR:
            decisions.append(
                LayerDecision(
                    layer.name,
                    hook,
                    mode,
                    "would_block",
                    code_value,
                    None,
                    elapsed_ms,
                    verdict.score,
                )
            )
            return None
        decisions.append(
            LayerDecision(
                layer.name, hook, mode, "deny", code_value, None, elapsed_ms, verdict.score
            )
        )
        return layer.name, verdict

    async def _finish_blocked(
        self,
        ctx: CallContext,
        call: ToolCall,
        block: tuple[str, Deny],
        decisions: list[LayerDecision],
        started: float,
        details: dict[str, JsonValue],
    ) -> Blocked:
        blocked_by, deny = block
        details.update(outcome="blocked", blocked_by=blocked_by, deny_code=deny.code.value)
        await self._emit("gateway.tool_call", ctx, call.exposed_name, decisions, started, details)
        return Blocked(deny)

    async def _emit(
        self,
        action: str,
        ctx: CallContext,
        subject_id: str | None,
        decisions: Sequence[LayerDecision],
        started: float,
        details: dict[str, JsonValue],
    ) -> None:
        # Recording is best effort. A record that cannot be built, a sink that raises and a
        # sink that awaits for too long must not change what the client gets: by now the
        # upstream may already have run, and a failed write would otherwise look like a
        # failed call. The bound cannot interrupt a sink that blocks the event loop without
        # awaiting; sinks must not.
        try:
            payload: dict[str, JsonValue] = {
                "request_id": str(ctx.request_id),
                "client_name": ctx.client.name,
                "protocol_version": ctx.protocol_version,
                "pipeline_config_sha256": self._config.sha256,
                "enabled_layers": list(self._config.enabled_layers),
                "layers": [decision.to_payload() for decision in decisions],
                "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                **details,
            }
            event = GatewayEvent(
                action=action,
                actor_id=ctx.client.actor_id,
                subject_id=subject_id,
                payload=payload,
            )
            if self._audit is not None and action == "gateway.tool_call":
                # Queues the record: never waits, never raises. Listings are not audited.
                self._audit.record(event)
            with anyio.fail_after(self._emit_timeout_s):
                await self._events.emit(event)
        except Exception:
            # Catching broadly is deliberate. The traceback and the request id are logged, so
            # a missing record can be matched to its request; the event is not, because the
            # record is the thing that failed.
            logger.exception("could not record a %s event for request %s", action, ctx.request_id)
