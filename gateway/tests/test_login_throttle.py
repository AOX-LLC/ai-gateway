"""The failed-login throttle: per token id, and a ceiling over all of them."""

import httpx2
import pytest

from ai_gateway.auth.middleware import BearerAuthMiddleware
from ai_gateway.auth.throttle import LoginThrottle, ThrottleConfig
from ai_gateway.auth.tokens import generate_token
from ai_gateway.auth.verifier import TokenVerifier
from ai_gateway.seams.events import MemoryEventSink
from tests.test_auth import InMemoryRegistry, _tampered, _whoami


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _throttle(clock: Clock, **options: float) -> LoginThrottle:
    config = ThrottleConfig(
        failures_per_id=3, window_s=60.0, lockout_s=30.0, global_ceiling=10, **options
    )
    return LoginThrottle(config, clock)


def test_an_id_that_fails_too_often_is_refused_until_the_lockout_ends() -> None:
    clock = Clock()
    throttle = _throttle(clock)
    for _ in range(2):
        throttle.record_failure("abcdefgh")
        assert throttle.check("abcdefgh") is None
    throttle.record_failure("abcdefgh")  # the third

    assert throttle.check("abcdefgh") == pytest.approx(30.0)
    assert throttle.check("zzzzzzzz") is None, "other ids are not affected"
    clock.now += 29
    assert throttle.check("abcdefgh") == pytest.approx(1.0)
    clock.now += 2
    assert throttle.check("abcdefgh") is None
    throttle.record_failure("abcdefgh")
    assert throttle.check("abcdefgh") is None, "the count starts again after a lockout"


def test_failures_older_than_the_window_do_not_count() -> None:
    clock = Clock()
    throttle = _throttle(clock)
    for _ in range(2):
        throttle.record_failure("abcdefgh")
    clock.now += 61

    throttle.record_failure("abcdefgh")

    assert throttle.check("abcdefgh") is None


def test_a_spray_of_unknown_ids_trips_the_ceiling_and_only_working_ids_are_served() -> None:
    clock = Clock()
    throttle = _throttle(clock)
    throttle.record_success("goodgood")
    for number in range(10):  # each id fails once: no id is ever locked
        throttle.record_failure(f"spray{number:03d}"[:8])
        assert throttle.check(f"spray{number:03d}"[:8]) is None or number >= 9

    assert throttle.check("goodgood") is None, "a client that was working still is"
    assert throttle.check("brandnew") is not None, "a stranger is refused while the attack lasts"
    assert throttle.check(None) is not None, "so is a request with no token id at all"
    clock.now += 61
    assert throttle.check("brandnew") is None, "and once the failures age out, everyone is served"


def test_a_known_good_id_is_forgotten_after_its_time() -> None:
    clock = Clock()
    throttle = _throttle(clock, known_good_ttl_s=100.0)
    throttle.record_success("goodgood")
    for number in range(10):
        throttle.record_failure(f"x{number}")
    assert throttle.check("goodgood") is None
    clock.now += 101
    for number in range(10):  # the old failures aged out; a new attack is on
        throttle.record_failure(f"y{number}")

    assert throttle.check("goodgood") is not None, "it has not worked for longer than its time"


def test_the_memory_is_bounded_whatever_ids_an_attacker_invents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_gateway.auth import throttle as module

    monkeypatch.setattr(module, "_MAX_TRACKED_IDS", 100)
    monkeypatch.setattr(module, "_MAX_KNOWN_GOOD", 50)
    throttle = LoginThrottle(ThrottleConfig(global_ceiling=10**9), Clock())

    for number in range(5000):
        throttle.record_failure(f"id{number}")
        throttle.record_success(f"ok{number}")

    assert len(throttle._failures) <= 100
    assert len(throttle._known_good) <= 50


# --- through the middleware ---------------------------------------------------------------------


def _client(
    verifier: TokenVerifier, events: MemoryEventSink, throttle: LoginThrottle
) -> httpx2.AsyncClient:
    app = BearerAuthMiddleware(_whoami, verifier, events, throttle=throttle)
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://127.0.0.1")


@pytest.mark.anyio
async def test_after_too_many_wrong_secrets_the_id_gets_a_429_even_with_the_right_one() -> None:
    registry = InMemoryRegistry()
    verifier = TokenVerifier(registry)
    token = generate_token()
    registry.add(token)
    events = MemoryEventSink()
    throttle = _throttle(Clock())
    wrong = {"Authorization": f"Bearer {_tampered(token.plaintext)}"}
    right = {"Authorization": f"Bearer {token.plaintext}"}

    async with _client(verifier, events, throttle) as client:
        first = [(await client.get("/", headers=wrong)).status_code for _ in range(3)]
        locked = await client.get("/", headers=right)

    assert first == [401, 401, 401]
    assert locked.status_code == 429
    assert int(locked.headers["retry-after"]) >= 1
    assert locked.json()["error"] == "too_many_attempts"
    reasons = [e.payload["reason"] for e in events.events]
    assert reasons == ["wrong_secret"] * 3 + ["throttled"]
    assert token.plaintext not in str(events.events), "the secret is in no record"


@pytest.mark.anyio
async def test_a_refused_attempt_is_not_verified_and_does_not_extend_the_lockout() -> None:
    registry = InMemoryRegistry()
    token = generate_token()
    registry.add(token)
    lookups: list[str] = []
    original = registry.find_token

    async def counting(lookup_id: str):  # type: ignore[no-untyped-def]
        lookups.append(lookup_id)
        return await original(lookup_id)

    registry.find_token = counting  # type: ignore[method-assign]
    clock = Clock()
    throttle = _throttle(clock)
    wrong = {"Authorization": f"Bearer {_tampered(token.plaintext)}"}

    async with _client(TokenVerifier(registry), MemoryEventSink(), throttle) as client:
        for _ in range(3):
            await client.get("/", headers=wrong)
        before = len(lookups)
        for _ in range(20):
            assert (await client.get("/", headers=wrong)).status_code == 429
        clock.now += 31
        again = await client.get("/", headers=wrong)

    assert len(lookups) == before + 1, "twenty refused attempts looked nothing up"
    assert again.status_code == 401, "the lockout ended on time: refusals did not extend it"


@pytest.mark.anyio
async def test_another_clients_logins_are_unaffected_by_one_ids_lockout() -> None:
    registry = InMemoryRegistry()
    attacked, other = generate_token(), generate_token()
    registry.add(attacked)
    registry.add(other)
    throttle = _throttle(Clock())
    wrong = {"Authorization": f"Bearer {_tampered(attacked.plaintext)}"}

    async with _client(TokenVerifier(registry), MemoryEventSink(), throttle) as client:
        for _ in range(3):
            await client.get("/", headers=wrong)
        fine = await client.get("/", headers={"Authorization": f"Bearer {other.plaintext}"})

    assert fine.status_code == 200
