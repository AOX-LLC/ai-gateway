"""Text from outside is cleaned before it is recorded or printed."""

import hashlib

import pytest

from ai_gateway.pipeline.types import displayable_tool_name
from ai_gateway.text import printable, sha256_of_name


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("tickets__get", "tickets__get"),
        ("a\x1b[31mred\x1b[0m", "ared"),  # colour codes
        ("a\x1b]0;owned title\x07b", "ab"),  # window title
        ("a\x1b]8;;http://x\x1b\\link\x1b]8;;\x1b\\", "alink"),  # hyperlink
        ("a\nb\rc\td", "a?b?c?d"),
        ("a\x00b\x7fc\x85d", "a?b?c?d"),
        ("a‮b​c", "a?b?c"),  # bidirectional override, zero-width space
        ("a\ud800b", "a?b"),  # a lone surrogate
        ("a\u2028b\u2029c", "a?b?c"),  # line and paragraph separators
        ("x" * 100, "x" * 37 + "..."),
    ],
)
def test_control_characters_and_escape_sequences_never_survive(raw: str, clean: str) -> None:
    assert printable(raw, 40) == clean


def test_a_displayable_tool_name_is_cleaned_and_cut() -> None:
    assert displayable_tool_name("echo__\x1b[2Jsay") == "echo__say"
    assert len(displayable_tool_name("x" * 500)) == 64


def test_the_hash_of_a_name_covers_all_of_it_and_survives_a_lone_surrogate() -> None:
    long_name = "x" * 500
    assert sha256_of_name(long_name) == hashlib.sha256(long_name.encode()).hexdigest()
    assert sha256_of_name(long_name) != sha256_of_name(long_name + "y")
    assert len(sha256_of_name("a\ud800b")) == 64
