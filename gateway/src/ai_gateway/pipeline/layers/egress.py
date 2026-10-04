"""The egress layer: data a client read does not flow back out through a write.

Detection. After every read the layer notes, per *client*, which values the result carried: record
identifiers (ACC-00001), email addresses and phone numbers (the patterns are configuration), each
with the time it was last read. A value is remembered for a sliding window (`window_s`, 30 minutes
by default) and forgotten after it. A write is refused when its arguments carry too many distinct
values the client read within the window, in any of the client's MCP sessions: five in one write (a
bulk copy of customer records), ten in one session's writes, or ten across all the client's writes
in the window (the same copy dripped out a few at a time, in one session or across several). A
refused attempt still counts, so a session that has reached its tally of ten is quarantined: every
later write in it is refused, whatever it carries (a session that tried a bulk copy is suspect). A
write that carries an internal-only marker (`[INTERNAL-ONLY]`) is refused whatever the client read,
since a marker has no honest use outside the system. The record a tool acts on (a ticket id the call
is *about*) is exempt per tool, so an ops bot working through a list is not counted for naming each
ticket once.

Why per client. A ledger kept per MCP session is bypassed by reading in one session and writing in
another, and sessions are cheap. The client is what the gateway authenticates, so what it read is
what it may not write back out, whichever session it uses. The cost is that one client's honest
sessions share a tally: a client that cites more than ten read values in its writes within half an
hour (an ops bot's summaries) is refused the next one that carries any; raise `per_window` for it,
and watch the score in monitor mode first.

What the gateway keeps. Never arguments or results, and nothing on disk, in the database or in
telemetry. The ledger holds, in memory, a 64-bit keyed hash of each value (the key is random per
process and never leaves it, so the hashes mean nothing outside it) per client, with the time, and a
small tally per session. Every cap is per client, so one client's sessions can never push another's
ledger out: `max_values_per_client` values (2 000), `max_sessions_per_client` session tallies (100,
least recently used first; a session whose tally was dropped starts a fresh tally, but the client's
own ledger and window tally still apply). A client that has read more than it can be tracked for
(or a result too large to scan) cannot write a value until the window has passed: the layer cannot
check it, and forgetting would fail open. A restart forgets everything.

False positives. A legitimate write that cites more than a few records the client read (an ops
summary ticket). Monitor mode records the count of matching values as the verdict's `score`, so the
limits can be set from what honest traffic does before they are enforced.

What it does not catch: a transformation of the data (a summary, an encoding, another spelling of
a phone number or an id), a value the client never read through the gateway, data copied out after
the window has passed, an exfiltration through a read tool's arguments. The classifier on arguments
and the canary layer cover some of that; this layer is the cheap deterministic one.
"""

import hashlib
import hmac
import re
import secrets
import time
import tomllib
import unicodedata
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp.types import CallToolResult, TextContent

from ai_gateway.pipeline.layers.canary import decoded_runs
from ai_gateway.pipeline.types import (
    ALLOW,
    POLICY_BLOCK_MESSAGE,
    Allow,
    BaseLayer,
    CallContext,
    Deny,
    DenyCode,
    ToolCall,
    Verdict,
)

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,255}\.[A-Za-z]{2,24}")
_TOP_KEYS = frozenset({"limits", "memory", "values", "markers", "exempt"})
_MAX_TEXT = 262_144
_DASHES = dict.fromkeys(
    map(ord, "\u2010\u2011\u2012\u2013\u2014\u2015\u2212\ufe58\ufe63\uff0d"), "-"
)


def normalise(text: str) -> str:
    """The text as a value would be matched in it: compatibility forms folded (full-width letters,
    ligatures), format and zero-width characters dropped, every dash a plain hyphen, upper case. A
    value dressed up to slip past a pattern is the same value once it is plain."""
    folded = unicodedata.normalize("NFKC", text)
    kept = "".join(c for c in folded if unicodedata.category(c) != "Cf").translate(_DASHES)
    return kept.upper()


class EgressConfigError(ValueError):
    pass


@dataclass(frozen=True)
class EgressConfig:
    per_write: int = 5
    per_session: int = 10
    per_window: int = 10
    window_s: int = 1_800
    max_sessions_per_client: int = 100
    max_values_per_client: int = 2_000
    identifier_patterns: tuple[re.Pattern[str], ...] = ()
    emails: bool = True
    phone_pattern: re.Pattern[str] | None = None
    markers: tuple[str, ...] = ()
    exempt: Mapping[str, frozenset[str]] = field(default_factory=dict)


def load_egress_config(path: Path) -> EgressConfig:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise EgressConfigError(f"cannot read the egress configuration {path}: {error}") from error
    return parse_egress_config(raw)


