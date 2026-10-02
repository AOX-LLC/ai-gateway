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
replaced there too, so the URLs stay consistent with the passwords they carry. An existing
password is percent-encoded inside a URL, so `p@ss:word` cannot break it, and a new URL for
the owner role or the main database uses the existing POSTGRES_USER and POSTGRES_DB rather
than the example's. An existing password that is empty is never copied into a URL: the
script stops and names the key to set. `export KEY=value` lines count as keys. The file is
created readable by its owner only (a private temporary file renamed into place), and no
secret is printed.
"""

import argparse
import os
import re
import secrets
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import quote

PLACEHOLDER_PREFIX = "change-me"
_EXPORT = re.compile(r"^\s*export\s+")
_URL_IDENTITY = re.compile(r"(?P<head>://)(?P<user>[^:/@]*)(?P<mid>:[^@]*@[^/\s]*/)(?P<db>[^?\s]*)")


class EmptyPasswordError(ValueError):
    """A new URL needs the password of a key that exists but is empty."""

    def __init__(self, keys: list[str]) -> None:
        super().__init__(f"{', '.join(keys)} is empty")
        self.keys = keys


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
    identity = _identity(example, present)
    added: list[str] = []
    comments: list[str] = []
    for line in example.splitlines():
        key = _assigned_key(line)
        if key is None:
            comments = [*comments, line] if line.lstrip().startswith("#") else []
            continue
        if key not in present:
            added.extend([*comments, _fill(line, secrets_by_placeholder, identity)])
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


def _identity(example: str, present: dict[str, str]) -> dict[str, str]:
    """The example's owner user and main database name, each mapped to what the .env already
    holds under POSTGRES_USER and POSTGRES_DB, where that is set and different."""
    renames: dict[str, str] = {}
    for line in example.splitlines():
        key, value = _assigned_key(line), _assigned_value(line)
        if key in ("POSTGRES_USER", "POSTGRES_DB") and value and present.get(key):
            renames[value] = present[key]
    return {old: new for old, new in renames.items() if old != new}


def _fill(line: str, values: dict[str, str], identity: dict[str, str]) -> str:
    """`line` with each placeholder replaced: as is when it is the whole value, and
    percent-encoded when it sits inside a URL."""
    whole = _assigned_value(line)
    # Longest first, so a placeholder that contains another is replaced whole.
    for placeholder in sorted(values, key=len, reverse=True):
        if placeholder not in line:
            continue
        if not values[placeholder]:
            raise EmptyPasswordError([])
        line = line.replace(
            placeholder,
            values[placeholder] if whole == placeholder else quote(values[placeholder], safe=""),
        )
    return _URL_IDENTITY.sub(lambda m: _renamed(m, identity), line)


def _renamed(match: re.Match[str], identity: dict[str, str]) -> str:
    user = identity.get(match["user"], match["user"])
    database = identity.get(match["db"], match["db"])
    return f"{match['head']}{user}{match['mid']}{database}"


def _assigned_key(line: str) -> str | None:
    if line.lstrip().startswith("#") or "=" not in line:
        return None
    return _EXPORT.sub("", line.split("=", 1)[0]).strip()


def _assigned_value(line: str) -> str | None:
    if line.lstrip().startswith("#") or "=" not in line:
        return None
    return line.split("=", 1)[1].strip()


def _empty_keys(example: str, present: dict[str, str]) -> list[str]:
    """The keys that are empty in the .env but carry a placeholder in the example."""
    keys = []
    for line in example.splitlines():
        key, value = _assigned_key(line), _assigned_value(line)
        if key in present and not present[key] and value and value.startswith(PLACEHOLDER_PREFIX):
            keys.append(key)
    return keys


def _write_private(path: Path, text: str) -> None:
    """Replace `path` with `text`, never readable by anyone else at any moment: the data goes
    into a new file that is private from creation (mkstemp makes it 0600), then is renamed."""
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _add_missing(example_path: Path, output: Path) -> None:
    existing = output.read_text(encoding="utf-8")
    example = example_path.read_text(encoding="utf-8")
    try:
        addition = missing_entries(example, existing)
    except EmptyPasswordError:
        empty = _empty_keys(example, _assignments(existing))
        sys.exit(
            f"init_env: {', '.join(empty)} in {output} is empty, and a new entry needs its"
            " value. Set it (or remove the line to get a fresh secret), then run this again."
        )
    if not addition:
        print(f"init_env: {output} already has every key; nothing to add")
        return
    separator = "" if not existing or existing.endswith("\n") else "\n"
    _write_private(output, existing + separator + addition)
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
    _write_private(args.output, text)
    print(f"init_env: wrote {args.output}")


if __name__ == "__main__":
    main()
