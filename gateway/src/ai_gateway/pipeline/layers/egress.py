"""The egress layer: data read in a session does not flow back out through a write.

Detection. After every read the layer notes, per MCP session, which *values* the result carried:
record identifiers (ACC-00001), email addresses and phone numbers (the patterns are configuration).
A write is refused when its arguments carry too many distinct values the same session read: five in
one write (a bulk copy of customer records), or ten across the session's writes (the same copy
dripped out a few at a time). A write that carries an internal-only marker (`[INTERNAL-ONLY]`) is
refused whatever the session read, since a marker has no honest use outside the system. The record a
tool acts on (a ticket id the call is *about*) is exempt per tool, so an ops bot working through a
list is not counted for naming each ticket once.

What the gateway keeps. Never arguments or results, and nothing on disk, in the database or in
telemetry. The ledger holds, in memory, a 64-bit keyed hash of each value (the key is random per
process and never leaves it, so the hashes mean nothing outside it) in a set per session, bounded:
2 000 values a session, 200 000 in all, 1 000 sessions, least recently used sessions first. A
restart forgets everything, and a session past its cap stops noting values (the score then reads
low, which is the safe error for monitoring and the unsafe one for enforcing: the caps are large
against what a session of this system reads).

False positives. A legitimate write that cites more than a few records the session read (an ops
summary ticket). Monitor mode records the count of matching values as the verdict's `score`, so the
limits can be set from what honest traffic does before they are enforced.

What it does not catch: a transformation of the data (a summary, an encoding), a value the session
never read, an exfiltration through a read tool's arguments. The classifier on arguments and the
canary layer cover some of that; this layer is the cheap deterministic one.
"""

import hashlib
import hmac
import re
import secrets
import tomllib
import unicodedata
from collections import OrderedDict
from collections.abc import Mapping
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
_MAX_EVICTED_REMEMBERED = 4_096


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
    max_sessions: int = 1_000
    max_values_per_session: int = 2_000
    max_values_total: int = 200_000
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
        max_sessions=_positive(memory, "max_sessions", 1_000),
        max_values_per_session=_positive(memory, "max_values_per_session", 2_000),
        max_values_total=_positive(memory, "max_values_total", 200_000),
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
    parts: list[str] = []
    if result.structured_content is not None:
        parts += _keys_and_values(result.structured_content)
    for block in result.content:
        if isinstance(block, TextContent):
            parts.append(block.text)
    return "\n".join(parts)[:_MAX_TEXT]


@dataclass
class _SessionLedger:
    read: set[int] = field(default_factory=set)
    egressed: set[int] = field(default_factory=set)
    saturated: bool = False
    """The session read more values than it may be tracked for: what it read afterwards is not
    recorded, so a write from it that carries any value cannot be checked and is refused."""


class EgressLayer(BaseLayer):
    name = "egress"

    def __init__(self, config: EgressConfig | None = None) -> None:
        self._config = config or EgressConfig()
        self._key = secrets.token_bytes(32)
        self._ledgers: OrderedDict[tuple[str, str], _SessionLedger] = OrderedDict()
        self._evicted: OrderedDict[tuple[str, str], None] = OrderedDict()
        """Sessions whose ledger was dropped for room (keys only): a write from one is refused,
        because what it read can no longer be compared, and forgetting would fail open."""
        self._total = 0

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

    def _ledger(self, ctx: CallContext) -> _SessionLedger:
        key = (str(ctx.client.id), ctx.session_id or "-")
        ledger = self._ledgers.get(key)
        if ledger is None:
            ledger = self._ledgers[key] = _SessionLedger()
        self._ledgers.move_to_end(key)
        while len(self._ledgers) > self._config.max_sessions or (
            self._total > self._config.max_values_total and len(self._ledgers) > 1
        ):
            gone, evicted = self._ledgers.popitem(last=False)
            self._total -= len(evicted.read) + len(evicted.egressed)
            self._evicted[gone] = None
            while len(self._evicted) > _MAX_EVICTED_REMEMBERED:
                self._evicted.popitem(last=False)
        return ledger

    def stored_values(self) -> int:
        """How many fingerprints are held now, for the memory measurement."""
        return self._total

    # -- hooks ----------------------------------------------------------------------------------

    async def after_call(self, ctx: CallContext, call: ToolCall, result: CallToolResult) -> Verdict:
        if call.effect != "read":
            return ALLOW
        values = self._values_in(_result_text(result))
        ledger = self._ledger(ctx)
        room = self._config.max_values_per_session - len(ledger.read)
        if len(values) > max(room, 0):
            ledger.saturated = True
        for value in list(values)[: max(room, 0)]:
            fingerprint = self._fingerprint(value)
            if fingerprint not in ledger.read:
                ledger.read.add(fingerprint)
                self._total += 1
        return Allow(score=len(values))

    async def before_call(self, ctx: CallContext, call: ToolCall) -> Verdict:
        if call.effect != "write":
            return ALLOW
        arguments = call.arguments
        text = "\n".join(_keys_and_values(arguments))[:_MAX_TEXT]
        normalised = unicodedata.normalize("NFKC", text).casefold()
        markers = sum(normalised.count(marker.casefold()) for marker in self._config.markers)
        if markers:
            return Deny(DenyCode.EGRESS_MARKER, POLICY_BLOCK_MESSAGE, score=markers)

        key = (str(ctx.client.id), ctx.session_id or "-")
        if key in self._evicted:
            return Deny(DenyCode.EGRESS_STATE_LOST, POLICY_BLOCK_MESSAGE, score=1)
        exempt_values: set[str] = set()
        for argument in self._config.exempt.get(call.exposed_name, frozenset()):
            exempt_values |= self._values_in("\n".join(_keys_and_values(arguments.get(argument))))
        carried = self._values_in(text) - exempt_values
        ledger = self._ledger(ctx)
        if ledger.saturated and carried:
            return Deny(DenyCode.EGRESS_STATE_LOST, POLICY_BLOCK_MESSAGE, score=len(carried))
        copied = {
            fingerprint
            for value in carried
            if (fingerprint := self._fingerprint(value)) in ledger.read
        }
        # Attempts count, refused or not: a write that was stopped still shows what was tried.
        new = copied - ledger.egressed
        room = self._config.max_values_per_session - len(ledger.egressed)
        for fingerprint in list(new)[: max(room, 0)]:
            ledger.egressed.add(fingerprint)
            self._total += 1
        if (
            len(copied) >= self._config.per_write
            or len(ledger.egressed) >= self._config.per_session
        ):
            return Deny(
                DenyCode.EGRESS_BULK,
                POLICY_BLOCK_MESSAGE,
                score=max(len(copied), len(ledger.egressed)),
            )
        return Allow(score=len(copied))
