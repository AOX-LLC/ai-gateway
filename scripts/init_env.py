"""Write .env from .env.example with a fresh random secret for every placeholder.

    python3 scripts/init_env.py            # no .env: write one; an existing .env: add the
                                           # keys it is missing, change nothing else
    python3 scripts/init_env.py --force    # regenerate it all (existing database roles keep
                                           # their old passwords; see the README)

With an existing .env, only keys that appear in .env.example and not in .env are appended
(with the comments above them), so pulling a release that adds a server never needs --force.
No existing line is changed, and a second run adds nothing. A new key's placeholder gets a
fresh secret, except where the .env already holds that placeholder's key: a new URL that
embeds an existing password carries that password.

Every value that starts with `change-me` becomes a new `secrets.token_urlsafe(32)`. A
placeholder that appears inside another value, such as the password in a database URL, is
replaced there too, so the URLs stay consistent with the passwords they carry. The file is
written readable by its owner only, and no secret is printed.
"""

import argparse
import os
import secrets
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


def missing_entries(example: str, existing: str) -> str:
    """The lines of `example` whose key `existing` lacks, with the comment lines above them
    and every placeholder filled in. Empty when `existing` has every key."""
    present = _assignments(existing)
    secrets_by_placeholder = _placeholder_values(example, present)
    added: list[str] = []
    comments: list[str] = []
    for line in example.splitlines():
        key = _assigned_key(line)
        if key is None:
            comments = [*comments, line] if line.lstrip().startswith("#") else []
            continue
        if key not in present:
            added.extend([*comments, _fill(line, secrets_by_placeholder)])
        comments = []
    return "".join(f"{line}\n" for line in added)


def _assignments(text: str) -> dict[str, str]:
    pairs = ((_assigned_key(line), _assigned_value(line)) for line in text.splitlines())
    return {key: value for key, value in pairs if key is not None and value is not None}


def _placeholder_values(example: str, present: dict[str, str]) -> dict[str, str]:
    """Each placeholder's value: what the .env already holds under the key that carries it
    in .env.example, or else a fresh secret."""
    values: dict[str, str] = {}
    for line in example.splitlines():
        key, value = _assigned_key(line), _assigned_value(line)
        if key in present and value is not None and value.startswith(PLACEHOLDER_PREFIX):
            values.setdefault(value, present[key])
    for line in example.splitlines():
        value = _assigned_value(line)
        if value is not None and value.startswith(PLACEHOLDER_PREFIX):
            values.setdefault(value, secrets.token_urlsafe(32))
    return values


def _fill(line: str, values: dict[str, str]) -> str:
    # Longest first, so a placeholder that contains another is replaced whole.
    for placeholder in sorted(values, key=len, reverse=True):
        line = line.replace(placeholder, values[placeholder])
    return line


def _assigned_key(line: str) -> str | None:
    if line.lstrip().startswith("#") or "=" not in line:
        return None
    return line.split("=", 1)[0].strip()


def _assigned_value(line: str) -> str | None:
    if line.lstrip().startswith("#") or "=" not in line:
        return None
    return line.split("=", 1)[1].strip()


def _add_missing(example_path: Path, output: Path) -> None:
    existing = output.read_text(encoding="utf-8")
    addition = missing_entries(example_path.read_text(encoding="utf-8"), existing)
    if not addition:
        print(f"init_env: {output} already has every key; nothing to add")
        return
    separator = "" if not existing or existing.endswith("\n") else "\n"
    with output.open("a", encoding="utf-8") as handle:
        handle.write(separator + addition)
    output.chmod(0o600)
    added = [key for line in addition.splitlines() if (key := _assigned_key(line))]
    print(f"init_env: added {len(added)} missing keys to {output}: {', '.join(added)}")


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--example", type=Path, default=Path(".env.example"))
    parser.add_argument("--output", type=Path, default=Path(".env"))
    parser.add_argument(
        "--force", action="store_true", help="regenerate an existing output file entirely"
    )
    args = parser.parse_args(argv)

    if args.output.exists() and not args.force:
        _add_missing(args.example, args.output)
        return
    text = render(args.example.read_text(encoding="utf-8"))
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    with os.fdopen(os.open(args.output, flags, 0o600), "w", encoding="utf-8") as handle:
        handle.write(text)
    args.output.chmod(0o600)
    print(f"init_env: wrote {args.output}")


if __name__ == "__main__":
    main()
