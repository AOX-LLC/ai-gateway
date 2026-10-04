"""Turn decision records and spans into rows of the telemetry tables.

Everything here is an allowlist: a row is built from named fields of the record, never from
the record as a whole, and text is cut to the length its column allows. The record has no
argument, result, credential or address to begin with; this is the second line, and the
tables' own CHECKs are the third.
"""

import logging
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.trace import StatusCode
from psycopg.types.json import Jsonb

from ai_gateway.classifier.judge import UsageRecord
from ai_gateway.seams.events import GatewayEvent
from ai_gateway.telemetry import attributes
from ai_gateway.telemetry.buffer import Row

logger = logging.getLogger(__name__)

SPAN_SCOPE = "ai_gateway"
"""Only spans of the gateway's own instrumentation are stored. The MCP SDK's spans are
parented by whatever trace the client sent and are not ours to keep."""

# The span attributes that may be stored, and how long each value may be.
SPAN_ATTRIBUTES = {
    attributes.GATEWAY_TOOL: 64,
    attributes.GATEWAY_CLIENT: 100,
    attributes.GATEWAY_REQUEST_ID: 36,
}

_TOOL_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_NAMESPACE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
_SPAN_NAME = re.compile(r"^gateway\.[a-z][a-z0-9_.]{0,90}$")
_CLIENT_ACTOR = re.compile(r"^client:(?P<id>[0-9a-fA-F-]{36})$")
_OUTCOMES = {"forwarded", "blocked"}


def rows_for_event(event: GatewayEvent) -> list[Row]:
    """The rows a gateway event becomes; none for an action the store does not keep."""
    match event.action:
        case "gateway.tool_call" | "gateway.tools_list":
            return _request_rows(event)
        case "gateway.auth_failure":
            return [_auth_failure_row(event)]
        case _:
            return []


def pipeline_config_row(sha256: str, layers: list[dict[str, str]], first_seen: datetime) -> Row:
    """The layers (name and mode, in order) of a pipeline configuration."""
    return Row(
        "pipeline_configs", {"sha256": sha256, "first_seen": first_seen, "layers": Jsonb(layers)}
    )


def _request_rows(event: GatewayEvent) -> list[Row]:
    payload = event.payload
    request_id = UUID(str(payload["request_id"]))
    is_call = event.action == "gateway.tool_call"
    tool = event.subject_id if is_call and _is_tool_name(event.subject_id) else None
    outcome = _text(payload, "outcome", 16) if is_call else "listed"
    if outcome is None or (is_call and outcome not in _OUTCOMES):
        raise ValueError("a tool call record without a known outcome")

    request = {
        "request_id": request_id,
        "ts": event.occurred_at,
        "kind": "tool_call" if is_call else "tools_list",
        "client_id": _client_id(event.actor_id),
        "client_name": _text(payload, "client_name", 100),
        "tool": tool,
        "namespace": _namespace(payload, tool),
        "effect": _text(payload, "effect", 5),
        "effect_source": _text(payload, "effect_source", 7),
        "outcome": outcome,
        "blocked_by": _text(payload, "blocked_by", 63),
        "deny_code": _text(payload, "deny_code", 63),
        "upstream_status": _text(payload, "upstream_status", 32),
        "duration_ms": _number(payload, "duration_ms") or 0.0,
        "upstream_duration_ms": _number(payload, "upstream_duration_ms"),
        "args_sha256": _text(payload, "arguments_sha256", 64),
        "protocol_version": _text(payload, "protocol_version", 20),
        "pipeline_config_sha256": _text(payload, "pipeline_config_sha256", 64),
        "trace_id": _text(payload, "trace_id", 32),
        "tools_available": _integer(payload, "tools_available"),
        "tools_returned": _integer(payload, "tools_returned"),
    }
    rows = [Row("requests", request)]
    layers = payload.get("layers")
    for ordinal, layer in enumerate(layers if isinstance(layers, list) else []):
        if isinstance(layer, dict):
            rows.append(_layer_row(request_id, ordinal, event.occurred_at, layer))
    return rows


