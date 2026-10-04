"""The egress layer: data read in a session does not flow back out through a write."""

import base64
import json
import tomllib
from pathlib import Path

import pytest

from ai_gateway.pipeline.layers.egress import (
    EgressConfig,
    EgressConfigError,
    EgressLayer,
    load_egress_config,
    parse_egress_config,
)
from ai_gateway.pipeline.runner import Blocked
from ai_gateway.pipeline.types import Allow, Deny, DenyCode
from ai_gateway.seams.events import MemoryEventSink
from tests.layer_helpers import call, ctx, last_layer, pipeline_with, result

pytestmark = pytest.mark.anyio
ROOT = Path(__file__).resolve().parents[2]
SHIPPED = load_egress_config(ROOT / "config" / "egress.toml")
READ = "crm__search_accounts"
WRITE = "tickets__create_ticket"


def _accounts(first: int, count: int) -> str:
    return json.dumps(
        {
            "accounts": [
                {"id": f"ACC-{n:05d}", "name": f"Fictional {n}"}
                for n in range(first, first + count)
            ]
        }
    )


async def _read(layer: EgressLayer, text: str, context=None) -> None:  # type: ignore[no-untyped-def]
    await layer.after_call(context or ctx(), call({}, name=READ, effect="read"), result(text))


def _write(description: str, **extra: object):  # type: ignore[no-untyped-def]
    return call({"subject": "s", "description": description, **extra}, name=WRITE)


async def test_a_write_that_copies_a_bulk_of_what_was_read_is_refused() -> None:
    layer, context = EgressLayer(SHIPPED), ctx()
    await _read(layer, _accounts(1, 20), context)

    verdict = await layer.before_call(
        context, _write(" ".join(f"ACC-{n:05d}" for n in range(1, 8)))
    )

    assert isinstance(verdict, Deny)
    assert verdict.code is DenyCode.EGRESS_BULK
    assert verdict.score == 7


async def test_a_few_values_cited_in_a_write_are_normal() -> None:
    layer, context = EgressLayer(SHIPPED), ctx()
    await _read(layer, _accounts(1, 20), context)

    verdict = await layer.before_call(context, _write("Damaged order for ACC-00003 and ACC-00004"))

    assert isinstance(verdict, Allow)
    assert verdict.score == 2, "kept for monitoring: how close honest calls come to the limit"


async def test_values_the_session_never_read_are_not_egress() -> None:
    layer, context = EgressLayer(SHIPPED), ctx()
    await _read(layer, _accounts(1, 3), context)

    verdict = await layer.before_call(
        context, _write(" ".join(f"ACC-{n:05d}" for n in range(30, 40)))
    )

    assert isinstance(verdict, Allow)
    assert verdict.score == 0


async def test_the_same_copy_dripped_out_a_few_at_a_time_is_caught_across_writes() -> None:
    layer, context = EgressLayer(SHIPPED), ctx()
    await _read(layer, _accounts(1, 30), context)
    verdicts = [
        await layer.before_call(
            context, _write(" ".join(f"ACC-{n:05d}" for n in (first, first + 1, first + 2)))
        )
        for first in (1, 4, 7, 10)
    ]

    assert [isinstance(v, Allow) for v in verdicts] == [True, True, True, False]
    refused = verdicts[-1]
    assert isinstance(refused, Deny)
    assert refused.score is not None
    assert refused.score >= 10


async def test_what_one_session_read_does_not_count_for_another_session_or_client() -> None:
    layer = EgressLayer(SHIPPED)
    await _read(layer, _accounts(1, 20), ctx(session="a"))

    other_session = await layer.before_call(
        ctx(session="b"), _write(" ".join(f"ACC-{n:05d}" for n in range(1, 8)))
    )
    other_client = await layer.before_call(
        ctx("another-bot", session="a"), _write(" ".join(f"ACC-{n:05d}" for n in range(1, 8)))
    )

    assert isinstance(other_session, Allow)
    assert isinstance(other_client, Allow)


async def test_emails_and_phones_read_count_as_values() -> None:
    layer, context = EgressLayer(SHIPPED), ctx()
    contacts = "\n".join(f"person{n}@harborline.example 555-01{n:02d}" for n in range(10, 14))
    await _read(layer, contacts, context)

    verdict = await layer.before_call(context, _write(contacts))

    assert isinstance(verdict, Deny)


