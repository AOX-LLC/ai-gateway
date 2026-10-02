"""Write .env from .env.example with a fresh random secret for every placeholder.

    python3 scripts/init_env.py            # refuses to overwrite an existing .env
    python3 scripts/init_env.py --force    # overwrites it (existing database roles keep
                                           # their old passwords; see the README)

Every value that starts with `change-me` becomes a new `secrets.token_urlsafe(32)`. A
placeholder that appears inside another value, such as the password in a database URL, is
replaced there too, so the URLs stay consistent with the passwords they carry. The file is
written readable by its owner only, and no secret is printed.
"""

import argparse
import os
import secrets
import sys
from collections.abc import Sequence
from pathlib import Path

PLACEHOLDER_PREFIX = "change-me"


def render(example: str) -> str:
    """The text of .env: `example` with each placeholder replaced by its own fresh secret."""
    secrets_by_placeholder: dict[str, str] = {}
    for line in example.splitlines():
        value = _assigned_value(line)
        if value is not None and value.startswith(PLACEHOLDER_PREFIX):
            secrets_by_placeholder.setdefault(value, secrets.token_urlsafe(32))

    text = example
    # Longest first, so a placeholder that contains another is replaced whole.
    for placeholder in sorted(secrets_by_placeholder, key=len, reverse=True):
        text = text.replace(placeholder, secrets_by_placeholder[placeholder])
    return text


def _assigned_value(line: str) -> str | None:
    if line.lstrip().startswith("#") or "=" not in line:
        return None
    return line.split("=", 1)[1].strip()


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--example", type=Path, default=Path(".env.example"))
    parser.add_argument("--output", type=Path, default=Path(".env"))
    parser.add_argument("--force", action="store_true", help="overwrite an existing output file")
    args = parser.parse_args(argv)

    if args.output.exists() and not args.force:
        sys.exit(f"init_env: {args.output} already exists; pass --force to overwrite it")
    text = render(args.example.read_text(encoding="utf-8"))
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    with os.fdopen(os.open(args.output, flags, 0o600), "w", encoding="utf-8") as handle:
        handle.write(text)
    args.output.chmod(0o600)
    print(f"init_env: wrote {args.output}")


if __name__ == "__main__":
    main()
