"""Judging units of text through agent-core's ModelClient, and keeping the books.

One call per unit, concurrently up to a limit, each deduplicated through a bounded in-process cache
keyed by a keyed hash of the unit, so a text the gateway has judged is not judged again (which is
also what keeps live cost down). What happens to each unit:

- the model answers: a verdict, and a row in `model_usage` (tokens, cost, mode, never the text);
- replay mode has no recording for it: **unclassified**. It is never read as clean: the layer
  records it under its own verdict and code, and the scorecard counts it apart. Only replay mode can
  produce this (a live or record call never reads a file), so it is a property of the demo and test
  stack, not a hole in a live deployment;
- anything else (a provider error, a refusal, a budget, a timeout, an answer that does not fit the
  schema, a recording made under other prompt text): **failed**, and the layer fails closed.

Cost guards: a per-client call rate, each client's own share of an hourly spend ceiling for billed
calls and the gateway-wide ceiling itself (a replayed call is not spend), agent-core's own per-call
budget (its configuration), a timeout that also covers the wait for a model slot, and a limit on
units per call (in the layer).
"""

import logging
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

import anyio
from aox_agent_core import ModelClient, RunContext, Tier
from aox_agent_core.errors import ReplayMissError

from ai_gateway.classifier.prompt import CONFIDENCE_ORDER, JUDGE, Judgement, prepare
from ai_gateway.hashing import keyed_sha256

logger = logging.getLogger(__name__)

MAX_TOKENS = 64
_HOUR_S = 3600.0
_MINUTE_S = 60.0


class Outcome(StrEnum):
    CLEAN = "clean"
    INJECTION = "injection"
    UNCLASSIFIED = "unclassified"
    FAILED = "failed"


@dataclass(frozen=True)
class UnitResult:
    outcome: Outcome
    technique: str | None = None


@dataclass(frozen=True)
class UsageRecord:
    """One model call's books. No text, no verdict content: tokens, cost and where it was made."""

    request_id: UUID | None
    purpose: str
    model: str
    tier: str
    mode: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost_usd: Decimal
    latency_ms: float
    status: str
    """`ok`, `unrecorded` (replay found no recording) or `error`."""


UsageSink = Callable[[UsageRecord], None]


@dataclass(frozen=True)
class JudgeConfig:
    min_confidence: str = "medium"
    min_chars: int = 24
    min_words: int = 3
    max_unit_chars: int = 6_000
    short_text: str = "normalized"
    """`normalized` counts a value's length and words after underscores and zero-width characters
    are read as spaces (the value is still judged as written); `legacy` counts them as written.
    `legacy` is what v0.1.0 did, kept so the scorecard can show before and after."""
    unit_overlap_chars: int = 400
    """How far a unit of a long value reaches back into the one before it; 0 is v0.1.0's cut
    without overlap."""
    max_units: int = 40
    concurrency: int = 8
    timeout_s: float = 8.0
    cache_entries: int = 4_096
    max_calls_per_minute_per_client: int = 120
    max_usd_per_hour: Decimal = Decimal("2.00")
    max_usd_per_hour_per_client: Decimal = Decimal("0.50")
    judge_arguments: bool = True


