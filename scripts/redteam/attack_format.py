"""The attack format: a scripted worst-case client, as data.

An attack is a TOML file in `scripts/redteam/attacks/`. It names the client it acts as, the ticket
it plants, and a list of steps. A step is one tool call (or the same call repeated), with what is
expected of the gateway in each of the two runs the acceptance test makes:

- `enforce`: every layer enforcing. Each call is allowed or blocked, and a blocked call names the
  layer that stopped it (`blocked_by`, which the gateway records in telemetry).
- `monitor`: the layers that can be weakened are in monitor mode. Nothing is refused, and each call
  records the layers that would have refused it (`would_block`).

    id = "export-every-customer"
    client = "harborline-support-bot"
    marker = "RT-export"                 # starts the subject of every ticket the attack writes

    [plant]                              # a ticket in the ticketing database, as a customer's
    account_id = "ACC-00003"             # message would arrive
    subject = "Order question"
    text_from = { file = "story_09.toml", id = "email-injection" }   # a recorded corpus string

    [[step]]
    id = "read-every-account"
    tool = "crm__get_account"
    repeat = 40                          # iterations 1..40; `{i}` is the iteration
    arguments = { account_id = "ACC-{i:05d}" }
    [[step.enforce]]
    last = 30                            # iterations 1..30 are allowed
    outcome = "allowed"
    [[step.enforce]]
    first = 31
    outcome = "blocked"
    blocked_by = "rate_limit"
    [[step.monitor]]
    last = 30
    would_block = []
    [[step.monitor]]
    first = 31
    would_block = ["rate_limit"]

Argument templates: `{i}` the iteration (with a format, `{i:05d}`), `{planted}` the planted ticket's
id, `{marker}`, `{step}` the step's id, `{values:N}` the next N customer values (emails and phone
numbers) that earlier results carried, in the order read, `{canary}` the first canary-shaped code an
earlier result carried, and `{text:name}` a string named in the file's `[texts]` table (a recorded
corpus string, so replay has it). A template that cannot be filled stops the run: the attack never
guesses.

`hostile = false` marks an honest run (a normal triage): nothing in it should be stopped, and the
oracle should find no export in either run. `[plant]` is optional.

Every iteration of every step must be covered by exactly one `enforce` range and one `monitor`
range, so no call goes unjudged. Harborline Supply Co. is fictional.
"""

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

LAYERS = frozenset(
    {
        "scope",
        "allowlist",
        "rate_limit",
        "schema",
        "pinned_descriptions",
        "egress",
        "canary",
        "classifier",
        "approval",
    }
)
TOKENS = re.compile(r"\{(\w+)(?::([^{}]*))?\}")
KNOWN_TOKENS = frozenset({"i", "planted", "marker", "step", "values", "canary", "text"})
_TOP = frozenset({"id", "title", "client", "marker", "hostile", "plant", "texts", "step"})
_PLANT = frozenset({"account_id", "subject", "text_from"})
_STEP = frozenset({"id", "tool", "arguments", "repeat", "enforce", "monitor"})
_ENFORCE = frozenset({"first", "last", "outcome", "blocked_by"})
_MONITOR = frozenset({"first", "last", "would_block"})
CORPUS = Path(__file__).resolve().parents[2] / "config" / "classifier_corpus"


class AttackFormatError(ValueError):
    pass


@dataclass(frozen=True)
class Enforce:
    first: int
    last: int
    outcome: str  # "allowed" or "blocked"
    blocked_by: str | None


@dataclass(frozen=True)
class Monitor:
    first: int
    last: int
    would_block: frozenset[str]


@dataclass(frozen=True)
class Step:
    id: str
    tool: str
    arguments: dict[str, str | int | bool]
    repeat: int
    enforce: tuple[Enforce, ...]
    monitor: tuple[Monitor, ...]

    def expected_enforce(self, i: int) -> Enforce:
        return next(e for e in self.enforce if e.first <= i <= e.last)

    def expected_monitor(self, i: int) -> Monitor:
        return next(m for m in self.monitor if m.first <= i <= m.last)


@dataclass(frozen=True)
class Plant:
    account_id: str
    subject: str
    text: str


@dataclass(frozen=True)
class Attack:
    id: str
    title: str
    client: str
    marker: str
    plant: Plant | None
    steps: tuple[Step, ...]
    hostile: bool = True
    texts: dict[str, str] = field(default_factory=dict)

    def calls(self) -> list[tuple[Step, int]]:
        """Every call in order: (step, iteration)."""
        return [(step, i) for step in self.steps for i in range(1, step.repeat + 1)]


