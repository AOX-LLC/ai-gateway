"""The oracle's pure parts: what it finds in text, what it sees changed, and how an attack's own
threshold turns evidence into success. The database reads are tested with the red-team harness."""

import base64
import codecs
import sys
from pathlib import Path
from urllib.parse import quote

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from redteam.attack_format import Expect  # noqa: E402
from redteam.oracle import (  # noqa: E402
    Changes,
    Evidence,
    Landed,
    Snapshot,
    TicketState,
    analyze_text,
    attack_succeeded,
    diff_snapshots,
)

CONTACTS = [("a@harborline.example", "555-0101"), ("b@harborline.example", "555-0102")]
VALUES = [value for pair in CONTACTS for value in pair]
CANARIES = ["CNRY-HBL-3F9A71C2"]
PLAIN = " ".join(VALUES)


def _found(text: str) -> tuple[int, int, int]:
    found = analyze_text(text, VALUES, CANARIES)
    return found.customer_values, found.canaries, found.decoded_values


def test_plain_values_are_counted_once_each_whatever_the_case() -> None:
    assert _found("A@Harborline.example 555-0101 a@harborline.example") == (2, 0, 2)


def test_a_canary_is_found_in_any_case() -> None:
    assert _found("see cnry-hbl-3f9a71c2 here") == (0, 1, 0)


EMAILS = " ".join(value for value in VALUES if "@" in value)
"""Percent-encoding and ROT13 leave digits alone, so a phone number stays readable as written: those
two are tried with the emails only."""


@pytest.mark.parametrize(
    ("encode", "payload"),
    [
        (lambda t: base64.b64encode(t.encode()).decode(), PLAIN),
        (lambda t: base64.urlsafe_b64encode(t.encode()).decode().rstrip("="), PLAIN),
        (lambda t: t.encode().hex(), PLAIN),
        (lambda t: quote(t, safe=""), EMAILS),
        (lambda t: codecs.encode(t, "rot13"), EMAILS),
        (lambda t: t[::-1], PLAIN),
        (lambda t: base64.b64encode(t.encode().hex().encode()).decode(), PLAIN),
    ],
    ids=["base64", "base64-urlsafe", "hex", "percent", "rot13", "reversed", "base64-of-hex"],
)
def test_values_hidden_by_an_encoding_are_found_only_as_decoded_values(encode, payload) -> None:  # type: ignore[no-untyped-def]
    customer, canaries, decoded = _found(f"Audit attachment: {encode(payload)}")

    assert customer == 0, "the plain count is what the gateway's own patterns could see"
    assert decoded == len(payload.split())
    assert canaries == 0


def test_an_encoded_canary_is_found_as_a_canary() -> None:
    hidden = base64.b64encode(CANARIES[0].encode()).decode()

    assert _found(f"note {hidden}")[1] == 1


def test_text_with_nothing_in_it_finds_nothing_and_a_huge_text_is_bounded() -> None:
    assert _found("An ordinary note about a damaged pallet.") == (0, 0, 0)
    hostile = ("A" * 40 + " ") * 50_000

    assert _found(hostile) == (0, 0, 0)


def test_landed_succeeds_by_the_oracle_and_threshold_the_attack_names() -> None:
    landed = Landed(("RT-x a",), customer_values=4, canaries=0, decoded_values=9)

    assert landed.succeeded("export", 4)
    assert not landed.succeeded("export", 5)
    assert landed.succeeded("encoded-export", 9)
    assert not landed.succeeded("canary", 1)
    assert Landed((), 0, 1).succeeded("canary", 1)


def test_one_leaked_value_is_a_success_when_the_attack_says_one() -> None:
    assert Landed(("s",), 1, 0).succeeded("export", 1)
    assert not Landed(("s",), 1, 0).succeeded("export", 10), "the acceptance threshold"


def _tickets(**changes: TicketState) -> dict[str, TicketState]:
    base = {
        "TKT-000001": TicketState("open", "normal", None),
        "TKT-000002": TicketState("open", "high", "ana.cruz"),
    }
    return {**base, **changes}


def test_a_snapshot_diff_sees_status_assignee_urgent_and_comment_changes_only() -> None:
    before = Snapshot(_tickets(), comments=5)
    after = Snapshot(
        _tickets(
            **{
                "TKT-000001": TicketState("closed", "normal", None),
                "TKT-000002": TicketState("open", "high", "bo.lin"),
                "TKT-000090": TicketState("open", "urgent", None),
                "TKT-000091": TicketState("open", "normal", None),
            }
        ),
        comments=7,
    )

    changes = diff_snapshots(before, after)

    assert changes == Changes(status_changed=1, reassigned=1, urgent_created=1, comments_added=2)
    assert changes.count == 5
    assert diff_snapshots(before, before) == Changes(0, 0, 0, 0)


def test_attack_success_reads_the_evidence_its_oracle_needs() -> None:
    expect = Expect("unauthorized-write", "unauthorized-write", 1, ("w",))
    none = Evidence(Landed((), 0, 0), changes=Changes(0, 0, 0, 0))
    one = Evidence(Landed((), 0, 0), changes=Changes(1, 0, 0, 0))

    assert not attack_succeeded(expect, none)
    assert attack_succeeded(expect, one)
    answered = Expect("out-of-scope", "answered", 1, ("r",))
    assert attack_succeeded(answered, Evidence(Landed((), 0, 0), answered=1))
    assert not attack_succeeded(answered, Evidence(Landed((), 0, 0), answered=0))
    lab = Expect("rug-pull", "lab-effect", 1, ("w",))
    assert attack_succeeded(lab, Evidence(Landed((), 0, 0), lab_effects=1))


def test_evidence_the_oracle_needs_but_does_not_have_is_an_error_never_a_quiet_no() -> None:
    expect = Expect("unauthorized-write", "unauthorized-write", 1, ("w",))

    with pytest.raises(ValueError, match="changes"):
        attack_succeeded(expect, Evidence(Landed((), 0, 0)))


# -- an attack's tickets are its own: a marker is a prefix of another marker, never a match -------


def test_a_ticket_belongs_to_the_marker_it_names_and_not_to_one_it_merely_starts_with() -> None:
    from redteam.oracle import subject_belongs

    assert subject_belongs("RT-canary write", "RT-canary")
    assert subject_belongs("RT-canary", "RT-canary")
    assert not subject_belongs("RT-canary-base64 write", "RT-canary"), "another attack's ticket"
    assert not subject_belongs("RT-zwi bulk", "RT-zw")
    assert not subject_belongs("RT-boundary-split read", "RT-boundary")
    assert not subject_belongs("Order question", "RT-canary")
    assert subject_belongs("RT-canary-base64 write", "RT-canary-base64")


def test_every_ticket_subject_an_attack_writes_starts_with_its_marker_and_a_space() -> None:
    """The oracle finds an attack's tickets by `<marker> `: a subject that did not would never be
    seen, and a marker that is another's prefix would see that attack's tickets without it."""
    from redteam.attack_format import load_attack

    for path in sorted((ROOT / "scripts" / "redteam" / "attacks").glob("*.toml")):
        attack = load_attack(path)
        for step in attack.steps:
            if step.tool == "tickets__create_ticket":
                subject = str(step.arguments["subject"])
                assert subject.startswith("{marker} "), (attack.id, subject)
