"""The rate limit layer: how fast one client may call, per kind of call and per tool.

Each limit is a token bucket kept in memory, one per client and kind: it holds up to `burst`
tokens, refills `burst` tokens every `per` seconds, and each call takes one. A call must find a
token in every bucket that applies to it: the client's reads or writes bucket (by the tool's
reviewed effect), and the tool's own, if the tool has a limit.

    [reads]
    burst = 120
    per = 60                  # 120 reads a minute per client

    [writes]
    burst = 30
    per = 60

    [tools.crm__list_deals]   # an exposed tool name: its own bucket as well
    burst = 4
    per = 3600

The state is in memory: it starts full when the gateway starts, and each gateway process counts for
itself. The layer comes after scope and the allowlist, so a call that is refused for another reason
takes no token. In monitor mode a call that would be refused is let through and still counted.
"""

import time
import tomllib
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from math import ceil
from pathlib import Path
from typing import Any

from ai_gateway.pipeline.types import (
    ALLOW,
    BaseLayer,
    CallContext,
    Deny,
    DenyCode,
    ToolCall,
    Verdict,
)

_MAX_BUCKETS = 20_000
"""Buckets kept: clients and tools are both bounded by the registry, this bounds the memory anyway.
The least recently used goes first, and a client whose bucket went starts again full."""


class RateLimitError(ValueError):
    pass


@dataclass(frozen=True)
class Limit:
    burst: int
    per: float

    @property
    def refill_per_s(self) -> float:
        return self.burst / self.per


@dataclass(frozen=True)
class RateLimits:
    reads: Limit | None = None
    writes: Limit | None = None
    tools: Mapping[str, Limit] | None = None


def load_rate_limits(path: Path) -> RateLimits:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise RateLimitError(f"cannot read the rate limits {path}: {error}") from error
    unknown = set(raw) - {"reads", "writes", "tools"}
    if unknown:
        raise RateLimitError(f"unknown keys in {path}: {sorted(unknown)}")
    tools = raw.get("tools", {})
    if not isinstance(tools, dict):
        raise RateLimitError("[tools] must be a table of tables")
    return RateLimits(
        reads=_limit(raw.get("reads"), "reads"),
        writes=_limit(raw.get("writes"), "writes"),
        tools={name: limit for name, entry in tools.items() if (limit := _limit(entry, name))},
    )


def _limit(entry: Any, where: str) -> Limit | None:
    if entry is None:
        return None
    if not isinstance(entry, dict) or set(entry) != {"burst", "per"}:
        raise RateLimitError(f"{where}: needs exactly burst and per")
    burst, per = entry["burst"], entry["per"]
    if isinstance(burst, bool) or not isinstance(burst, int) or burst < 1:
        raise RateLimitError(f"{where}: burst must be a positive integer")
    if isinstance(per, bool) or not isinstance(per, int | float) or per <= 0:
        raise RateLimitError(f"{where}: per must be a positive number of seconds")
    return Limit(burst, float(per))


class _Bucket:
    __slots__ = ("stamp", "tokens")

    def __init__(self, tokens: float, stamp: float) -> None:
        self.tokens = tokens
        self.stamp = stamp


class RateLimitLayer(BaseLayer):
    name = "rate_limit"

    def __init__(
        self,
        limits: RateLimits | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limits = limits or RateLimits()
        self._clock = clock
        self._buckets: OrderedDict[tuple[str, str], _Bucket] = OrderedDict()

    def _applicable(self, call: ToolCall) -> list[tuple[str, Limit]]:
        kind = "writes" if call.effect == "write" else "reads"
        found: list[tuple[str, Limit]] = []
        kind_limit = self._limits.writes if kind == "writes" else self._limits.reads
        if kind_limit is not None:
            found.append((kind, kind_limit))
        tool_limit = (self._limits.tools or {}).get(call.exposed_name)
        if tool_limit is not None:
            found.append((f"tool:{call.exposed_name}", tool_limit))
        return found

    async def before_call(self, ctx: CallContext, call: ToolCall) -> Verdict:
        now = self._clock()
        wait_s = 0.0
        taken: list[_Bucket] = []
        for key, limit in self._applicable(call):
            bucket = self._bucket(ctx.client.actor_id, key, limit, now)
            if bucket.tokens >= 1.0:
                taken.append(bucket)
            else:
                wait_s = max(wait_s, (1.0 - bucket.tokens) / limit.refill_per_s)
        if wait_s:
            # Refused: no bucket is charged, so a call that cannot go through does not drain the
            # buckets that could have let others through.
            return Deny(
                DenyCode.RATE_LIMITED,
                f"Too many requests. Retry in {ceil(wait_s)} seconds.",
            )
        for bucket in taken:
            bucket.tokens -= 1.0
        return ALLOW

    def _bucket(self, client: str, key: str, limit: Limit, now: float) -> _Bucket:
        slot = (client, key)
        bucket = self._buckets.get(slot)
        if bucket is None:
            bucket = _Bucket(float(limit.burst), now)
            self._buckets[slot] = bucket
            if len(self._buckets) > _MAX_BUCKETS:
                self._buckets.popitem(last=False)
        else:
            elapsed = max(0.0, now - bucket.stamp)
            bucket.tokens = min(float(limit.burst), bucket.tokens + elapsed * limit.refill_per_s)
            bucket.stamp = now
            self._buckets.move_to_end(slot)
        return bucket