def _layer_row(request_id: UUID, ordinal: int, ts: datetime, layer: Mapping[str, Any]) -> Row:
    return Row(
        "layer_verdicts",
        {
            "request_id": request_id,
            "ordinal": ordinal,
            "ts": ts,
            "layer": _text(layer, "layer", 63),
            "hook": _text(layer, "hook", 11),
            "mode": _text(layer, "mode", 7),
            "verdict": _text(layer, "verdict", 11),
            "code": _text(layer, "code", 63),
            "tools_removed": _integer(layer, "tools_removed"),
            "duration_ms": _number(layer, "duration_ms"),
            "score": _integer(layer, "score"),
        },
    )


def _auth_failure_row(event: GatewayEvent) -> Row:
    return Row(
        "auth_failures",
        {
            "event_id": uuid4(),
            "ts": event.occurred_at,
            "reason": _text(event.payload, "reason", 31),
            "lookup_id": _text(event.payload, "lookup_id", 16),
        },
    )


def span_row(span: ReadableSpan) -> Row | None:
    """The row for a finished span of the gateway's own instrumentation, or None."""
    scope = span.instrumentation_scope
    context = span.get_span_context()
    if (
        scope is None
        or scope.name != SPAN_SCOPE
        or context is None
        or span.start_time is None
        or span.end_time is None
        or _SPAN_NAME.match(span.name) is None
    ):
        return None
    kept = {
        key: str(value)[:limit]
        for key, limit in SPAN_ATTRIBUTES.items()
        if (value := (span.attributes or {}).get(key)) is not None
    }
    return Row(
        "spans",
        {
            "trace_id": format(context.trace_id, "032x"),
            "span_id": format(context.span_id, "016x"),
            "parent_span_id": format(span.parent.span_id, "016x") if span.parent else None,
            "ts": datetime.fromtimestamp(span.start_time / 1e9, UTC),
            "name": span.name,
            "duration_us": max(0, (span.end_time - span.start_time) // 1000),
            "status": _STATUS.get(span.status.status_code, "unset"),
            "request_id": _uuid_or_none(kept.get(attributes.GATEWAY_REQUEST_ID)),
            "attrs": Jsonb(kept),
        },
    )


_STATUS = {StatusCode.UNSET: "unset", StatusCode.OK: "ok", StatusCode.ERROR: "error"}


def _text(source: Mapping[str, Any], key: str, limit: int) -> str | None:
    value = source.get(key)
    return value[:limit] if isinstance(value, str) and value else None


def _number(source: Mapping[str, Any], key: str) -> float | None:
    value = source.get(key)
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _integer(source: Mapping[str, Any], key: str) -> int | None:
    value = source.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _is_tool_name(name: str | None) -> bool:
    return name is not None and _TOOL_NAME.match(name) is not None


def _namespace(payload: Mapping[str, Any], tool: str | None) -> str | None:
    """The namespace the record names, else the one in the tool's name; only a well-formed one.

    A tool no upstream offers (`blocked_by` is `catalog`) has a name the client chose, so it gets
    no namespace: any client could otherwise invent namespaces in the store."""
    if payload.get("blocked_by") == "catalog":
        return None
    candidates = [_text(payload, "namespace", 63)]
    if tool is not None and "__" in tool:
        candidates.append(tool.split("__", 1)[0])
    return next((c for c in candidates if c is not None and _NAMESPACE.match(c)), None)


def _client_id(actor_id: str) -> UUID | None:
    match = _CLIENT_ACTOR.match(actor_id)
    return _uuid_or_none(match["id"]) if match else None


def _uuid_or_none(value: str | None) -> UUID | None:
    try:
        return UUID(value) if value else None
    except ValueError:
        return None


def model_usage_row(record: "UsageRecord", ts: datetime) -> Row:
    """A model call's books as a row: tokens, cost, mode and where it was made, never the text."""
    return Row(
        "model_usage",
        {
            "usage_id": uuid4(),
            "request_id": record.request_id,
            "ts": ts,
            "layer": "classifier",
            "purpose": record.purpose,
            "model": record.model[:100] or "none",
            "tier": record.tier,
            "mode": record.mode,
            "input_tokens": record.input_tokens,
            "output_tokens": record.output_tokens,
            "cache_read_tokens": record.cache_read_tokens,
            "cache_write_tokens": record.cache_write_tokens,
            "cost_usd": record.cost_usd,
            "latency_ms": record.latency_ms,
            "status": record.status,
        },
    )