async def test_an_internal_only_marker_in_a_write_is_refused_whatever_was_read() -> None:
    layer = EgressLayer(SHIPPED)

    verdict = await layer.before_call(ctx(), _write("note: [internal-only] credit limit 7777"))

    assert isinstance(verdict, Deny)
    assert verdict.code is DenyCode.EGRESS_MARKER


async def test_the_record_a_write_is_about_is_not_counted() -> None:
    layer, context = EgressLayer(SHIPPED), ctx()
    await _read(layer, " ".join(f"TKT-{n:06d}" for n in range(1, 21)), context)

    verdicts = [
        await layer.before_call(
            context,
            call({"ticket_id": f"TKT-{n:06d}", "status": "closed"}, name="tickets__change_status"),
        )
        for n in range(1, 16)
    ]

    assert all(isinstance(v, Allow) for v in verdicts), "an ops bot working through a list"


async def test_a_read_is_never_refused_and_a_write_is_the_only_thing_checked() -> None:
    layer, context = EgressLayer(SHIPPED), ctx()
    await _read(layer, _accounts(1, 20), context)

    verdict = await layer.before_call(
        context,
        call({"query": " ".join(f"ACC-{n:05d}" for n in range(1, 9))}, name=READ, effect="read"),
    )

    assert isinstance(verdict, Allow)


async def test_the_ledger_holds_no_argument_or_result_content() -> None:
    marker = "marker-text-that-must-not-be-kept"
    layer, context = EgressLayer(SHIPPED), ctx()
    await _read(layer, f"{marker} ACC-00001 someone@harborline.example", context)
    await layer.before_call(context, _write(f"{marker} ACC-00001"))

    held = repr(vars(layer)) + repr([vars(ledger) for ledger in layer._ledgers.values()])
    assert marker not in held
    assert "ACC-00001" not in held
    assert "someone@" not in held
    ledger = next(iter(layer._ledgers.values()))
    assert all(isinstance(fingerprint, int) for fingerprint in ledger.read | ledger.egressed)


async def test_the_ledger_is_bounded_and_the_least_recent_sessions_go_first() -> None:
    layer = EgressLayer(
        EgressConfig(
            max_sessions=3,
            max_values_per_session=5,
            max_values_total=100,
            identifier_patterns=SHIPPED.identifier_patterns,
        )
    )
    for session in ("a", "b", "c", "d"):
        await _read(layer, _accounts(1, 20), ctx(session=session))

    assert len(layer._ledgers) == 3, "the oldest session was dropped"
    assert all(len(ledger.read) <= 5 for ledger in layer._ledgers.values()), "per-session cap"
    assert layer.stored_values() == sum(
        len(s.read) + len(s.egressed) for s in layer._ledgers.values()
    )


async def test_the_total_cap_drops_whole_sessions() -> None:
    layer = EgressLayer(
        EgressConfig(
            max_values_total=12,
            max_values_per_session=10,
            identifier_patterns=SHIPPED.identifier_patterns,
        )
    )
    for session in ("a", "b", "c"):
        await _read(layer, _accounts(1, 10), ctx(session=session))

    assert layer.stored_values() <= 12 + 10, "never more than a session over the cap"
    assert len(layer._ledgers) < 3


async def test_the_modes_and_the_score_in_the_record() -> None:
    bulk = _write(" ".join(f"ACC-{n:05d}" for n in range(1, 8)))
    seen = {}
    for mode in ("enforce", "monitor", "off"):
        layer = EgressLayer(SHIPPED)
        events = MemoryEventSink()
        pipeline = pipeline_with(EgressLayer, mode, events)
        pipeline._layers[0] = (layer, pipeline._layers[0][1])
        await layer.after_call(ctx(), call({}, name=READ, effect="read"), result(_accounts(1, 20)))

        async def forward(c, k):  # type: ignore[no-untyped-def]
            from ai_gateway.pipeline.runner import UpstreamOutcome, UpstreamStatus

            return UpstreamOutcome(result("ok"), UpstreamStatus.OK)

        outcome = await pipeline.call_tool(ctx(), bulk, forward)
        seen[mode] = (outcome, last_layer(events))

    assert isinstance(seen["enforce"][0], Blocked)
    assert seen["enforce"][1]["code"] == "egress_bulk"
    assert not isinstance(seen["monitor"][0], Blocked)
    assert seen["monitor"][1]["verdict"] == "would_block"
    assert seen["monitor"][1]["score"] == 7
    assert seen["off"][1]["verdict"] == "off"


