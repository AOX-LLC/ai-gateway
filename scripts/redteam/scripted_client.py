"""The deterministic scripted client: plays an attack file against the gateway, step by step.

It stands in for a model that has been talked into doing what a planted text says. It does not
reason and it does not give up: a refused call is recorded and the next step is made anyway, as a
compromised agent would. It uses one MCP session for the whole attack, reads nothing from the
environment, and given the same attack and the same data makes the same calls in the same order.
What it collects (customer values and a canary-shaped code from the results it was given) is held
in memory for the run and never printed. Harborline Supply Co. is fictional.
"""

import json
import re
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx2
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp.types import INVALID_PARAMS, CallToolResult, TextContent

from redteam.attack_format import TOKENS, Attack

# What an attacker copies out of a result: anything shaped like an email address or a phone number
# of the fictional data, and the shape of a canary code.
_VALUE = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,255}\.[A-Za-z]{2,24}|555-01[0-9]{2}")
_CANARY = re.compile(r"CNRY-HBL-[0-9A-F]{8}")


class AttackStoppedError(RuntimeError):
    """The attack could not make its next call (a template it could not fill)."""


@dataclass
class CallRecord:
    step: str
    iteration: int
    tool: str
    outcome: str  # "answered", "refused" (the gateway's refusal) or "error" (the tool's own)


@dataclass
class Loot:
    """What the attack has read so far."""

    values: list[str] = field(default_factory=list)
    next_value: int = 0
    canary: str | None = None

    def take(self, count: int) -> list[str]:
        taken = self.values[self.next_value : self.next_value + count]
        if len(taken) < count:
            raise AttackStoppedError(f"the attack read only {len(self.values)} customer values")
        self.next_value += count
        return taken

    def note(self, text: str) -> None:
        for value in _VALUE.findall(text):
            if value not in self.values:
                self.values.append(value)
        if self.canary is None and (found := _CANARY.search(text)):
            self.canary = found.group(0)


def render(template: str, *, attack: Attack, step_id: str, i: int, planted: str, loot: Loot) -> str:
    def fill(match: re.Match[str]) -> str:
        name, spec = match.group(1), match.group(2)
        if name == "i":
            return format(i, spec or "")
        if name == "planted":
            return planted
        if name == "marker":
            return attack.marker
        if name == "step":
            return step_id
        if name == "values":
            return " ".join(loot.take(int(spec or "0")))
        if loot.canary is None:  # name == "canary"
            raise AttackStoppedError("the attack never saw a canary")
        return loot.canary

    return str(TOKENS.sub(fill, template))


def _text(result: CallToolResult) -> str:
    parts = [block.text for block in result.content if isinstance(block, TextContent)]
    if result.structured_content is not None:
        parts.append(json.dumps(result.structured_content))
    return str("\n".join(parts))


@asynccontextmanager
async def connect(url: str, token: str) -> AsyncGenerator[Client]:
    async with (
        httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=60) as http,
        Client(streamable_http_client(url, http_client=http), mode="legacy") as client,
    ):
        yield client


async def play(attack: Attack, url: str, token: str, planted: str) -> list[CallRecord]:
    """Make every call of the attack, in order, in one session. Returns what happened to each."""
    loot = Loot()
    records: list[CallRecord] = []
    async with connect(url, token) as client:
        for step, i in attack.calls():
            arguments: dict[str, Any] = {
                key: render(value, attack=attack, step_id=step.id, i=i, planted=planted, loot=loot)
                for key, value in step.arguments.items()
            }
            try:
                result = await client.call_tool(step.tool, arguments)
            except MCPError as error:
                if error.code != INVALID_PARAMS:
                    raise
                outcome = "refused"
            else:
                outcome = "error" if result.is_error else "answered"
                if outcome == "answered":
                    loot.note(_text(result))
            records.append(CallRecord(step.id, i, step.tool, outcome))
    return records
