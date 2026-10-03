"""A throttle on failed logins: per token lookup id, and a ceiling over all of them.

Guessing a token's secret means many tries against one lookup id. After `failures_per_id` failures
for one id within `window_s`, further attempts naming that id are refused outright, without looking
at the secret, for `lockout_s`. The refusal covers the right secret too: an attacker who knows a
client's lookup id (it is printed once, when the token is issued, and is not in any record a client
can read) can lock that client out for a minute at a time. That is the price of not answering a
guess, and the lookup id is not guessable (8 characters of base32).

Guessing lookup ids instead (a spray of unknown ids, each failing once) never trips the per-id
limit, so there is a ceiling too: when `global_ceiling` failures of any kind have happened within
`window_s`, only a lookup id that authenticated successfully in the last `known_good_ttl_s` is
served; everything else gets the same refusal until the failures age out of the window. Clients
that were working keep working while an attack is on.

All state is in memory, per gateway process, and bounded: an attacker's invented ids push out the
oldest entries, never the memory. It starts empty when the gateway starts.
"""

import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass

_MAX_TRACKED_IDS = 10_000
_MAX_KNOWN_GOOD = 1_024


@dataclass(frozen=True)
class ThrottleConfig:
    failures_per_id: int = 5
    window_s: float = 60.0
    lockout_s: float = 60.0
    global_ceiling: int = 200
    known_good_ttl_s: float = 900.0


class LoginThrottle:
    def __init__(
        self,
        config: ThrottleConfig | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config or ThrottleConfig()
        self._clock = clock
        self._failures: OrderedDict[str, deque[float]] = OrderedDict()
        self._locked_until: dict[str, float] = {}
        self._all_failures: deque[float] = deque()
        self._known_good: OrderedDict[str, float] = OrderedDict()

    def check(self, lookup_id: str | None) -> float | None:
        """Seconds the caller must wait, or None to go on and verify the token."""
        now = self._clock()
        if lookup_id is not None:
            until = self._locked_until.get(lookup_id)
            if until is not None:
                if until > now:
                    return until - now
                del self._locked_until[lookup_id]
        self._trim_all(now)
        if len(self._all_failures) >= self._config.global_ceiling and not self._is_known_good(
            lookup_id, now
        ):
            return max(0.0, self._all_failures[0] + self._config.window_s - now)
        return None

    def record_failure(self, lookup_id: str | None, *, counts_toward_ceiling: bool = True) -> None:
        """`counts_toward_ceiling` is False for a request that named no token at all: it costs
        nothing to refuse, MCP clients probe without one first, and a flood of them must not shut
        the gateway to the clients that have logged in."""
        now = self._clock()
        if counts_toward_ceiling:
            self._all_failures.append(now)
        self._trim_all(now)
        if lookup_id is None:
            return  # nothing to count it against: only the ceiling sees it
        recent = self._failures.get(lookup_id)
        if recent is None:
            recent = self._failures[lookup_id] = deque()
            if len(self._failures) > _MAX_TRACKED_IDS:
                evicted, _ = self._failures.popitem(last=False)
                self._locked_until.pop(evicted, None)
        else:
            self._failures.move_to_end(lookup_id)
        recent.append(now)
        while recent and recent[0] <= now - self._config.window_s:
            recent.popleft()
        if len(recent) >= self._config.failures_per_id:
            self._locked_until[lookup_id] = now + self._config.lockout_s
            recent.clear()

    def record_success(self, lookup_id: str) -> None:
        self._known_good[lookup_id] = self._clock()
        self._known_good.move_to_end(lookup_id)
        if len(self._known_good) > _MAX_KNOWN_GOOD:
            self._known_good.popitem(last=False)

    def _is_known_good(self, lookup_id: str | None, now: float) -> bool:
        if lookup_id is None:
            return False
        seen = self._known_good.get(lookup_id)
        return seen is not None and now - seen <= self._config.known_good_ttl_s

    def _trim_all(self, now: float) -> None:
        while self._all_failures and self._all_failures[0] <= now - self._config.window_s:
            self._all_failures.popleft()