def test_the_shipped_configuration_loads_and_a_mistake_stops_startup(tmp_path: Path) -> None:
    assert SHIPPED.per_write == 5
    assert SHIPPED.markers == ("[INTERNAL-ONLY]",)
    for bad in (
        {"limits": {"per_write": 0}},
        {"unknown": 1},
        {"values": {"identifier_patterns": ["("]}},
        {"markers": {"internal": [""]}},
        {"exempt": {"t": "x"}},
    ):
        with pytest.raises(EgressConfigError):
            parse_egress_config(bad)
    with pytest.raises(EgressConfigError):
        load_egress_config(tmp_path / "nope.toml")
    assert (
        tomllib.loads((ROOT / "config" / "egress.toml").read_text())["limits"]["per_session"] == 10
    )


@pytest.mark.parametrize(
    "disguise",
    [
        lambda text: text.lower(),
        lambda text: text.replace("-", "\u2011"),
        lambda text: text.replace("-", "\u2212"),
        lambda text: "".join(chr(ord(c) + 0xFEE0) if c.isalnum() else c for c in text),
        lambda text: text.replace("ACC", "A\u200bCC"),
        lambda text: base64.b64encode(text.encode()).decode(),
        lambda text: text.encode().hex(),
    ],
    ids=["lower", "nb-hyphen", "minus", "fullwidth", "zero-width", "base64", "hex"],
)
async def test_a_value_dressed_up_to_slip_past_the_pattern_is_still_counted(disguise) -> None:  # type: ignore[no-untyped-def]
    layer, context = EgressLayer(SHIPPED), ctx()
    await _read(layer, _accounts(1, 20), context)
    ids = " ".join(f"ACC-{n:05d}" for n in range(1, 8))

    verdict = await layer.before_call(context, _write(disguise(ids)))

    assert isinstance(verdict, Deny)


async def test_a_value_in_a_dict_key_is_counted_too() -> None:
    layer, context = EgressLayer(SHIPPED), ctx()
    await _read(layer, _accounts(1, 20), context)
    keyed = {f"ACC-{n:05d}": "x" for n in range(1, 8)}

    verdict = await layer.before_call(context, call({"subject": "s", "notes": keyed}, name=WRITE))

    assert isinstance(verdict, Deny)


async def test_a_session_that_read_more_than_it_is_tracked_for_cannot_write_values() -> None:
    """Flooding a session with reads must not make it forget what it read afterwards: past the cap
    the layer refuses writes that carry any value (it cannot check them), not wave them through."""
    config = EgressConfig(max_values_per_session=5, identifier_patterns=SHIPPED.identifier_patterns)
    layer, context = EgressLayer(config), ctx()
    await _read(layer, _accounts(1, 30), context)

    carrying = await layer.before_call(context, _write("see ACC-00099"))
    plain = await layer.before_call(context, _write("see the attached"))

    assert isinstance(carrying, Deny)
    assert carrying.code is DenyCode.EGRESS_STATE_LOST
    assert isinstance(plain, Allow)


async def test_a_session_whose_ledger_was_dropped_for_room_cannot_write_values() -> None:
    config = EgressConfig(max_sessions=2, identifier_patterns=SHIPPED.identifier_patterns)
    layer = EgressLayer(config)
    victim = ctx(session="victim")
    await _read(layer, _accounts(1, 20), victim)
    for other in ("a", "b"):
        await _read(layer, _accounts(1, 2), ctx(session=other))  # crowd the victim out

    verdict = await layer.before_call(victim, _write(" ".join(f"ACC-{n:05d}" for n in range(1, 8))))

    assert isinstance(verdict, Deny), "forgetting would fail open"
    assert verdict.code is DenyCode.EGRESS_STATE_LOST
