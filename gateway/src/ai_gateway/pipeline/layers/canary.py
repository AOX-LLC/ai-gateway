"""The canary layer: a seeded decoy value in a call's arguments is blocked and alerted.

Canaries are made-up values placed in the (fictional) data where only someone reading it all would
find them: a note on an account, a comment on a ticket. They mean nothing to anyone, so one that
appears in a call's *arguments* was copied out of a result by something that was not asked to, which
is the footprint of an exfiltration however it was provoked. The layer needs no model and no read
history: it looks at the arguments of every call, reads as well as writes.

Detection. The arguments' text is normalised (NFKC, format and zero-width characters dropped,
upper-cased, everything but letters and digits squeezed out, so separators and spacing do not hide a
value), the canary's shape is searched for, and each match is looked up by SHA-256 in
`config/canaries.toml`, which holds hashes and names, never the values. Base64 and hex runs are
decoded (a bounded number of them) and searched the same way. What this does not catch: a
transformation that destroys the value (a translation, a hash, a paraphrase).

False positives are near zero: the values mean nothing to a person. The one honest source is a
model quoting a note verbatim into a comment; the canaries sit where nobody has a reason to quote
them. The canaries in the shipped data are public (they are in the repository): a real deployment
makes its own per stack and keeps the list secret.

Monitor mode records `would_block` and the number of canaries found (`score`) and the call goes on.
Either way an alert names the canary (by its name in the file), the tool and the client.
"""

import base64
import binascii
import hashlib
import re
import tomllib
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ai_gateway.pipeline.alerts import Alerts
from ai_gateway.pipeline.types import (
    ALLOW,
    BaseLayer,
    CallContext,
    Deny,
    DenyCode,
    ToolCall,
    Verdict,
)

POLICY_BLOCK_MESSAGE = "Request blocked by gateway policy."
_B64 = re.compile(r"[A-Za-z0-9+/_-]{16,512}={0,2}")
_HEX = re.compile(r"(?:[0-9A-Fa-f]{2}){8,256}")
_MAX_DECODED_RUNS = 20


class CanaryConfigError(ValueError):
    pass


@dataclass(frozen=True)
class CanaryConfig:
    shape: re.Pattern[str]
    """What a canary looks like once squeezed to upper-case letters and digits."""
    by_sha256: Mapping[str, str]
    """SHA-256 of the squeezed value to the canary's name."""


def squeeze(text: str) -> str:
    """The text with everything that could hide or split a value taken out of it."""
    kept = (c for c in unicodedata.normalize("NFKC", text) if unicodedata.category(c) != "Cf")
    return re.sub(r"[^A-Z0-9]", "", "".join(kept).upper())


def canary_sha256(value: str) -> str:
    """The hash `config/canaries.toml` records for a canary value."""
    return hashlib.sha256(squeeze(value).encode()).hexdigest()


def load_canary_config(path: Path) -> CanaryConfig:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise CanaryConfigError(f"cannot read the canaries {path}: {error}") from error
    unknown = set(raw) - {"shape", "canaries"}
    if unknown:
        raise CanaryConfigError(f"unknown keys in {path}: {sorted(unknown)}")
    try:
        shape = re.compile(str(raw["shape"]), re.ASCII)
    except (KeyError, re.error) as error:
        raise CanaryConfigError(f"{path} needs a `shape` that is a regular expression") from error
    table = raw.get("canaries", {})
    if not isinstance(table, dict) or not all(
        isinstance(name, str) and isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest)
        for name, digest in table.items()
    ):
        raise CanaryConfigError(f"[canaries] in {path} maps a name to the SHA-256 of a value")
    return CanaryConfig(shape=shape, by_sha256={digest: name for name, digest in table.items()})


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for k, item in value.items() for s in [k, *_strings(item)]]
    if isinstance(value, list):
        return [s for item in value for s in _strings(item)]
    return []


def _decoded_runs(text: str) -> list[str]:
    runs: list[str] = []
    for match in _B64.finditer(text):
        token = match.group(0).replace("-", "+").replace("_", "/")
        try:
            runs.append(
                base64.b64decode(token + "=" * (-len(token) % 4), validate=True).decode("latin-1")
            )
        except (binascii.Error, ValueError):
            continue
        if len(runs) >= _MAX_DECODED_RUNS:
            return runs
    for match in _HEX.finditer(text):
        runs.append(bytes.fromhex(match.group(0)).decode("latin-1"))
        if len(runs) >= _MAX_DECODED_RUNS:
            break
    return runs


class CanaryLayer(BaseLayer):
    name = "canary"

    def __init__(self, config: CanaryConfig | None = None, alerts: Alerts | None = None) -> None:
        self._config = config
        self._alerts = alerts or Alerts()

    def _found(self, text: str) -> set[str]:
        """The names of the canaries in the text, in the clear or in a decoded run of it."""
        config = self._config
        if config is None:
            return set()
        names: set[str] = set()
        for candidate in (text, *_decoded_runs(text)):
            for match in config.shape.finditer(squeeze(candidate)):
                digest = hashlib.sha256(match.group(0).encode()).hexdigest()
                if digest in config.by_sha256:
                    names.add(config.by_sha256[digest])
        return names

    async def before_call(self, ctx: CallContext, call: ToolCall) -> Verdict:
        if self._config is None:
            return ALLOW
        found = self._found("\n".join(_strings(call.arguments)))
        if not found:
            return ALLOW
        for name in sorted(found):
            self._alerts.raise_alert(
                "canary_hit",
                tool=call.exposed_name,
                client=ctx.client.name,
                detail={"canary": name},
            )
        return Deny(DenyCode.CANARY_HIT, POLICY_BLOCK_MESSAGE, score=len(found))