def _positive(table: Mapping[str, Any], key: str, default: int) -> int:
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise EgressConfigError(f"{key} must be a whole number of at least 1")
    return value


def parse_egress_config(raw: Mapping[str, Any]) -> EgressConfig:
    unknown = set(raw) - _TOP_KEYS
    if unknown:
        raise EgressConfigError(f"unknown egress configuration keys: {sorted(unknown)}")
    limits, memory, values = (raw.get(k, {}) for k in ("limits", "memory", "values"))
    try:
        patterns = tuple(re.compile(p, re.ASCII) for p in values.get("identifier_patterns", []))
        phone = values.get("phone_pattern")
        phone_pattern = re.compile(phone, re.ASCII) if phone else None
    except (re.error, TypeError) as error:
        raise EgressConfigError(f"a value pattern is not a regular expression: {error}") from error
    markers = tuple(raw.get("markers", {}).get("internal", []))
    if not all(isinstance(m, str) and m for m in markers):
        raise EgressConfigError("markers.internal must be a list of non-empty strings")
    exempt_raw = raw.get("exempt", {})
    exempt = {
        tool: frozenset(arguments)
        for tool, arguments in exempt_raw.items()
        if isinstance(arguments, list) and all(isinstance(a, str) for a in arguments)
    }
    if len(exempt) != len(exempt_raw):
        raise EgressConfigError("[exempt] maps a tool name to a list of argument names")
    return EgressConfig(
        per_write=_positive(limits, "per_write", 5),
        per_session=_positive(limits, "per_session", 10),
        per_window=_positive(limits, "per_window", 10),
        window_s=_positive(limits, "window_s", 1_800),
        max_sessions_per_client=_positive(memory, "max_sessions_per_client", 100),
        max_values_per_client=_positive(memory, "max_values_per_client", 2_000),
        identifier_patterns=patterns,
        emails=bool(values.get("emails", True)),
        phone_pattern=phone_pattern,
        markers=markers,
        exempt=exempt,
    )


