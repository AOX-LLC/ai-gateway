"""The classifier layer: a small model judges the free text a call carries for injections.

What it judges. The results of reads (every prose value in them, one unit each) after the upstream
answers, and the arguments of writes (the same) before approval, so a person is never asked to
approve a write that carries an injection and a client never receives a result that does. A read's
arguments are not judged (they go nowhere). What a unit is, and what the model sees of it, is in
`classifier/prompt.py`; the calls, the cache, the cost guards and the books are in
`classifier/judge.py`.

Verdicts. A unit judged as an injection (at or above the configured confidence) refuses the call
(`classifier_injection`). A unit the model could not judge (a provider error, a timeout, a budget,
an answer that did not fit) refuses it too (`classifier_unavailable`): the layer fails closed. More
units than `max_units` is refused (`classifier_oversize`): not judging the rest would let an
injection hide behind padding. A unit that replay mode has no recording for is **unclassified**: the
call goes on, and the record says `unclassified`, with its own code, never `allow`: the scorecard
counts it apart. That can happen only in replay mode (the default, which needs no key), and every
text the repository's seeds, scenarios and attack corpus can produce is recorded.

False positives: ordinary business text that reads like an instruction ("please call the customer
back"). The model's confidence threshold is configuration; monitor mode records `would_block` with
the number of flagged units (`score`) so it can be set from what honest traffic does.
"""

import json
import tomllib
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import anyio
from mcp.types import CallToolResult, TextContent

from ai_gateway.classifier.judge import Judge, JudgeConfig, Outcome, UnitResult
from ai_gateway.classifier.prompt import SURFACE_ARGUMENTS, SURFACE_RESULT, units_of
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


class ClassifierConfigError(ValueError):
    pass


_KEYS = frozenset(
    {
        "min_confidence",
        "min_chars",
        "min_words",
        "max_unit_chars",
        "max_units",
        "concurrency",
        "timeout_s",
        "cache_entries",
        "max_calls_per_minute_per_client",
        "max_usd_per_hour",
        "judge_arguments",
    }
)


def load_judge_config(path: Path) -> JudgeConfig:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ClassifierConfigError(
            f"cannot read the classifier configuration {path}: {error}"
        ) from error
    return parse_judge_config(raw)


def parse_judge_config(raw: Mapping[str, Any]) -> JudgeConfig:
    unknown = set(raw) - {"classifier"}
    table = raw.get("classifier", {})
    if unknown or not isinstance(table, Mapping) or set(table) - _KEYS:
        extra = sorted(unknown | (set(table) - _KEYS))
        raise ClassifierConfigError(f"unknown keys in the classifier configuration: {extra}")
    defaults = JudgeConfig()
    try:
        config = JudgeConfig(
            min_confidence=table.get("min_confidence", defaults.min_confidence),
            min_chars=table.get("min_chars", defaults.min_chars),
            min_words=table.get("min_words", defaults.min_words),
            max_unit_chars=table.get("max_unit_chars", defaults.max_unit_chars),
            max_units=table.get("max_units", defaults.max_units),
            concurrency=table.get("concurrency", defaults.concurrency),
            timeout_s=float(table.get("timeout_s", defaults.timeout_s)),
            cache_entries=table.get("cache_entries", defaults.cache_entries),
            max_calls_per_minute_per_client=table.get(
                "max_calls_per_minute_per_client", defaults.max_calls_per_minute_per_client
            ),
            max_usd_per_hour=Decimal(str(table.get("max_usd_per_hour", defaults.max_usd_per_hour))),
            judge_arguments=table.get("judge_arguments", "writes") == "writes",
        )
    except (InvalidOperation, ValueError, TypeError) as error:
        raise ClassifierConfigError(f"a classifier setting is not a number: {error}") from error
    whole = (
        config.min_chars,
        config.min_words,
        config.max_unit_chars,
        config.max_units,
        config.concurrency,
        config.cache_entries,
        config.max_calls_per_minute_per_client,
    )
    if config.min_confidence not in ("low", "medium", "high"):
        raise ClassifierConfigError("min_confidence is low, medium or high")
    if not all(isinstance(v, int) and not isinstance(v, bool) and v >= 1 for v in whole):
        raise ClassifierConfigError(
            "the classifier's sizes and limits are whole numbers of 1 or more"
        )
    if config.timeout_s <= 0 or config.max_usd_per_hour <= 0:
        raise ClassifierConfigError("timeout_s and max_usd_per_hour are above zero")
    if table.get("judge_arguments", "writes") not in ("writes", "none"):
        raise ClassifierConfigError('judge_arguments is "writes" or "none"')
    return config


def result_values(result: CallToolResult) -> Any:
    """What of a result is judged: its structured content, or else its text (parsed as JSON when it
    is, so each value is its own unit)."""
    if result.structured_content is not None:
        return result.structured_content
    texts = [block.text for block in result.content if isinstance(block, TextContent)]
    values: list[Any] = []
    for text in texts:
        try:
            values.append(json.loads(text))
        except ValueError:
            values.append(text)
    return values


class ClassifierLayer(BaseLayer):
    name = "classifier"

    def __init__(self, judge: Judge | None = None) -> None:
        self._judge = judge

    async def _verdict(self, ctx: CallContext, surface: str, value: Any) -> Verdict:
        judge = self._judge
        if judge is None:
            return Deny(DenyCode.CLASSIFIER_UNAVAILABLE, POLICY_BLOCK_MESSAGE, score=1)
        config = judge.config
        units = units_of(
            value,
            min_chars=config.min_chars,
            min_words=config.min_words,
            max_chars=config.max_unit_chars,
        )
        if len(units) > config.max_units:
            return Deny(DenyCode.CLASSIFIER_OVERSIZE, POLICY_BLOCK_MESSAGE, score=len(units))
        results: list[UnitResult | None] = [None] * len(units)

        async def one(index: int, unit: str) -> None:
            results[index] = await judge.judge(
                surface, unit, client_name=ctx.client.name, request_id=ctx.request_id
            )

        async with anyio.create_task_group() as tasks:
            for index, unit in enumerate(units):
                tasks.start_soon(one, index, unit)
        judged = [r for r in results if r is not None]
        flagged = sum(r.outcome is Outcome.INJECTION for r in judged)
        if any(r.outcome is Outcome.FAILED for r in judged):
            return Deny(DenyCode.CLASSIFIER_UNAVAILABLE, POLICY_BLOCK_MESSAGE, score=1)
        if flagged:
            return Deny(DenyCode.CLASSIFIER_INJECTION, POLICY_BLOCK_MESSAGE, score=flagged)
        if any(r.outcome is Outcome.UNCLASSIFIED for r in judged):
            return Allow(
                score=sum(r.outcome is Outcome.UNCLASSIFIED for r in judged), unclassified=True
            )
        return Allow(score=0) if units else ALLOW

    async def before_call(self, ctx: CallContext, call: ToolCall) -> Verdict:
        if call.effect != "write" or (
            self._judge is not None and not self._judge.config.judge_arguments
        ):
            return ALLOW
        return await self._verdict(ctx, SURFACE_ARGUMENTS, call.arguments)

    async def after_call(self, ctx: CallContext, call: ToolCall, result: CallToolResult) -> Verdict:
        if call.effect != "read":
            return ALLOW
        return await self._verdict(ctx, SURFACE_RESULT, result_values(result))
