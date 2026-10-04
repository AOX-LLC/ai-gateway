"""Keyed hashes of what a call carries, for records that must not let an argument be guessed.

An unkeyed SHA-256 of a low-entropy argument (`{"ticket_id":"TKT-000123"}`) can be recovered by
anyone who can read the record by hashing every candidate. The hash stored in decision records,
telemetry and new audit records is therefore an HMAC-SHA256 under `GATEWAY_ARGUMENT_HASH_KEY`, a
secret the readers of those records do not hold. It still lets two records for one set of arguments
be matched, which is all it is for. Records written before the key existed hold the plain hash, and
changing the key breaks the match between records made under different keys (it does not expose
anything). agent-core's own approval payload hash is left alone: it binds an approval to its
arguments and is agent-core's.
"""

import hashlib
import hmac

_MIN_KEY_BYTES = 32
_PLACEHOLDER = b"change-me"
_key: bytes | None = None


def configure_hash_key(key: bytes) -> None:
    """Set the key, once at startup. A key too short to be a secret, or still the placeholder of
    `.env.example` (as the servers refuse a service credential that is), is refused."""
    global _key
    if len(key) < _MIN_KEY_BYTES:
        raise ValueError("the argument hash key is too short to be a secret")
    if _PLACEHOLDER in key:
        raise ValueError("the argument hash key is still the placeholder: run scripts/init_env.py")
    _key = key


def keyed_sha256(data: str | bytes) -> str:
    """HMAC-SHA256 of the data, in hex. Raises if no key was configured: a gateway that forgot it
    must not quietly fall back to a hash anyone can compute."""
    if _key is None:
        raise RuntimeError("the argument hash key is not configured")
    raw = data.encode() if isinstance(data, str) else data
    return hmac.new(_key, raw, hashlib.sha256).hexdigest()