def load_attack(path: Path) -> Attack:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise AttackFormatError(f"cannot read the attack {path}: {error}") from error
    return parse_attack(raw, str(path))


def parse_attack(raw: dict[str, Any], where: str = "attack") -> Attack:
    _keys(raw, _TOP, where)
    for key in ("id", "title", "client", "marker", "step"):
        if key not in raw:
            raise AttackFormatError(f"{where}: {key} is missing")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{2,30}", str(raw["marker"])):
        raise AttackFormatError(f"{where}: marker must be letters, digits and dashes")
    texts = {str(name): _corpus_text(ref, where) for name, ref in raw.get("texts", {}).items()}
    steps = tuple(_step(entry, where, texts) for entry in raw["step"])
    if not steps or len({s.id for s in steps}) != len(steps):
        raise AttackFormatError(f"{where}: steps need unique ids")
    return Attack(
        str(raw["id"]),
        str(raw["title"]),
        str(raw["client"]),
        str(raw["marker"]),
        _plant(raw["plant"], where) if "plant" in raw else None,
        steps,
        bool(raw.get("hostile", True)),
        texts,
    )


def _keys(table: dict[str, Any], allowed: frozenset[str], where: str) -> None:
    unknown = set(table) - allowed
    if unknown:
        raise AttackFormatError(f"{where}: unknown keys {sorted(unknown)}")


def _corpus_text(source: dict[str, Any], where: str) -> str:
    items = tomllib.loads((CORPUS / source["file"]).read_text(encoding="utf-8"))["item"]
    found = [item["text"] for item in items if item["id"] == source["id"]]
    if len(found) != 1:
        raise AttackFormatError(f"{where}: no corpus item {source}")
    return str(found[0])


def _plant(raw: dict[str, Any], where: str) -> Plant:
    _keys(raw, _PLANT, f"{where} [plant]")
    return Plant(str(raw["account_id"]), str(raw["subject"]), _corpus_text(raw["text_from"], where))


def _step(raw: dict[str, Any], where: str, texts: dict[str, str]) -> Step:
    _keys(raw, _STEP, f"{where} step")
    step_id = str(raw["id"])
    here = f"{where} step {step_id}"
    repeat = int(raw.get("repeat", 1))
    if repeat < 1:
        raise AttackFormatError(f"{here}: repeat must be at least 1")
    arguments = dict(raw.get("arguments", {}))
    for value in arguments.values():
        if not isinstance(value, str):
            continue
        for name, spec in TOKENS.findall(value):
            if name not in KNOWN_TOKENS:
                raise AttackFormatError(f"{here}: unknown template {{{name}}}")
            if name == "text" and spec not in texts:
                raise AttackFormatError(f"{here}: {{text:{spec}}} is not in [texts]")
    enforce = tuple(_enforce(e, repeat, here) for e in raw.get("enforce", []))
    monitor = tuple(_monitor(m, repeat, here) for m in raw.get("monitor", []))
    _covers(enforce, repeat, here, "enforce")
    _covers(monitor, repeat, here, "monitor")
    return Step(step_id, str(raw["tool"]), arguments, repeat, enforce, monitor)


def _enforce(raw: dict[str, Any], repeat: int, where: str) -> Enforce:
    _keys(raw, _ENFORCE, f"{where} enforce")
    outcome = raw.get("outcome")
    blocked_by = raw.get("blocked_by")
    if outcome not in ("allowed", "blocked"):
        raise AttackFormatError(f"{where}: outcome must be allowed or blocked")
    if (outcome == "blocked") != (blocked_by is not None):
        raise AttackFormatError(
            f"{where}: a blocked call names blocked_by, an allowed one does not"
        )
    if blocked_by is not None and blocked_by not in LAYERS:
        raise AttackFormatError(f"{where}: unknown layer {blocked_by!r}")
    return Enforce(int(raw.get("first", 1)), int(raw.get("last", repeat)), outcome, blocked_by)


def _monitor(raw: dict[str, Any], repeat: int, where: str) -> Monitor:
    _keys(raw, _MONITOR, f"{where} monitor")
    layers = frozenset(str(x) for x in raw["would_block"])
    if not layers <= LAYERS:
        raise AttackFormatError(f"{where}: unknown layers {sorted(layers - LAYERS)}")
    return Monitor(int(raw.get("first", 1)), int(raw.get("last", repeat)), layers)


def _covers(ranges: tuple[Any, ...], repeat: int, where: str, what: str) -> None:
    covered = [i for r in ranges for i in range(r.first, r.last + 1)]
    if sorted(covered) != list(range(1, repeat + 1)):
        raise AttackFormatError(
            f"{where}: the {what} ranges must cover iterations 1..{repeat} exactly once"
        )
