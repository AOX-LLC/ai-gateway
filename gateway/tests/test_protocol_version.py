from mcp.types.version import LATEST_HANDSHAKE_VERSION

from ai_gateway.proxy.http import ACCEPTED_PROTOCOL_VERSIONS, PINNED_PROTOCOL_VERSION


def test_the_pinned_revision_is_the_sdks_newest_handshake_revision() -> None:
    # Fails on an SDK upgrade that changes the handshake revision, so the pin is
    # revisited on purpose rather than drifting.
    assert LATEST_HANDSHAKE_VERSION == PINNED_PROTOCOL_VERSION


def test_the_stateless_revision_is_not_accepted() -> None:
    assert "2026-07-28" not in ACCEPTED_PROTOCOL_VERSIONS
    assert PINNED_PROTOCOL_VERSION in ACCEPTED_PROTOCOL_VERSIONS