def _keys_and_values(value: Any) -> list[str]:
    """Every string in the value, dict keys included: a value can hide in a key."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for k, item in value.items() for s in [k, *_keys_and_values(item)]]
    if isinstance(value, list):
        return [s for item in value for s in _keys_and_values(item)]
    return []


def _result_text(result: CallToolResult) -> str:
    """The text of a result, whole: the caller notes when it is longer than can be scanned."""
    parts: list[str] = []
    if result.structured_content is not None:
        parts += _keys_and_values(result.structured_content)
    for block in result.content:
        if isinstance(block, TextContent):
            parts.append(block.text)
    return "\n".join(parts)


@dataclass
class _ClientLedger:
    """What one client read and tried to write out, within the window."""

    read: dict[int, float] = field(default_factory=dict)
    """Fingerprint of a value read, with when it was last read, oldest first."""
    egressed: dict[int, float] = field(default_factory=dict)
    """Fingerprints of values a write of the client carried that it had read (refused or not)."""
    sessions: OrderedDict[str, set[int]] = field(default_factory=OrderedDict)
    """Per MCP session, the fingerprints its writes carried: the quarantine tally."""
    saturated_at: float | None = None
    """When the client last read more than it can be tracked for (or a result too large to scan):
    until the window has passed, a write of it that carries any value cannot be checked and is
    refused."""


class EgressLayer(BaseLayer):
    name = "egress"

    def __init__(
        self, config: EgressConfig | None = None, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._config = config or EgressConfig()
        self._clock = clock
        self._key = secrets.token_bytes(32)
        self._clients: dict[str, _ClientLedger] = {}

    # -- what counts as a value -----------------------------------------------------------------

    def _values_in(self, text: str) -> set[str]:
        """The values in the text, in the clear or in a decoded base64 or hex run of it."""
        found: set[str] = set()
        for candidate in (text, *decoded_runs(text)):
            plain = normalise(candidate)
            for pattern in self._config.identifier_patterns:
                found.update(m.group(0) for m in pattern.finditer(plain))
            if self._config.emails:
                found.update(m.group(0).lower() for m in _EMAIL.finditer(plain))
            if self._config.phone_pattern is not None:
                found.update(m.group(0) for m in self._config.phone_pattern.finditer(plain))
        return found

    def _fingerprint(self, value: str) -> int:
        digest = hmac.new(self._key, value.encode(), hashlib.sha256).digest()
        return int.from_bytes(digest[:8], "big")

    # -- the ledger -----------------------------------------------------------------------------

    def _client(self, ctx: CallContext, now: float) -> _ClientLedger:
        """The client's ledger with everything older than the window forgotten."""
        ledger = self._clients.setdefault(str(ctx.client.id), _ClientLedger())
        horizon = now - self._config.window_s
        for table in (ledger.read, ledger.egressed):
            while table:
                oldest = next(iter(table))
                if table[oldest] > horizon:
                    break
                del table[oldest]
        if ledger.saturated_at is not None and ledger.saturated_at <= horizon:
            ledger.saturated_at = None
        return ledger

    def _session(self, ledger: _ClientLedger, ctx: CallContext) -> set[int]:
        """The session's tally; the client's least recently used sessions go first when it has
        more than `max_sessions_per_client` (never another client's)."""
        key = ctx.session_id or "-"
        tally = ledger.sessions.get(key)
        if tally is None:
            tally = ledger.sessions[key] = set()
        ledger.sessions.move_to_end(key)
        while len(ledger.sessions) > self._config.max_sessions_per_client:
            ledger.sessions.popitem(last=False)
        return tally

    def stored_values(self) -> int:
        """How many fingerprints are held now, for the memory measurement."""
        return sum(
            len(c.read) + len(c.egressed) + sum(len(t) for t in c.sessions.values())
            for c in self._clients.values()
        )

    # -- hooks ----------------------------------------------------------------------------------

    async def after_call(self, ctx: CallContext, call: ToolCall, result: CallToolResult) -> Verdict:
        if call.effect != "read":
            return ALLOW
        now = self._clock()
        text = _result_text(result)
        values = self._values_in(text[:_MAX_TEXT])
        ledger = self._client(ctx, now)
        if len(text) > _MAX_TEXT:
            ledger.saturated_at = now  # what lies past the bound is not recorded
        for value in values:
            fingerprint = self._fingerprint(value)
            if fingerprint in ledger.read:
                del ledger.read[fingerprint]  # read again: the window starts over, newest last
            elif len(ledger.read) >= self._config.max_values_per_client:
                ledger.saturated_at = now  # not recorded: writes cannot be checked for a while
                continue
            ledger.read[fingerprint] = now
        return Allow(score=len(values))

    async def before_call(self, ctx: CallContext, call: ToolCall) -> Verdict:
        if call.effect != "write":
            return ALLOW
        arguments = call.arguments
        text = "\n".join(_keys_and_values(arguments))
        if len(text) > _MAX_TEXT:
            # Not scanning the rest would let values hide behind padding: refuse what is unchecked.
            return Deny(DenyCode.EGRESS_STATE_LOST, POLICY_BLOCK_MESSAGE, score=1)
        normalised = unicodedata.normalize("NFKC", text).casefold()
        markers = sum(normalised.count(marker.casefold()) for marker in self._config.markers)
        if markers:
            return Deny(DenyCode.EGRESS_MARKER, POLICY_BLOCK_MESSAGE, score=markers)

        now = self._clock()
        exempt_values: set[str] = set()
        for argument in self._config.exempt.get(call.exposed_name, frozenset()):
            exempt_values |= self._values_in("\n".join(_keys_and_values(arguments.get(argument))))
        carried = self._values_in(text) - exempt_values
        ledger = self._client(ctx, now)
        if ledger.saturated_at is not None and carried:
            return Deny(DenyCode.EGRESS_STATE_LOST, POLICY_BLOCK_MESSAGE, score=len(carried))
        copied = {
            fingerprint
            for value in carried
            if (fingerprint := self._fingerprint(value)) in ledger.read
        }
        # Attempts count, refused or not: a write that was stopped still shows what was tried.
        tally = self._session(ledger, ctx)
        tally |= copied
        for fingerprint in copied:
            if fingerprint in ledger.egressed or len(ledger.egressed) < (
                self._config.max_values_per_client
            ):
                ledger.egressed.pop(fingerprint, None)
                ledger.egressed[fingerprint] = now
        # A session that has reached its tally is quarantined: every later write in it is refused,
        # even one that carries nothing it read. A session that tried a bulk copy is suspect, and a
        # write in the writer's own words, or in a spelling the patterns do not match, is how it
        # would carry on. The client's window tally stops the same copy dripped out across sessions:
        # a write that carries what the client read is refused once the client has tried ten.
        if (
            len(copied) >= self._config.per_write
            or len(tally) >= self._config.per_session
            or (copied and len(ledger.egressed) >= self._config.per_window)
        ):
            return Deny(
                DenyCode.EGRESS_BULK,
                POLICY_BLOCK_MESSAGE,
                score=max(len(copied), len(tally), len(ledger.egressed)),
            )
        return Allow(score=len(copied))
