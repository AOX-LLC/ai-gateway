"""Text that came from outside, made safe to store in a record or print to a terminal."""

import hashlib
import re
import unicodedata

_ANSI = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"  # CSI: colours, cursor moves, erase
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"  # OSC: window titles, hyperlinks
    r"|\x1b[@-Z\\-_]"  # two-character escapes
)


def printable(value: str, limit: int | None = None) -> str:
    """The text with escape sequences removed and every other control, format, surrogate,
    unassigned or private-use character (and line break) replaced by `?`.

    The result can be printed to a terminal or kept in a log without changing what the reader
    sees. A long value is cut and ends with `...`."""
    cleaned = "".join(
        "?" if unicodedata.category(char).startswith("C") else char for char in _ANSI.sub("", value)
    )
    if limit is not None and len(cleaned) > limit:
        return cleaned[: limit - 3] + "..."
    return cleaned


def sha256_of_name(value: str) -> str:
    """The hash of a client-chosen name exactly as sent, whatever characters it holds."""
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()
