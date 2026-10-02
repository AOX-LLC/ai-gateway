import pytest

from ai_gateway.proxy.naming import expose, split_exposed


def test_exposed_names_are_namespaced() -> None:
    assert expose("echo", "say") == "echo__say"
    assert expose("crm", "find-contact") == "crm__find-contact"


@pytest.mark.parametrize("upstream_name", ["has.dot", "has space", "émoji", "", "x" * 60])
def test_names_claude_clients_cannot_take_are_refused(upstream_name: str) -> None:
    assert expose("echo", upstream_name) is None


def test_the_first_separator_splits_namespace_from_tool() -> None:
    assert split_exposed("echo__say") == ("echo", "say")
    assert split_exposed("echo___private") == ("echo", "_private")
    assert split_exposed("echo__a__b") == ("echo", "a__b")
    assert split_exposed("nounderscore") is None
