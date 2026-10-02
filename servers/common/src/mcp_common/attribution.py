"""Who asked: the client name the gateway puts in a request's `_meta`.

The gateway overwrites `_meta` on every forwarded call with its own authenticated client
name. A server records that name for attribution (who created this ticket) and for
nothing else: it is never an authorisation input, because anything that can reach a
server directly can write any value here.
"""

import re
from collections.abc import Mapping
from typing import Any

CLIENT_META_KEY = "io.aox.ai-gateway/client"
"""The `_meta` key that carries the calling client's name."""

DIRECT_CLIENT = "direct"
"""Recorded when a call carries no valid client name, e.g. one made straight to a server."""

_CLIENT_NAME = re.compile(r"^[a-z][a-z0-9-]{1,62}$")


def client_name_from_meta(meta: Mapping[str, Any] | None) -> str:
    """The client name in a request's `_meta`, or "direct" when it is absent or malformed.

    The accepted shape matches the gateway's client names, so a value that is not a
    plausible name (wrong type, too long, odd characters) is never stored.
    """
    if not meta:
        return DIRECT_CLIENT
    value = meta.get(CLIENT_META_KEY)
    if not isinstance(value, str) or _CLIENT_NAME.fullmatch(value) is None:
        return DIRECT_CLIENT
    return value
