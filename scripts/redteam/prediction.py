"""What an attack file predicts for a column of the scorecard.

A column weakens some layers (turns them off, or puts them in monitor mode) and fixes how the lab
approver decides. The rule is applied to the file and never tuned after a run:

- a call the file says is *allowed* with every layer on stays allowed whatever is weakened (a
  weaker pipeline stops no more);
- a call it says is *blocked* stays blocked while at least one of its `catchers` still enforces, and
  is let through once all of them are weakened;
- a denying approver stops every write that got that far, and no read;
- the attack is predicted to succeed when at least one iteration of a landing step goes through.

It is deliberately the simple independent-layers model. Layers are not independent: an egress
attempt that is refused still counts, so one layer's refusal changes what another sees. Where a run
differs from the prediction the scorecard says so, and that difference is a finding, not an error.
"""

import tomllib
from collections.abc import Mapping
from pathlib import Path

from redteam.attack_format import LAYERS, Attack, Step

CONFIG = Path(__file__).resolve().parents[2] / "config"

FLOOR = frozenset({"scope", "approval"})
"""Layers a pipeline configuration cannot weaken without `allow_floor_override`."""
WEAKENABLE = LAYERS - FLOOR


def call_goes_through(
    step: Step,
    i: int,
    weakened: frozenset[str],
    effects: Mapping[str, str],
    approver: str = "allow",
) -> bool:
    """Whether iteration `i` of the step reaches the upstream in this column."""
    expected = step.expected_enforce(i)
    if expected.outcome == "blocked" and expected.catchers - weakened:
        return False  # a catcher still enforces
    return not (approver == "deny" and effects.get(step.tool, "write") == "write")


def predicted_success(
    attack: Attack,
    weakened: frozenset[str],
    effects: Mapping[str, str],
    approver: str = "allow",
) -> bool:
    """Whether the attack is predicted to succeed in the column. An honest run never does."""
    if not attack.hostile:
        return False
    landing = [step for step in attack.steps if step.id in attack.expect.landing]
    return any(
        call_goes_through(step, i, weakened, effects, approver)
        for step in landing
        for i in range(1, step.repeat + 1)
    )


def load_effects(*files: Path) -> dict[str, str]:
    """The effect (`read` or `write`) of each tool by its exposed name (`tickets__create_ticket`),
    from policy files in the format of `config/tool_policies.toml`; the product's own by default.
    A tool no file names is a write, as the gateway treats it."""
    effects: dict[str, str] = {}
    for path in files or (CONFIG / "tool_policies.toml",):
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        for namespace, tools in raw.items():
            if isinstance(tools, dict):
                for tool, table in tools.items():
                    if isinstance(table, dict) and "effect" in table:
                        effects[f"{namespace}__{tool}"] = str(table["effect"])
    return effects
