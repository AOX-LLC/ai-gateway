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


async def test_what_a_client_read_in_one_session_cannot_be_written_out_from_another() -> None:
    layer = EgressLayer(SHIPPED)
    await _read(layer, _accounts(1, 20), ctx(session="a"))

    other_session = await layer.before_call(
        ctx(session="b"), _write(" ".join(f"ACC-{n:05d}" for n in range(1, 8)))
    )

    assert isinstance(other_session, Deny)
    assert other_session.code is DenyCode.EGRESS_BULK
    assert other_session.score == 7


async def test_what_one_client_read_does_not_count_for_another_client() -> None:
    layer = EgressLayer(SHIPPED)
    await _read(layer, _accounts(1, 20), ctx(session="a"))

    other_client = await layer.before_call(
        ctx("another-bot", session="a"), _write(" ".join(f"ACC-{n:05d}" for n in range(1, 8)))
    )

    assert isinstance(other_client, Allow)


async def test_a_copy_dripped_out_across_sessions_is_caught_by_the_clients_window_tally() -> None:
    layer = EgressLayer(SHIPPED)
    await _read(layer, _accounts(1, 30), ctx(session="reader"))

    def four(first: int):  # type: ignore[no-untyped-def]
        return _write(" ".join(f"ACC-{n:05d}" for n in range(first, first + 4)))

    verdicts = [
        await layer.before_call(ctx(session=f"writer-{n}"), four(1 + 4 * n)) for n in range(3)
    ]

    assert [isinstance(v, Allow) for v in verdicts] == [True, True, False]
    refused = verdicts[2]
    assert isinstance(refused, Deny)
    assert refused.score is not None
    assert refused.score >= 10


async def test_an_honest_triage_across_sessions_passes() -> None:
    """One session looks the account up, another opens the ticket: the ticket names the account it
    is about (exempt) and cites a value or two, as a real one does."""
    layer = EgressLayer(SHIPPED)
    await _read(layer, _accounts(3, 1) + " ops@harborline.example 555-0142", ctx(session="look"))

    ticket = await layer.before_call(
        ctx(session="open"),
        _write("Damaged order for ACC-00003, call 555-0142", account_id="ACC-00003"),
    )

    assert isinstance(ticket, Allow)
    assert ticket.score == 1, "the phone number; the account the ticket is about is exempt"


async def test_a_value_read_is_forgotten_after_the_window_unless_it_is_read_again() -> None:
    now = [1000.0]
    layer = EgressLayer(SHIPPED, clock=lambda: now[0])
    bulk = _write(" ".join(f"ACC-{n:05d}" for n in range(1, 8)))
    await _read(layer, _accounts(1, 20), ctx(session="a"))

    now[0] += 1700  # inside the 30 minutes
    inside = await layer.before_call(ctx(session="b"), bulk)
    await _read(layer, _accounts(1, 20), ctx(session="c"))  # read again: the window starts over
    now[0] += 1700
    renewed = await layer.before_call(ctx(session="d"), bulk)
    now[0] += 1900  # past it, with no read since
    after = await layer.before_call(ctx(session="e"), bulk)

    assert isinstance(inside, Deny)
    assert isinstance(renewed, Deny)
    assert isinstance(after, Allow)
    assert after.score == 0


async def test_the_tally_of_what_a_client_tried_to_write_also_ages_out() -> None:
    now = [1000.0]
    layer = EgressLayer(SHIPPED, clock=lambda: now[0])
    await _read(layer, _accounts(1, 30), ctx(session="a"))
    refused = await layer.before_call(
        ctx(session="b"), _write(" ".join(f"ACC-{n:05d}" for n in range(1, 12)))
    )
    now[0] += 1700
    await _read(layer, _accounts(1, 30), ctx(session="c"))  # still reading: values stay known
    soon = await layer.before_call(ctx(session="d"), _write("see ACC-00003"))
    now[0] += 1900
    await _read(layer, _accounts(1, 30), ctx(session="e"))
    later = await layer.before_call(ctx(session="f"), _write("see ACC-00003"))

    assert isinstance(refused, Deny)
    assert isinstance(soon, Deny), "the attempt 1700 s ago still counts"
    assert isinstance(later, Allow), "and 3600 s ago it does not"


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

    held = repr(vars(layer)) + repr([vars(ledger) for ledger in layer._clients.values()])
    assert marker not in held
    assert "ACC-00001" not in held
    assert "someone@" not in held
    ledger = next(iter(layer._clients.values()))
    assert all(isinstance(fingerprint, int) for fingerprint in [*ledger.read, *ledger.egressed])
    assert all(isinstance(f, int) for tally in ledger.sessions.values() for f in tally)


async def test_every_cap_is_per_client_so_one_client_cannot_push_another_out() -> None:
    config = EgressConfig(
        max_sessions_per_client=3,
        max_values_per_client=10,
        identifier_patterns=SHIPPED.identifier_patterns,
    )
    layer = EgressLayer(config)
    honest = ctx("honest-bot", session="v")
    await _read(layer, _accounts(1, 6), honest)
    for n in range(300):  # a client that opens session after session, each reading what it can
        loud_session = ctx("loud-bot", session=f"s{n}")
        await _read(layer, _accounts(1, 20), loud_session)
        await layer.before_call(loud_session, _write("nothing to see"))

    loud = layer._clients[str(ctx("loud-bot").client.id)]
    quiet = layer._clients[str(honest.client.id)]
    bulk = await layer.before_call(
        ctx("honest-bot", session="other"), _write(" ".join(f"ACC-{n:05d}" for n in range(1, 7)))
    )
    cites_one = await layer.before_call(ctx("honest-bot", session="x"), _write("see ACC-00001"))

    assert len(loud.read) <= 10, "its own cap held"
    assert len(loud.sessions) <= 3, "and so did its session tallies"
    assert len(quiet.read) == 6, "nothing of the honest client's ledger was dropped"
    assert isinstance(bulk, Deny), "still checked, from any session"
    assert bulk.code is DenyCode.EGRESS_BULK
    assert isinstance(cites_one, Allow), "not refused for want of state"


