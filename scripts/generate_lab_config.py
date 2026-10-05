"""Write the scorecard's lab-only configuration (`config/lab/`), or check it is up to date.

    uv run scripts/generate_lab_config.py            # rewrite the files
    uv run scripts/generate_lab_config.py --check    # fail if a file is not what it would write

It is generated from the attack files and the product's own configuration, so a new attack, a new
allowlist rule or a changed classifier setting changes it, and a test fails until it is regenerated.
See scripts/redteam/lab_config.py. Harborline Supply Co. is fictional.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from redteam.lab_config import ROOT, generated_files


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="fail if a file is out of date")
    args = parser.parse_args()
    files = generated_files()
    if args.check:
        stale = [
            relative
            for relative, text in files.items()
            if not (ROOT / relative).is_file()
            or (ROOT / relative).read_text(encoding="utf-8") != text
        ]
        if stale:
            sys.exit(f"out of date: {stale}: run scripts/generate_lab_config.py")
        print(f"the {len(files)} lab files are up to date")
        return
    for relative, text in files.items():
        path = ROOT / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    print(f"wrote {len(files)} files under config/lab/")


if __name__ == "__main__":
    main()
