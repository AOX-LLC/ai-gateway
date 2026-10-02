"""Client bearer tokens: their format, generation and hashing.

A token reads `aig_<lookup_id>_<secret>`. The lookup id is 8 base32 characters and is
not secret: it finds the database row and may appear in logs. The secret is 256 random
bits. Only the SHA-256 of the whole token is stored. A slow password hash would add
latency to every request without adding safety, because a 256-bit random value cannot
be guessed offline the way a password can.
"""

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass, field

TOKEN_PREFIX = "aig_"  # noqa: S105 - a format marker, not a secret
_LOOKUP_ID_ALPHABET = "abcdefghijklmnopqrstuvwxyz234567"
_LOOKUP_ID_LENGTH = 8
_SECRET_BYTES = 32

# token_urlsafe(32) always yields 43 characters of the URL-safe base64 alphabet.
_TOKEN_PATTERN = re.compile(r"^aig_(?P<lookup_id>[a-z2-7]{8})_[A-Za-z0-9_-]{43}$")


@dataclass(frozen=True)
class IssuedToken:
    """A freshly generated token. `plaintext` is shown to the operator once and never stored."""

    lookup_id: str
    token_sha256: bytes = field(repr=False)
    plaintext: str = field(repr=False)


def generate_token() -> IssuedToken:
    lookup_id = "".join(secrets.choice(_LOOKUP_ID_ALPHABET) for _ in range(_LOOKUP_ID_LENGTH))
    plaintext = f"{TOKEN_PREFIX}{lookup_id}_{secrets.token_urlsafe(_SECRET_BYTES)}"
    return IssuedToken(lookup_id=lookup_id, token_sha256=hash_token(plaintext), plaintext=plaintext)


def parse_lookup_id(presented: str) -> str | None:
    """Return the lookup id of a well-formed token, or None for anything else."""
    match = _TOKEN_PATTERN.fullmatch(presented)
    return match["lookup_id"] if match else None


def hash_token(presented: str) -> bytes:
    return hashlib.sha256(presented.encode()).digest()


def token_matches(presented: str, stored_sha256: bytes) -> bool:
    return hmac.compare_digest(hash_token(presented), stored_sha256)
