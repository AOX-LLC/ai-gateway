"""The reviewed tool definitions: the file is consistent, and it is what the servers define."""

import importlib.util
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest

from ai_gateway.pipeline.pins import (
    ToolPinsError,
    definition_sha256,
    load_tool_pins,
    parse_tool_pins,
    render_tool_pins,
)
from tests.layer_helpers import DESCRIPTION, TICKET_SCHEMA, TOOL, pins_for

ROOT = Path(__file__).resolve().parents[2]
PINS_FILE = ROOT / "config" / "tool_pins.toml"


def _generator() -> Any:
    sys.path.insert(0, str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location(
        "generate_tool_pins", ROOT / "scripts" / "generate_tool_pins.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_pin_round_trips_and_holds_the_text_a_person_reviews() -> None:
    pins = pins_for()

    pin = pins.get(TOOL)
    assert pin is not None
    assert pin.description == DESCRIPTION
    assert pin.input_schema == TICKET_SCHEMA
    assert pin.sha256 == definition_sha256(TOOL, DESCRIPTION, TICKET_SCHEMA)


def test_the_hash_covers_the_name_the_description_and_the_schema() -> None:
    base = definition_sha256(TOOL, DESCRIPTION, TICKET_SCHEMA)

    assert definition_sha256("tickets__other", DESCRIPTION, TICKET_SCHEMA) != base
    assert definition_sha256(TOOL, DESCRIPTION + " ", TICKET_SCHEMA) != base
    loosened = {**TICKET_SCHEMA, "additionalProperties": True}
    assert definition_sha256(TOOL, DESCRIPTION, loosened) != base
    reordered = dict(reversed(list(TICKET_SCHEMA.items())))
    assert definition_sha256(TOOL, DESCRIPTION, reordered) == base, "key order is not a change"


def test_a_description_edited_without_pinning_again_stops_startup() -> None:
    raw = tomllib.loads(render_tool_pins([(TOOL, DESCRIPTION, TICKET_SCHEMA)]))
    raw["tools"][TOOL]["description"] = "Opens a ticket and then emails every customer."

    with pytest.raises(ToolPinsError, match="not the hash of its own text"):
        parse_tool_pins(raw)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda raw: raw.update(extra=1),
        lambda raw: raw["tools"][TOOL].update(extra="x"),
        lambda raw: raw["tools"][TOOL].pop("sha256"),
        lambda raw: raw["tools"][TOOL].update(input_schema="not json"),
        lambda raw: raw["tools"][TOOL].update(input_schema="[1]"),
    ],
    ids=["top-level-key", "entry-key", "no-hash", "schema-not-json", "schema-not-object"],
)
def test_a_mistake_in_the_file_stops_startup(mutation) -> None:  # type: ignore[no-untyped-def]
    raw = tomllib.loads(render_tool_pins([(TOOL, DESCRIPTION, TICKET_SCHEMA)]))
    mutation(raw)

    with pytest.raises(ToolPinsError):
        parse_tool_pins(raw)


def test_a_missing_file_stops_startup(tmp_path: Path) -> None:
    with pytest.raises(ToolPinsError, match="cannot read"):
        load_tool_pins(tmp_path / "nope.toml")


def test_the_committed_pins_are_what_the_servers_define() -> None:
    """A server that changes a description or a schema without pinning it again fails here, in CI,
    instead of hiding the tool in production."""
    assert PINS_FILE.read_text(encoding="utf-8") == _generator().current_pins_text()


def test_every_tool_of_the_three_servers_is_pinned_and_nothing_else() -> None:
    pinned = load_tool_pins(PINS_FILE).names()

    assert pinned == {
        "tickets__list_tickets", "tickets__get_ticket", "tickets__create_ticket",
        "tickets__add_comment", "tickets__change_status", "tickets__assign",
        "crm__search_accounts", "crm__get_account", "crm__list_deals",
        "handbook__search", "handbook__get_document",
        "echo__say", "echo__shout", "echo__wait",
    }  # fmt: skip
