import hmac
import re

import pytest

from ai_gateway.auth.tokens import generate_token, hash_token, parse_lookup_id, token_matches


def test_generated_token_has_the_documented_format() -> None:
    token = generate_token()

    assert re.fullmatch(r"aig_[a-z2-7]{8}_[A-Za-z0-9_-]{43}", token.plaintext)
    assert parse_lookup_id(token.plaintext) == token.lookup_id
    assert token.token_sha256 == hash_token(token.plaintext)


def test_generated_tokens_are_unique() -> None:
    assert len({generate_token().plaintext for _ in range(200)}) == 200


def test_repr_never_shows_the_secret() -> None:
    token = generate_token()

    assert token.plaintext not in repr(token)
    assert token.token_sha256.hex() not in repr(token)


def test_malformed_tokens_have_no_lookup_id() -> None:
    well_formed = generate_token().plaintext
    for presented in ("", "aig_", well_formed + "x", well_formed[:-1], well_formed.upper()):
        assert parse_lookup_id(presented) is None


def test_matching_uses_a_constant_time_comparison(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    original = hmac.compare_digest

    def spy(a: bytes, b: bytes) -> bool:
        calls.append((a, b))
        return original(a, b)

    monkeypatch.setattr(hmac, "compare_digest", spy)
    token = generate_token()

    assert token_matches(token.plaintext, token.token_sha256)
    assert not token_matches(token.plaintext + "x", token.token_sha256)
    assert len(calls) == 2