async def test_a_clients_flooding_leaves_another_clients_writes_unaffected() -> None:
    config = EgressConfig(max_values_per_client=5, identifier_patterns=SHIPPED.identifier_patterns)
    layer = EgressLayer(config)
    await _read(layer, _accounts(1, 30), ctx("loud-bot", session="a"))  # past its cap: saturated
    await _read(layer, _accounts(1, 3), ctx("quiet-bot", session="a"))

    loud = await layer.before_call(ctx("loud-bot", session="b"), _write("see ACC-00099"))
    quiet = await layer.before_call(ctx("quiet-bot", session="b"), _write("see ACC-00099"))

    assert isinstance(loud, Deny)
    assert loud.code is DenyCode.EGRESS_STATE_LOST
    assert isinstance(quiet, Allow)


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


async def test_a_client_that_read_more_than_it_is_tracked_for_cannot_write_values_for_a_while() -> (
    None
):
    """Flooding the ledger with reads must not make the client forget what it read afterwards: past
    the cap the layer refuses writes that carry any value (it cannot check them), not wave them
    through, until the window has passed."""
    now = [0.0]
    config = EgressConfig(max_values_per_client=5, identifier_patterns=SHIPPED.identifier_patterns)
    layer, context = EgressLayer(config, clock=lambda: now[0]), ctx()
    await _read(layer, _accounts(1, 30), context)

    carrying = await layer.before_call(context, _write("see ACC-00099"))
    plain = await layer.before_call(context, _write("see the attached"))
    now[0] += 2000
    later = await layer.before_call(ctx(session="other"), _write("see ACC-00099"))

    assert isinstance(carrying, Deny)
    assert carrying.code is DenyCode.EGRESS_STATE_LOST
    assert isinstance(plain, Allow)
    assert isinstance(later, Allow), "the window has passed and the values it read are forgotten"


async def test_a_session_tally_dropped_for_room_starts_fresh_but_the_ledger_applies() -> None:
    config = EgressConfig(
        max_sessions_per_client=2, identifier_patterns=SHIPPED.identifier_patterns
    )
    layer = EgressLayer(config)
    victim = ctx(session="victim")
    await _read(layer, _accounts(1, 20), victim)
    for other in ("a", "b"):
        await layer.before_call(ctx(session=other), _write("nothing"))  # crowd its tally out

    verdict = await layer.before_call(victim, _write(" ".join(f"ACC-{n:05d}" for n in range(1, 8))))

    assert isinstance(verdict, Deny), "what the client read is still known"
    assert verdict.code is DenyCode.EGRESS_BULK


async def test_arguments_too_large_to_scan_are_refused_not_scanned_in_part() -> None:
    from ai_gateway.pipeline.layers.egress import _MAX_TEXT

    layer, context = EgressLayer(SHIPPED), ctx()
    padded = "x" * (_MAX_TEXT + 10) + " ".join(f"ACC-{n:05d}" for n in range(1, 8))

    verdict = await layer.before_call(context, _write(padded))

    assert isinstance(verdict, Deny), "values hidden behind padding"
    assert verdict.code is DenyCode.EGRESS_STATE_LOST


async def test_a_result_too_large_to_scan_marks_the_session_so_writes_with_values_are_refused() -> (
    None
):
    from ai_gateway.pipeline.layers.egress import _MAX_TEXT

    layer, context = EgressLayer(SHIPPED), ctx()
    await _read(layer, "y" * (_MAX_TEXT + 10) + _accounts(1, 20), context)

    verdict = await layer.before_call(context, _write("see ACC-00005"))

    assert isinstance(verdict, Deny)


async def test_a_session_at_its_tally_is_quarantined_and_a_new_session_starts_clean() -> None:
    """A refused attempt still counts, so one bulk attempt fills the session's tally. After that
    every write in the session is refused, even one that carries nothing it read (a session that
    tried a bulk copy is suspect, and its own words are how it would carry on). A new session of
    the same client starts clean."""
    layer, context = EgressLayer(SHIPPED), ctx()
    await _read(layer, _accounts(1, 30), context)
    bulk = await layer.before_call(context, _write(" ".join(f"ACC-{n:05d}" for n in range(1, 12))))

    cites = await layer.before_call(context, _write("Damaged order for ACC-00003"))
    own_words = await layer.before_call(context, _write("The pallet arrived with crushed corners"))

    assert isinstance(bulk, Deny)
    assert isinstance(cites, Deny)
    assert cites.code is DenyCode.EGRESS_BULK
    assert isinstance(own_words, Deny)
    assert own_words.code is DenyCode.EGRESS_BULK
    fresh = await layer.before_call(
        ctx(session="s2"), _write("The pallet arrived with crushed corners")
    )
    assert isinstance(fresh, Allow)
