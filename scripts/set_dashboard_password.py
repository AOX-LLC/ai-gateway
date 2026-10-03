#!/usr/bin/env python3
"""Set the dashboard's admin password in .env, as a scrypt hash. Prints nothing secret.

    python3 scripts/set_dashboard_password.py
    python3 scripts/set_dashboard_password.py --env other.env
    DASHBOARD_PASSWORD=... python3 scripts/set_dashboard_password.py --password-env VAR_NAME

It asks for the password twice (or reads the named variable) and writes
DASHBOARD_ADMIN_PASSWORD_HASH. The hash is `scrypt:<N>:<r>:<p>:<salt>:<hash>` (base64url, no `$`,
which Compose would read as a variable), with the same parameters and Unicode normalisation as
dashboard/src/lib/auth/password.ts, which verifies it. The password is never an argument (it would
show in `ps`). The key is replaced in place, or added; every other line is left alone, and the file
is rewritten privately (0600) through a temporary file. Restart the dashboard afterwards: it reads
its settings once.
"""

import argparse
import base64
import getpass
import hashlib
import os
import re
import secrets
import sys
import tempfile
import unicodedata
from pathlib import Path

KEY = "DASHBOARD_ADMIN_PASSWORD_HASH"
N, R, P, KEY_LENGTH, SALT_LENGTH = 32768, 8, 1, 32, 16
MAX_MEMORY = 128 * 1024 * 1024
MIN_LENGTH = 12
MAX_LENGTH = 512
"""Sign-in refuses over 1024 UTF-16 units; 512 characters is under that however counted."""


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = secrets.token_bytes(SALT_LENGTH) if salt is None else salt
    key = hashlib.scrypt(
        unicodedata.normalize("NFKC", password).encode(),
        salt=salt,
        n=N,
        r=R,
        p=P,
        maxmem=MAX_MEMORY,
        dklen=KEY_LENGTH,
    )

    def b64(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return ":".join(["scrypt", str(N), str(R), str(P), b64(salt), b64(key)])


def with_hash(text: str, encoded: str) -> str:
    """`text` with the key set to `encoded`: replaced where it is, else added at the end."""
    line = f"{KEY}={encoded}"
    pattern = re.compile(rf"^(?:export\s+)?{KEY}=.*$", re.MULTILINE)
    if pattern.search(text):
        return pattern.sub(lambda _: line, text, count=1)
    return text + ("" if text.endswith("\n") or not text else "\n") + line + "\n"


def write_private(path: Path, text: str) -> None:
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".env-")
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(text)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        os.unlink(temporary)
        raise


def read_password(args: argparse.Namespace) -> str:
    if args.password_env:
        password = os.environ.get(args.password_env, "")
        if not password:
            sys.exit(f"set_dashboard_password: {args.password_env} is not set")
        return password
    first = getpass.getpass("Dashboard admin password: ")
    if first != getpass.getpass("Again: "):
        sys.exit("set_dashboard_password: the two did not match")
    return first


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--env", type=Path, default=Path(".env"), help="default: .env")
    parser.add_argument(
        "--password-env", metavar="VAR", help="read the password from this variable"
    )
    args = parser.parse_args(argv)
    password = read_password(args)
    if len(password) < MIN_LENGTH:
        sys.exit(f"set_dashboard_password: use at least {MIN_LENGTH} characters")
    if len(password) > MAX_LENGTH:
        sys.exit(
            f"set_dashboard_password: use at most {MAX_LENGTH} characters (sign-in refuses longer)"
        )
    text = args.env.read_text() if args.env.exists() else ""
    write_private(args.env, with_hash(text, hash_password(password)))
    print(f"{KEY} is set in {args.env}. Restart the dashboard to use it.")


if __name__ == "__main__":
    main()
