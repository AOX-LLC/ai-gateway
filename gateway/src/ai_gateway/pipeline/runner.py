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

from mcp.types import CallToolResult
from opentelemetry import trace
from pydantic import JsonValue

from ai_gateway.pipeline.config import PipelineConfig
from ai_gateway.pipeline.registry import LAYER_ORDER
from ai_gateway.pipeline.types import (
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
from ai_gateway.seams.events import EventSink, GatewayEvent
from ai_gateway.telemetry import attributes

logger = logging.getLogger(__name__)
_tracer = trace.get_tracer("ai_gateway")

POLICY_BLOCK_MESSAGE = "Request blocked by gateway policy."


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

LayerVerdict = Literal["allow", "deny", "would_block", "off", "error"]


@dataclass(frozen=True)
class LayerDecision:
    layer: str
    mode: LayerMode
    verdict: LayerVerdict
    code: str | None = None
    tools_removed: int | None = None
    duration_ms: float = 0.0

    def to_payload(self) -> dict[str, JsonValue]:
        payload: dict[str, JsonValue] = {
            "layer": self.layer,
            "mode": self.mode.value,
            "verdict": self.verdict,
            "duration_ms": round(self.duration_ms, 3),
        }
        if self.code is not None:
            payload["code"] = self.code
        if self.tools_removed is not None:
            payload["tools_removed"] = self.tools_removed
        return payload


class Pipeline:
    def __init__(
        self,
        layers: Sequence[BaseLayer],
        config: PipelineConfig,
        events: EventSink,
    ) -> None:
        self._layers = [(layer, config.modes[layer.name]) for layer in layers]
        self._config = config
        self._events = events

    @classmethod
    def build(
        cls,
        config: PipelineConfig,
        events: EventSink,
        layer_order: Sequence[type[BaseLayer]] = LAYER_ORDER,
    ) -> "Pipeline":
        return cls([layer_class() for layer_class in layer_order], config, events)

    async def list_tools(self, ctx: CallContext, tools: Sequence[CatalogTool]) -> list[CatalogTool]:
        started = time.perf_counter()
        visible = list(tools)
        decisions = []
        with _tracer.start_as_current_span(attributes.SPAN_TOOLS_LIST):
            for layer, mode in self._layers:
                decision, visible = await self._filter_with(layer, mode, ctx, visible)
                decisions.append(decision)

        await self._emit(
            "gateway.tools_list",
            ctx,
            subject_id=None,
            decisions=decisions,
            started=started,
            details={"tools_available": len(tools), "tools_returned": len(visible)},
        )
        return visible

    async def call_tool(self, ctx: CallContext, call: ToolCall, forward: Forwarder) -> CallOutcome:
        started = time.perf_counter()
        decisions: list[LayerDecision] = []
        details: dict[str, JsonValue] = {"arguments_sha256": call.arguments_sha256}

        with _tracer.start_as_current_span(attributes.SPAN_TOOL_CALL) as span:
            span.set_attribute(attributes.GATEWAY_TOOL, call.exposed_name)

            for layer, mode in self._layers:
                block = await self._check_with(
                    layer, mode, decisions, partial(layer.before_call, ctx, call)
                )
                if block is not None:
                    return await self._finish_blocked(ctx, call, block, decisions, started, details)

            with _tracer.start_as_current_span(attributes.SPAN_UPSTREAM_CALL):
                upstream = await forward(ctx, call)
            details["upstream_status"] = upstream.status.value

            for layer, mode in self._layers:
                block = await self._check_with(
                    layer, mode, decisions, partial(layer.after_call, ctx, call, upstream.result)
                )
                if block is not None:
                    return await self._finish_blocked(ctx, call, block, decisions, started, details)

        details["outcome"] = "forwarded"
        await self._emit("gateway.tool_call", ctx, call.exposed_name, decisions, started, details)
        return Forwarded(upstream.result)

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
            details={"outcome": "blocked", "blocked_by": "catalog", "deny_code": deny.code.value},
        )
        return deny

    async def _filter_with(
        self, layer: BaseLayer, mode: LayerMode, ctx: CallContext, visible: list[CatalogTool]
    ) -> tuple[LayerDecision, list[CatalogTool]]:
        if mode is LayerMode.OFF:
            return LayerDecision(layer.name, mode, "off"), visible

        started = time.perf_counter()
        try:
            kept_names = {tool.exposed_name for tool in await layer.filter_tools(ctx, visible)}
        except Exception:
            # Fail closed: in enforce mode a layer that cannot decide hides every tool.
            # Catching broadly is deliberate here; the traceback is logged.
            logger.exception("layer %s failed in filter_tools", layer.name)
            decision = LayerDecision(layer.name, mode, "error", DenyCode.LAYER_ERROR.value)
            return decision, (visible if mode is LayerMode.MONITOR else [])

        # Layers may only remove tools, never add or alter them.
        kept = [tool for tool in visible if tool.exposed_name in kept_names]
        removed = len(visible) - len(kept)
        elapsed_ms = (time.perf_counter() - started) * 1000
        if mode is LayerMode.MONITOR:
            verdict: LayerVerdict = "would_block" if removed else "allow"
            return LayerDecision(layer.name, mode, verdict, None, removed, elapsed_ms), visible
        verdict = "deny" if removed else "allow"
        return LayerDecision(layer.name, mode, verdict, None, removed, elapsed_ms), kept

    async def _check_with(
        self,
        layer: BaseLayer,
        mode: LayerMode,
        decisions: list[LayerDecision],
        run_hook: Callable[[], Awaitable[Verdict]],
    ) -> tuple[str, Deny] | None:
        """Run one layer hook, record its verdict, and return (layer, deny) if it blocks."""
        if mode is LayerMode.OFF:
            decisions.append(LayerDecision(layer.name, mode, "off"))
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
                decisions.append(LayerDecision(layer.name, mode, "error", code.value))
                if mode is LayerMode.MONITOR:
                    return None
                return layer.name, Deny(code, POLICY_BLOCK_MESSAGE)
        elapsed_ms = (time.perf_counter() - started) * 1000

        if not isinstance(verdict, Deny):
            decisions.append(LayerDecision(layer.name, mode, "allow", duration_ms=elapsed_ms))
            return None
        code_value = verdict.code.value
        if mode is LayerMode.MONITOR:
            decisions.append(
                LayerDecision(layer.name, mode, "would_block", code_value, None, elapsed_ms)
            )
            return None
        decisions.append(LayerDecision(layer.name, mode, "deny", code_value, None, elapsed_ms))
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
        payload: dict[str, JsonValue] = {
            "request_id": str(ctx.request_id),
            "protocol_version": ctx.protocol_version,
            "pipeline_config_sha256": self._config.sha256,
            "enabled_layers": list(self._config.enabled_layers),
            "layers": [decision.to_payload() for decision in decisions],
            "duration_ms": round((time.perf_counter() - started) * 1000, 3),
            **details,
        }
        await self._events.emit(
            GatewayEvent(
                action=action,
                actor_id=ctx.client.actor_id,
                subject_id=subject_id,
                payload=payload,
            )
        )
