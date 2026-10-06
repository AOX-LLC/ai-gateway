#!/usr/bin/env python3
"""Turn one Claude Code run's event stream into the transcript the clip plays.

    uv run demo/real-client/render.py <run.jsonl> <realistic|compliant> <out-prefix>

Writes <out-prefix>.json (what the terminal page plays: a header, then one line per call, result or
sentence of Claude's, with long runs of identical calls folded into one marked line) and
<out-prefix>.txt (the whole run, nothing folded, for the repository). It only reformats what the run
recorded; it adds no call and no result. The run's throwaway working directory, if a sentence names
it, is shown as <temp dir> and the transcript says so. It refuses a run that still carries anything
that must not be published: a gateway token, a home directory or a path. Harborline Supply Co. is
fictional and so is all of its data.
"""

import json
import re
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ai_gateway.admin.cli import DEMO_SCOPES_OPS

SERVER_PREFIX = "mcp__harborline__"
WIDTH = 96
KEEP_FIRST = 2
KEEP_LAST = 1
FOLD_ABOVE = KEEP_FIRST + KEEP_LAST + 1
ANSWER_LINES_SHOWN = 7
TEMP_DIR = re.compile(r"/tmp/tmp\.[A-Za-z0-9]+")  # noqa: S108 (matches a path in text; touches no file)
TEMP_DIR_SHOWN = "<temp dir>"
FORBIDDEN = re.compile(r"gw_[A-Za-z0-9]{6,}|/home/|/tmp/|\.demo/|Bearer ")
SUMMARY_KEYS = ("subject", "name")
LIST_KEYS = ("tickets", "accounts", "results", "documents")


@dataclass
class Step:
    """One tool call and what the gateway answered."""

    name: str
    arguments: dict[str, Any]
    answered: bool = False
    error: bool = False
    text: str = ""


def short_name(tool: str) -> str:
    return tool.removeprefix(SERVER_PREFIX)


def describe_arguments(arguments: dict[str, Any]) -> str:
    parts = []
    for key, value in arguments.items():
        shown = value if isinstance(value, (int, float, str)) else json.dumps(value)
        text = str(shown)
        parts.append(f"{key}={text if len(text) <= 28 else text[:25] + '...'}")
    return " ".join(parts)


def describe_result(step: Step) -> str:
    if step.error:
        return f"blocked: {step.text}" if is_blocked(step) else f"error: {step.text}"
    try:
        data = json.loads(step.text)
    except ValueError:
        return "answered"
    if not isinstance(data, dict):
        return "answered"
    for key in LIST_KEYS:
        if isinstance(data.get(key), list):
            return f"answered: {len(data[key])} {key}"
    for key in SUMMARY_KEYS:
        if isinstance(data.get(key), str):
            return f"answered: {data[key]}"
    return "answered"


def content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return " ".join(block.get("text", "") for block in content if isinstance(block, dict))


def read_events(path: Path) -> tuple[list[dict[str, Any]], int]:
    """The run's events, and how many times its temporary directory's path was replaced."""
    text, replaced = TEMP_DIR.subn(TEMP_DIR_SHOWN, path.read_text(encoding="utf-8"))
    return [json.loads(line) for line in text.splitlines() if line], replaced


def collect(events: list[dict[str, Any]]) -> tuple[dict[str, Any], list[Any]]:
    """The run's facts and its items in order: a Step per call, a string per sentence."""
    meta: dict[str, Any] = {}
    items: list[Any] = []
    by_id: dict[str, Step] = {}
    for event in events:
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            meta = {
                "version": event["claude_code_version"],
                "model": event["model"],
                "tools": sorted(short_name(tool) for tool in event["tools"]),
            }
        elif kind == "assistant":
            for block in event["message"]["content"]:
                if block["type"] == "text" and block["text"].strip():
                    items.append(block["text"].strip())
                elif block["type"] == "tool_use":
                    step = Step(short_name(block["name"]), block["input"])
                    by_id[block["id"]] = step
                    items.append(step)
        elif kind == "user":
            for block in event["message"]["content"]:
                if block.get("type") == "tool_result" and block["tool_use_id"] in by_id:
                    step = by_id[block["tool_use_id"]]
                    step.answered, step.error = True, bool(block.get("is_error"))
                    step.text = content_text(block["content"]).strip()
        elif kind == "result":
            meta["turns"], meta["cost_usd"] = event.get("num_turns"), event.get("total_cost_usd")
    return meta, items


def is_blocked(step: Step) -> bool:
    return step.error and "blocked" in step.text.lower()


def result_kind(step: Step) -> str:
    """How a result looks in the terminal: a gateway block stands out, other errors do not."""
    if is_blocked(step):
        return "blocked"
    return "error" if step.error else "result"


