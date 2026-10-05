"""Render the scorecard's files from the committed JSON, with no stack.

    uv run scripts/redteam/scorecard_render.py        # or: make scorecard-render

`docs/scorecard.md`, `docs/images/scorecard.svg` and the README's scorecard section are all
rendered from `docs/scorecard.json` alone, so a change to how they read (a sentence, the chart's
layout) needs no new run. The observed figures only change with `make scorecard`.
Harborline Supply Co. is fictional.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from redteam.scorecard import (
    render_markdown,
    render_readme_section,
    render_svg,
    replace_readme_section,
)

ROOT = Path(__file__).resolve().parents[2]


def render_all(docs: Path = ROOT / "docs", readme: Path = ROOT / "README.md") -> None:
    card = json.loads((docs / "scorecard.json").read_text(encoding="utf-8"))
    (docs / "scorecard.md").write_text(render_markdown(card), encoding="utf-8")
    (docs / "images").mkdir(exist_ok=True)
    (docs / "images" / "scorecard.svg").write_text(render_svg(card), encoding="utf-8")
    readme.write_text(
        replace_readme_section(readme.read_text(encoding="utf-8"), render_readme_section(card)),
        encoding="utf-8",
    )


def main() -> None:
    render_all()
    print("rendered docs/scorecard.md, docs/images/scorecard.svg and the README section")


if __name__ == "__main__":
    main()
