"""A stand-in for agent-core's ModelClient, for the classifier's tests."""

from collections.abc import Callable
from decimal import Decimal
from typing import Any

from aox_agent_core import CallResult, Tier, Usage
from aox_agent_core.config import Mode, Provider

from ai_gateway.classifier.prompt import Judgement

ANSWERS = {
    "injection": Judgement(verdict="injection", confidence="high", technique="override"),
    "clean": Judgement(verdict="clean", confidence="high", technique="none"),
    "unsure": Judgement(verdict="injection", confidence="low", technique="other"),
}


class FakeClient:
    """Answers by what the text says: "ignore your" in it is an injection, `unsure` a low-confidence
    one, anything else clean. Counts its calls; can be told to raise."""

    def __init__(
        self, mode: Mode = Mode.REPLAY, raises: Callable[[str], Exception | None] | None = None
    ) -> None:
        self.calls: list[str] = []
        self.surfaces: list[str] = []
        self.mode = mode
        self._raises = raises

    @property
    def config(self) -> Any:
        return type("Config", (), {"mode": self.mode})()

    async def call(
        self, prompt: Any, *, inputs: Any = None, output: Any = None, **options: Any
    ) -> CallResult[Any]:
        text = str(inputs["text"])
        self.calls.append(text)
        self.surfaces.append(str(inputs["surface"]))
        if self._raises is not None and (error := self._raises(text)) is not None:
            raise error
        key = "injection" if "ignore your" in text else "unsure" if "unsure" in text else "clean"
        return CallResult(
            output=ANSWERS[key],
            tier=Tier.SMALL,
            task=None,
            provider=Provider.ANTHROPIC,
            model="fake-small",
            mode=self.mode,
            usage=Usage(input_tokens=300, output_tokens=20),
            cost_usd=Decimal("0.0004"),
            latency_ms=12.5,
            stop_reason="end_turn",
        )