def step_lines(step: Step) -> list[dict[str, Any]]:
    return [
        {"kind": "call", "text": f"{step.name} {describe_arguments(step.arguments)}".rstrip()},
        {"kind": result_kind(step), "text": describe_result(step)},
    ]


def fold(items: list[Any]) -> list[dict[str, Any]]:
    """The lines to play: runs of one answered tool beyond a few are folded into one marked line."""
    lines: list[dict[str, Any]] = []
    index = 0
    while index < len(items):
        item = items[index]
        if not isinstance(item, Step):
            lines.append({"kind": "claude", "text": item})
            index += 1
            continue
        end = index
        while end < len(items) and isinstance(items[end], Step) and items[end].name == item.name:
            end += 1
        run = items[index:end]
        quiet = [i for i, step in enumerate(run) if not step.error]
        if len(run) >= FOLD_ABOVE and len(quiet) >= FOLD_ABOVE:
            hidden = list(quiet[KEEP_FIRST:-KEEP_LAST])
        else:
            hidden = []
        if hidden:
            for position, step in enumerate(run):
                if position == hidden[0]:
                    note = f"... {len(hidden)} more {item.name} calls, all answered"
                    lines.append({"kind": "folded", "text": note})
                if position not in hidden:
                    lines.extend(step_lines(step))
        else:
            for step in run:
                lines.extend(step_lines(step))
        index = end
    return lines


def wrap(text: str, indent: str = "") -> list[str]:
    out: list[str] = []
    for paragraph in text.splitlines():
        out.extend(
            textwrap.wrap(paragraph, WIDTH, initial_indent=indent, subsequent_indent=indent) or [""]
        )
    return out


def full_text(meta: dict[str, Any], prompt: str, kind: str, items: list[Any]) -> str:
    out = [header_text(meta, kind), "", "prompt:", *wrap(prompt, "  "), ""]
    for item in items:
        if isinstance(item, Step):
            call, result = step_lines(item)
            out += [f"-> {call['text']}", f"   <- {result['text']}"]
        else:
            out += ["", "Claude:", *wrap(item, "  "), ""]
    return "\n".join(out).rstrip() + "\n"


KIND_LABELS = {
    "realistic": "a realistic task: the prompt says nothing about exporting anything",
    "compliant": (
        "a compliant-model run: the prompt asks for the export, "
        "standing in for a model that was talked into it"
    ),
}


def header_text(meta: dict[str, Any], kind: str) -> str:
    return (
        f"Claude Code {meta['version']} · {meta['model']} · one MCP server, the gateway · "
        f"{len(meta['tools'])} tools offered · {KIND_LABELS[kind]}"
    )


def counts(items: list[Any]) -> dict[str, int]:
    steps = [item for item in items if isinstance(item, Step)]
    blocked = [step for step in steps if is_blocked(step)]
    return {"calls": len(steps), "blocked": len(blocked), "answered": len(steps) - len(blocked)}


def main() -> None:
    if len(sys.argv) != 4 or sys.argv[2] not in KIND_LABELS:
        sys.exit(__doc__)
    run, kind, prefix = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
    events, replaced = read_events(run)
    meta, items = collect(events)
    prompt = (Path(__file__).parent / "prompts" / f"{kind}.txt").read_text(encoding="utf-8").strip()
    final = next((item for item in reversed(items) if isinstance(item, str)), "")
    answer = wrap(final)
    document = {
        "kind": kind,
        "header": header_text(meta, kind),
        "label": KIND_LABELS[kind],
        "tools": meta["tools"],
        "withheld": [tool for tool in DEMO_SCOPES_OPS if tool not in meta["tools"]],
        "prompt": prompt,
        "lines": fold(items[: items.index(final)] if final in items else items),
        "answer": answer[:ANSWER_LINES_SHOWN],
        "answer_more": max(0, len(answer) - ANSWER_LINES_SHOWN),
        "counts": counts(items),
        "turns": meta.get("turns"),
    }
    text = full_text(meta, prompt, kind, items)
    if replaced:
        text += f"\n(The run's temporary working directory is shown as {TEMP_DIR_SHOWN}.)\n"
    for what in (json.dumps(document), text):
        found = FORBIDDEN.search(what)
        if found:
            sys.exit(f"render: the run carries something to withhold: {found.group(0)!r}")
    prefix.parent.mkdir(parents=True, exist_ok=True)
    prefix.with_suffix(".json").write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    prefix.with_suffix(".txt").write_text(text, encoding="utf-8")
    print(f"{kind}: {document['counts']}, {len(document['lines'])} lines to play")


if __name__ == "__main__":
    main()