@dataclass
class Judge:
    client: ModelClient
    config: JudgeConfig = field(default_factory=JudgeConfig)
    usage: UsageSink | None = None

    def __post_init__(self) -> None:
        self._cache: OrderedDict[str, UnitResult] = OrderedDict()
        self._calls: dict[str, deque[float]] = {}
        self._spend: deque[tuple[float, Decimal, str]] = deque()
        self._warned: dict[tuple[str, str], float] = {}
        self._limiter = anyio.CapacityLimiter(self.config.concurrency)

    # -- the guards -----------------------------------------------------------------------------

    def _allow_call(self, client_name: str) -> bool:
        """Whether this client may cause one more model call now. Three guards, cheapest first: the
        client's rate, the client's own share of the hour's spend, and the gateway-wide ceiling. A
        client that has spent its share is refused while the others still have the rest of the
        ceiling, so one client cannot use it all up. Only billed calls (live, record) are spend: a
        replayed call costs nothing."""
        now = time.monotonic()
        recent = self._calls.setdefault(client_name, deque())
        while recent and now - recent[0] > _MINUTE_S:
            recent.popleft()
        if len(recent) >= self.config.max_calls_per_minute_per_client:
            self._refused(client_name, "rate", now)
            return False
        while self._spend and now - self._spend[0][0] > _HOUR_S:
            self._spend.popleft()
        total = sum((cost for _, cost, _ in self._spend), Decimal(0))
        own = sum((cost for _, cost, who in self._spend if who == client_name), Decimal(0))
        if own >= self.config.max_usd_per_hour_per_client:
            self._refused(client_name, "client_budget", now)
            return False
        if total >= self.config.max_usd_per_hour:
            self._refused(client_name, "ceiling", now)
            return False
        recent.append(now)
        if len(self._calls) > 2_048:  # bounded: the least recently inserted client goes
            self._calls.pop(next(iter(self._calls)))
        return True

    def _refused(self, client_name: str, guard: str, now: float) -> None:
        """Say, once a minute per client and guard, that a guard refused a call: otherwise the only
        sign is `classifier_unavailable`. A client's name and a guard's name, never any text."""
        key = (client_name, guard)
        if now - self._warned.get(key, -_MINUTE_S) >= _MINUTE_S:
            self._warned[key] = now
            logger.error(
                "the injection classifier refused a model call for client %s: the %s guard",
                client_name,
                guard,
            )
            if len(self._warned) > 2_048:
                self._warned.pop(next(iter(self._warned)))

    # -- one unit -------------------------------------------------------------------------------

    async def judge(
        self, surface: str, text: str, *, client_name: str, request_id: UUID | None
    ) -> UnitResult:
        key = keyed_sha256(f"{surface}\x00{text}")
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            return cached
        if not self._allow_call(client_name):
            return UnitResult(Outcome.FAILED)  # a budget: not cached, tried again later
        result = await self._call(surface, text, request_id, client_name)
        if result.outcome is not Outcome.FAILED:
            self._cache[key] = result
            while len(self._cache) > self.config.cache_entries:
                self._cache.popitem(last=False)
        return result

    async def _call(
        self, surface: str, text: str, request_id: UUID | None, client_name: str
    ) -> UnitResult:
        started = time.perf_counter()
        context = RunContext(run_id=str(request_id)) if request_id is not None else None
        try:
            # The limit covers the wait for a slot as well as the call: one client's backlog cannot
            # hold another's calls longer than `timeout_s`.
            with anyio.fail_after(self.config.timeout_s):
                async with self._limiter:
                    answered = await self.client.call(
                        JUDGE,
                        inputs={"surface": surface, "text": prepare(text)},
                        output=Judgement,
                        tier=Tier.SMALL,
                        max_tokens=MAX_TOKENS,
                        context=context,
                    )
        except ReplayMissError:
            self._record(request_id, surface, None, started, "unrecorded", client_name)
            return UnitResult(Outcome.UNCLASSIFIED)
        except Exception as error:
            # Fail closed. The class name says what happened; the message might quote the text.
            logger.error(
                "the injection classifier could not judge a unit (%s)", type(error).__name__
            )
            self._record(request_id, surface, None, started, "error", client_name)
            return UnitResult(Outcome.FAILED)
        self._record(request_id, surface, answered, started, "ok", client_name)
        judged = answered.output
        flagged = (
            judged.verdict == "injection"
            and CONFIDENCE_ORDER[judged.confidence] >= CONFIDENCE_ORDER[self.config.min_confidence]
        )
        return UnitResult(
            Outcome.INJECTION if flagged else Outcome.CLEAN,
            judged.technique if flagged else None,
        )

    def _record(
        self,
        request_id: UUID | None,
        surface: str,
        answered: object,
        started: float,
        status: str,
        client_name: str,
    ) -> None:
        if self.usage is None:
            return
        elapsed_ms = (time.perf_counter() - started) * 1000
        if answered is None:
            record = UsageRecord(
                request_id, surface, "none", Tier.SMALL.value, _mode_of(self.client), 0, 0, 0, 0,
                Decimal(0), elapsed_ms, status,
            )  # fmt: skip
        else:
            usage = answered.usage  # type: ignore[attr-defined]
            if answered.mode.value != "replay":  # type: ignore[attr-defined]
                # Billed calls only: a replayed call's cost is the recording's, nothing was charged.
                self._spend.append(
                    (time.monotonic(), answered.cost_usd, client_name)  # type: ignore[attr-defined]
                )
            record = UsageRecord(
                request_id,
                surface,
                answered.model,  # type: ignore[attr-defined]
                answered.tier.value,  # type: ignore[attr-defined]
                answered.mode.value,  # type: ignore[attr-defined]
                usage.input_tokens,
                usage.output_tokens,
                usage.cache_read_input_tokens,
                usage.cache_creation_input_tokens,
                answered.cost_usd,  # type: ignore[attr-defined]
                answered.latency_ms,  # type: ignore[attr-defined]
                status,
            )
        try:
            self.usage(record)
        except Exception:
            logger.exception("could not record a model call's usage")


def _mode_of(client: object) -> str:
    config = getattr(client, "config", None)
    mode = getattr(config, "mode", None)
    return str(getattr(mode, "value", "replay"))
