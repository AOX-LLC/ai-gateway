"""The allowlist layer: value rules per client and tool, and a file that cannot be misread."""

from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from ai_gateway.pipeline.layers.allowlist import (
    AllowlistError,
    AllowlistLayer,
    load_allowlist,
)
from ai_gateway.pipeline.types import ALLOW, CallContext, ClientIdentity, Deny, DenyCode, ToolCall

ROOT = Path(__file__).resolve().parents[2]


def _ctx(client: str = "harborline-support-bot") -> CallContext:
    return CallContext(
        request_id=uuid4(),
        client=ClientIdentity(id=uuid4(), name=client, scopes=frozenset()),
        session_id=None,
        protocol_version="2025-11-25",
    )


def _call(tool: str = "tickets__create_ticket", **arguments: Any) -> ToolCall:
    return ToolCall.create(tool, "tickets", tool.split("__")[1], arguments, "write")


def _layer(tmp_path: Path, text: str) -> AllowlistLayer:
    path = tmp_path / "allowlist.toml"
    path.write_text(text)
    return AllowlistLayer(load_allowlist(path))


RULE = """
[[rule]]
name = "r"
client = "harborline-support-bot"
tool = "tickets__create_ticket"
argument = "priority"
one_of = ["low", "normal", "high"]
"""


@pytest.mark.anyio
async def test_a_value_outside_the_list_is_refused_and_the_message_does_not_say_why(
    tmp_path: Path,
) -> None:
    layer = _layer(tmp_path, RULE)

    refused = await layer.before_call(_ctx(), _call(priority="urgent"))

    assert isinstance(refused, Deny)
    assert refused.code is DenyCode.ALLOWLIST_VIOLATION
    assert refused.public_message == "Request blocked by gateway policy."
    assert "priority" not in refused.public_message
    assert "urgent" not in refused.public_message


@pytest.mark.anyio
async def test_a_value_in_the_list_other_clients_other_tools_and_absent_arguments_pass(
    tmp_path: Path,
) -> None:
    layer = _layer(tmp_path, RULE)

    assert await layer.before_call(_ctx(), _call(priority="high")) is ALLOW
    assert await layer.before_call(_ctx("harborline-ops-bot"), _call(priority="urgent")) is ALLOW
    assert await layer.before_call(_ctx(), _call("tickets__assign", priority="urgent")) is ALLOW
    assert await layer.before_call(_ctx(), _call(subject="no priority given")) is ALLOW


@pytest.mark.anyio
@pytest.mark.parametrize("value", [1, True, None, ["high"], "High", "high ", "hi"])
async def test_a_value_of_the_wrong_type_or_spelling_is_not_in_the_list(
    tmp_path: Path, value: object
) -> None:
    layer = _layer(tmp_path, RULE)

    assert isinstance(await layer.before_call(_ctx(), _call(priority=value)), Deny)


@pytest.mark.anyio
async def test_a_one_of_that_lists_one_is_not_satisfied_by_true(tmp_path: Path) -> None:
    layer = _layer(
        tmp_path,
        RULE.replace('one_of = ["low", "normal", "high"]', "one_of = [1]").replace(
            'argument = "priority"', 'argument = "n"'
        ),
    )

    assert await layer.before_call(_ctx(), _call(n=1)) is ALLOW
    assert isinstance(await layer.before_call(_ctx(), _call(n=True)), Deny), "True is not 1 here"


@pytest.mark.anyio
async def test_a_wildcard_client_a_pattern_a_length_and_a_range(tmp_path: Path) -> None:
    layer = _layer(
        tmp_path,
        """
[[rule]]
name = "id"
client = "*"
tool = "tickets__assign"
argument = "assignee"
pattern = "[a-z][a-z0-9.]{1,30}"

[[rule]]
name = "note"
client = "*"
tool = "tickets__add_comment"
argument = "body"
max_length = 5

[[rule]]
name = "n"
client = "*"
tool = "tickets__list_tickets"
argument = "limit"
minimum = 1
maximum = 50
""",
    )

    async def allowed(tool: str, **arguments: Any) -> bool:
        return await layer.before_call(_ctx("anyone"), _call(tool, **arguments)) is ALLOW

    assert await allowed("tickets__assign", assignee="amara.k")
    assert not await allowed("tickets__assign", assignee="Amara")  # the whole value must match
    assert not await allowed("tickets__assign", assignee="amara.k\n")  # not just the start
    assert not await allowed("tickets__assign", assignee="a" * 5000)  # too long to match at all
    assert not await allowed("tickets__assign", assignee=12)
    assert await allowed("tickets__add_comment", body="hello")
    assert not await allowed("tickets__add_comment", body="hello!")
    assert await allowed("tickets__list_tickets", limit=50)
    assert not await allowed("tickets__list_tickets", limit=51)
    assert not await allowed("tickets__list_tickets", limit=0.5)
    assert not await allowed("tickets__list_tickets", limit=True)
    assert not await allowed("tickets__list_tickets", limit="5")


@pytest.mark.anyio
async def test_a_required_argument_that_is_missing_is_refused(tmp_path: Path) -> None:
    layer = _layer(
        tmp_path,
        RULE.replace('one_of = ["low", "normal", "high"]', "required = true"),
    )

    assert isinstance(await layer.before_call(_ctx(), _call(subject="x")), Deny)
    assert await layer.before_call(_ctx(), _call(priority="anything")) is ALLOW


@pytest.mark.anyio
async def test_every_rule_that_covers_a_call_must_pass(tmp_path: Path) -> None:
    layer = _layer(
        tmp_path,
        RULE
        + """
[[rule]]
name = "second"
client = "*"
tool = "tickets__create_ticket"
argument = "priority"
max_length = 3
""",
    )

    assert isinstance(await layer.before_call(_ctx(), _call(priority="high")), Deny)
    assert await layer.before_call(_ctx(), _call(priority="low")) is ALLOW


_HEAD = "[[rule]]\nname = 'a'\nclient = 'c'\ntool = 't'\nargument = 'x'\n"


@pytest.mark.parametrize(
    "text",
    [
        _HEAD,  # constrains nothing
        _HEAD + "one_of = []\n",
        _HEAD + "pattern = '('\n",
        _HEAD + "max_length = -1\n",
        _HEAD + "minimum = 'low'\n",
        _HEAD + "max_length = 1\nextra = 1\n",
        "[[rule]]\nclient = 'c'\ntool = 't'\nargument = 'x'\nmax_length = 1\n",  # no name
        _HEAD + "max_length = 1\n" + _HEAD.replace("'x'", "'y'") + "max_length = 1\n",  # same name
        "[other]\nx = 1\n",
        "not toml [",
    ],
)
def test_a_mistake_in_the_file_stops_startup(tmp_path: Path, text: str) -> None:
    path = tmp_path / "allowlist.toml"
    path.write_text(text)

    with pytest.raises(AllowlistError):
        load_allowlist(path)


def test_the_shipped_allowlist_loads_and_holds_the_urgent_ticket_rule() -> None:
    rules = load_allowlist(ROOT / "config" / "allowlist.toml")

    assert [rule.name for rule in rules] == ["support-bot-no-urgent-tickets"]
