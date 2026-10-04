"""Write `config/tool_pins.toml`: the reviewed description and input schema of each Harborline tool.

    uv run scripts/generate_tool_pins.py            # rewrite the file
    uv run scripts/generate_tool_pins.py --check    # fail if the file is not what the servers say

The definitions come from the servers' own toolsets (the code the servers run, not a copy),
under the names the gateway exposes (<namespace>__<tool>). A change to a tool's description or
schema changes this file's diff, which is what a person reviews; a test runs the same check, so a
server that changes a description without re-pinning fails the build instead of hiding the tool in
production. Harborline Supply Co. is fictional.
"""

import argparse
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ai_gateway.pipeline.pins import render_tool_pins
from crm_server.tools import build_toolset as crm_toolset
from handbook_server.tools import build_toolset as handbook_toolset
from mcp_common.toolset import StrictToolset
from ticketing_server.tools import build_toolset as ticketing_toolset

PINS_FILE = Path(__file__).resolve().parents[1] / "config" / "tool_pins.toml"
NAMESPACES: dict[str, Callable[[Any], StrictToolset]] = {
    "tickets": ticketing_toolset,
    "crm": crm_toolset,
    "handbook": handbook_toolset,
}


def current_pins_text() -> str:
    """The pins file as the servers' own definitions would write it."""
    definitions = []
    for namespace, build in NAMESPACES.items():
        # The repositories are never called: only the tool definitions are read.
        for tool in build(None).tools():
            definitions.append((f"{namespace}__{tool.name}", tool.description, tool.input_schema))
    return render_tool_pins(definitions)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if the file is out of date")
    args = parser.parse_args()
    wanted = current_pins_text()
    if args.check:
        if not PINS_FILE.is_file() or PINS_FILE.read_text(encoding="utf-8") != wanted:
            sys.exit(
                f"{PINS_FILE} is not what the servers define: run scripts/generate_tool_pins.py"
            )
        print("tool pins are up to date")
        return
    PINS_FILE.write_text(wanted, encoding="utf-8")
    print(f"wrote {PINS_FILE}")


if __name__ == "__main__":
    main()
