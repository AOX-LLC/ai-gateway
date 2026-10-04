"""The schema layer: arguments must fit the reviewed input schema."""

from typing import Any

import pytest

from ai_gateway.pipeline.layers.schema import MAX_ARGUMENT_BYTES, SchemaLayer
from ai_gateway.pipeline.pins import ToolPins
from ai_gateway.pipeline.runner import Blocked
from ai_gateway.pipeline.types import Allow, Deny, DenyCode, Verdict
from ai_gateway.seams.events import MemoryEventSink
from tests.layer_helpers import TICKET_SCHEMA, call, ctx, last_layer, pins_for, pipeline_with, run

pytestmark = pytest.mark.anyio


async def _verdict(arguments: dict[str, Any], **options: Any) -> Verdict:
    pins = options.pop("pins", pins_for())
    return await SchemaLayer(pins).before_call(ctx(), call(arguments, **options))


async def test_arguments_that_fit_the_schema_pass() -> None:
    verdict = await _verdict({"subject": "Dock gate", "priority": "low", "count": 3})

    assert isinstance(verdict, Allow)


@pytest.mark.parametrize(
    "arguments",
    [
        {"subject": "x", "extra": "y"},
        {"subject": "x", "priority": "urgent"},
        {"subject": 5},
        {"subject": "x", "count": "3"},
        {"subject": "x", "count": True},
        {"subject": "x", "count": 11},
        {"subject": "y" * 101},
        {},
    ],
    ids=["extra", "enum", "type", "numeric-string", "bool-as-int", "range", "length", "required"],
)
async def test_extras_and_type_mismatches_are_refused(arguments) -> None:  # type: ignore[no-untyped-def]
    verdict = await _verdict(arguments)

    assert isinstance(verdict, Deny)
    assert verdict.code is DenyCode.SCHEMA_VIOLATION
    assert verdict.score is not None, "the count of violations, for the record"
    assert verdict.score >= 1


async def test_an_extra_argument_is_refused_even_when_the_servers_schema_allows_it() -> None:
    loose = {key: value for key, value in TICKET_SCHEMA.items() if key != "additionalProperties"}

    verdict = await _verdict(
        {"subject": "x", "extra": 1}, pins=pins_for(schema=loose), schema=loose
    )

    assert isinstance(verdict, Deny), "additionalProperties: false is forced at the top level"


@pytest.mark.parametrize(
    "arguments",
    [
        {"subject": "a\x00b"},
        {"subject": "x", "nested": {"a": {"b": {"c": {"d": {"e": {"f": {"g": 1}}}}}}}},
        {"subject": "x", "n": float("nan")},
        {"subject": "x", "n": float("inf")},
    ],
    ids=["nul", "deep", "nan", "inf"],
)
async def test_what_no_honest_client_sends_is_refused(arguments) -> None:  # type: ignore[no-untyped-def]
    loose = {"type": "object"}  # a schema that would otherwise let it all through
    verdict = await SchemaLayer(ToolPins({})).before_call(ctx(), call(arguments, schema=loose))

    assert isinstance(verdict, Deny)


async def test_arguments_over_the_size_limit_are_refused() -> None:
    verdict = await _verdict({"subject": "x" * (MAX_ARGUMENT_BYTES + 1)})

    assert isinstance(verdict, Deny)


async def test_the_pinned_schema_wins_over_what_the_catalog_offers_now() -> None:
    """A server that loosens its schema after review does not loosen the layer: it validates against
    what was reviewed."""
    loosened = {"type": "object", "properties": {"subject": {"type": "string"}}}

    verdict = await _verdict({"subject": "x", "count": 99}, schema=loosened)

    assert isinstance(verdict, Deny), "the pinned schema caps count at 10"


async def test_a_tool_with_no_pin_is_checked_against_the_catalog_schema() -> None:
    verdict = await SchemaLayer(ToolPins({})).before_call(
        ctx(), call({"subject": "x", "count": 99})
    )

    assert isinstance(verdict, Deny)


async def test_a_call_with_no_schema_at_all_is_refused() -> None:
    from ai_gateway.pipeline.types import ToolCall

    bare = ToolCall.create(
        "tickets__create_ticket", "tickets", "create_ticket", {"subject": "x"}, "write"
    )

    verdict = await SchemaLayer(ToolPins({})).before_call(ctx(), bare)

    assert isinstance(verdict, Deny), "nothing to validate against is not a pass"


async def test_the_modes_enforce_monitor_and_off() -> None:
    bad = call({"subject": "x", "extra": 1})
    outcomes = {}
    for mode in ("enforce", "monitor", "off"):
        events = MemoryEventSink()
        outcomes[mode] = (
            await run(pipeline_with(SchemaLayer, mode, events, pins=pins_for()), bad),
            last_layer(events),
        )

    assert isinstance(outcomes["enforce"][0], Blocked)
    assert outcomes["enforce"][1]["verdict"] == "deny"
    assert not isinstance(outcomes["monitor"][0], Blocked), "monitor lets it through"
    assert outcomes["monitor"][1]["verdict"] == "would_block"
    assert outcomes["monitor"][1]["code"] == "schema_violation"
    assert outcomes["monitor"][1]["score"] == 1
    assert outcomes["off"][1]["verdict"] == "off"


async def test_the_record_never_holds_the_argument_names_or_values() -> None:
    events = MemoryEventSink()
    await run(
        pipeline_with(SchemaLayer, "monitor", events, pins=pins_for()),
        call({"subject": "x", "secret_name_zq": "secret-value-zq"}),
    )

    assert "zq" not in str(events.events[-1].payload)


async def test_a_schema_that_points_at_a_remote_reference_is_never_fetched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An upstream's own schema (a tool with no pin) must not make the gateway fetch a URL: the
    gateway can reach places the upstream cannot."""
    import urllib.request

    def refuse(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("the gateway fetched a remote reference")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    hostile = {
        "type": "object",
        "properties": {"subject": {"$ref": "http://127.0.0.1:1/secret.json#/x"}},
    }

    verdict = await SchemaLayer(ToolPins({})).before_call(
        ctx(), call({"subject": "x"}, schema=hostile)
    )

    assert isinstance(verdict, Deny), "an unreachable reference is a refusal, not a fetch"


def test_a_pinned_schema_that_is_not_a_valid_schema_stops_startup() -> None:
    broken = {"type": "object", "properties": {"subject": {"type": "no-such-type"}}}

    with pytest.raises(Exception, match="no-such-type"):
        SchemaLayer(pins_for(schema=broken))
