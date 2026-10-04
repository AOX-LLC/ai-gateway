"""The rate limit layer: token buckets per client and kind of call, and per tool."""

from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from ai_gateway.pipeline.layers.rate_limit import (
    Limit,
    RateLimitError,
    RateLimitLayer,
    RateLimits,
    load_rate_limits,
)
from ai_gateway.pipeline.types import ALLOW, CallContext, ClientIdentity, Deny, DenyCode, ToolCall


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _ctx(name: str = "bot") -> CallContext:
    return CallContext(
        request_id=uuid4(),
        client=ClientIdentity(id=uuid4(), name=name, scopes=frozenset()),
        session_id=None,
        protocol_version="2025-11-25",
    )


def _call(tool: str = "tickets__get_ticket", effect: Any = "read") -> ToolCall:
    return ToolCall.create(tool, "tickets", tool.split("__")[1], {}, effect)


CTX = _ctx()


@pytest.mark.anyio
async def test_a_client_can_burst_then_must_wait_for_the_refill() -> None:
    clock = Clock()
    layer = RateLimitLayer(RateLimits(reads=Limit(burst=3, per=30.0)), clock)

    first = [await layer.before_call(CTX, _call()) for _ in range(3)]
    refused = await layer.before_call(CTX, _call())

    assert first == [ALLOW] * 3
    assert isinstance(refused, Deny)
    assert refused.code is DenyCode.RATE_LIMITED
    assert "Retry in 10 seconds" in refused.public_message  # 3 per 30 s: a token every 10 s
    clock.now += 10
    assert await layer.before_call(CTX, _call()) is ALLOW
    assert isinstance(await layer.before_call(CTX, _call()), Deny)
    clock.now += 1000
    assert [await layer.before_call(CTX, _call()) for _ in range(3)] == [ALLOW] * 3, (
        "capped at burst"
    )
    assert isinstance(await layer.before_call(CTX, _call()), Deny)


@pytest.mark.anyio
async def test_reads_and_writes_have_separate_buckets_and_clients_do_not_share() -> None:
    layer = RateLimitLayer(RateLimits(reads=Limit(1, 60.0), writes=Limit(2, 60.0)), Clock())
    other = _ctx("another")

    assert await layer.before_call(CTX, _call()) is ALLOW
    assert isinstance(await layer.before_call(CTX, _call()), Deny), "reads used up"
    assert await layer.before_call(CTX, _call("tickets__create_ticket", "write")) is ALLOW
    assert await layer.before_call(CTX, _call("tickets__assign", "write")) is ALLOW
    assert isinstance(await layer.before_call(CTX, _call("tickets__assign", "write")), Deny)
    assert await layer.before_call(other, _call()) is ALLOW, "another client has its own"


@pytest.mark.anyio
async def test_a_tool_can_have_a_limit_of_its_own_on_top_of_its_kind() -> None:
    layer = RateLimitLayer(
        RateLimits(reads=Limit(100, 60.0), tools={"crm__list_deals": Limit(2, 3600.0)}), Clock()
    )

    deals = [await layer.before_call(CTX, _call("crm__list_deals")) for _ in range(3)]

    assert deals[:2] == [ALLOW, ALLOW]
    assert isinstance(deals[2], Deny)
    assert await layer.before_call(CTX, _call("tickets__get_ticket")) is ALLOW, "other tools fine"


@pytest.mark.anyio
async def test_a_call_that_is_refused_takes_no_token_from_the_buckets_that_would_have_let_it() -> (
    None
):
    layer = RateLimitLayer(
        RateLimits(reads=Limit(5, 60.0), tools={"crm__list_deals": Limit(1, 3600.0)}), Clock()
    )
    assert await layer.before_call(CTX, _call("crm__list_deals")) is ALLOW  # reads: 4 left

    for _ in range(10):
        assert isinstance(await layer.before_call(CTX, _call("crm__list_deals")), Deny)

    reads = [await layer.before_call(CTX, _call("tickets__get_ticket")) for _ in range(4)]
    assert reads == [ALLOW] * 4, "the refusals did not drain the reads bucket"


@pytest.mark.anyio
async def test_with_no_limits_nothing_is_limited() -> None:
    layer = RateLimitLayer(None, Clock())

    assert all([await layer.before_call(CTX, _call()) is ALLOW for _ in range(1000)])


@pytest.mark.anyio
async def test_the_memory_is_bounded_when_many_clients_come_and_go(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_gateway.pipeline.layers import rate_limit

    monkeypatch.setattr(rate_limit, "_MAX_BUCKETS", 50)
    layer = RateLimitLayer(RateLimits(reads=Limit(1, 60.0)), Clock())

    for _ in range(500):
        await layer.before_call(_ctx(), _call())

    assert len(layer._buckets) <= 50


@pytest.mark.parametrize(
    "text",
    [
        "[reads]\nburst = 0\nper = 60\n",
        "[reads]\nburst = 5\n",
        "[reads]\nburst = 5\nper = 0\n",
        "[reads]\nburst = true\nper = 60\n",
        "[reads]\nburst = 5\nper = 60\nextra = 1\n",
        "[other]\nx = 1\n",
        "[tools]\nx = 1\n",
        "not toml [",
    ],
)
def test_a_mistake_in_the_file_stops_startup(tmp_path: Path, text: str) -> None:
    path = tmp_path / "limits.toml"
    path.write_text(text)

    with pytest.raises(RateLimitError):
        load_rate_limits(path)


def test_the_shipped_limits_load() -> None:
    limits = load_rate_limits(Path(__file__).resolve().parents[2] / "config" / "rate_limits.toml")

    assert limits.reads == Limit(600, 60.0)
    assert limits.writes == Limit(60, 60.0)
    assert limits.tools == {
        "crm__list_deals": Limit(4, 3600.0),
        "crm__get_account": Limit(30, 3600.0),
    }
