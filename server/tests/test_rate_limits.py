"""Per-IP rate limiting on the unauthenticated surface: room creation,
WebSocket connection attempts, and how the client IP is derived.

The limiter and endpoint tests run against both stores: the default in-process
counters and the shared Redis one (via FakeRedis below, so no server and no
extra test dependency)."""

from __future__ import annotations

import logging
import sys
import time
import types
from collections.abc import Callable, Iterator
from fnmatch import fnmatchcase
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.requests import HTTPConnection
from starlette.websockets import WebSocketDisconnect

try:
    from server import config, limits
    from server.limits import (
        FixedWindowLimiter,
        RedisCounterStore,
        build_limiter,
        client_ip,
    )
    from server.main import WS_CLOSE_RATE_LIMITED, app
except ImportError:  # Render rootDir=server
    import config
    import limits
    from limits import (
        FixedWindowLimiter,
        RedisCounterStore,
        build_limiter,
        client_ip,
    )
    from main import WS_CLOSE_RATE_LIMITED, app


def _connection(
    *,
    peer: tuple[str, int] | None,
    forwarded: str | None = None,
    extra: dict[str, str] | None = None,
) -> HTTPConnection:
    headers = [(b"x-forwarded-for", forwarded.encode())] if forwarded else []
    for name, value in (extra or {}).items():
        headers.append((name.lower().encode(), value.encode()))
    return HTTPConnection({"type": "http", "headers": headers, "client": peer})


@pytest.fixture
def no_platform_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    """client_ip as on a plain host: not on Fly, not trusting Cloudflare, one
    proxy hop, whatever the test machine's environment says."""
    monkeypatch.setattr(config, "BEHIND_FLY", False)
    monkeypatch.setattr(config, "TRUST_CLOUDFLARE", False)
    monkeypatch.setattr(config, "TRUSTED_PROXY_HOPS", 1)


# --- a Redis double -----------------------------------------------------------


class RedisDown(ConnectionError):
    """What FakeRedis raises while `down` is set."""


class FakeRedis:
    """The slice of redis-py the limiter uses, with Redis's semantics for it:
    SET NX PX, INCR keeping the expiry, PTTL, keys vanishing at their expiry.

    Time comes from `clock` (seconds) so tests can step it like a real Redis
    clock moving on.
    """

    def __init__(self, clock: Callable[[], float]) -> None:
        self.clock = clock
        self.down = False
        # Round trips attempted, including ones that failed while down.
        self.attempts = 0
        self._values: dict[str, int] = {}
        self._expires_at: dict[str, float] = {}

    # --- client surface ---

    def pipeline(self, transaction: bool = True) -> FakePipeline:
        assert transaction, "limiter counts must be updated atomically"
        return FakePipeline(self)

    def pexpire(self, name: str, ms: int) -> bool:
        self._call()
        if name not in self._values:
            return False
        self._expires_at[name] = self.clock() + ms / 1000
        return True

    def scan_iter(self, match: str) -> Iterator[str]:
        self._call()
        return iter([key for key in list(self._values) if fnmatchcase(key, match)])

    def delete(self, *names: str) -> int:
        self._call()
        removed = 0
        for name in names:
            removed += self._values.pop(name, None) is not None
            self._expires_at.pop(name, None)
        return removed

    # --- commands, applied by FakePipeline.execute ---

    def _call(self) -> None:
        self.attempts += 1
        if self.down:
            raise RedisDown("Connection refused")
        now = self.clock()
        for name, expires_at in list(self._expires_at.items()):
            if now >= expires_at:
                self._values.pop(name, None)
                del self._expires_at[name]

    def _set(self, name: str, value: int, nx: bool, px: int | None) -> bool | None:
        if nx and name in self._values:
            return None
        self._values[name] = value
        self._expires_at.pop(name, None)
        if px is not None:
            self._expires_at[name] = self.clock() + px / 1000
        return True

    def _incr(self, name: str) -> int:
        self._values[name] = self._values.get(name, 0) + 1
        return self._values[name]

    def _pttl(self, name: str) -> int:
        if name not in self._values:
            return -2
        if name not in self._expires_at:
            return -1
        return round((self._expires_at[name] - self.clock()) * 1000)

    def set_without_expiry(self, name: str, value: int) -> None:
        self._values[name] = value
        self._expires_at.pop(name, None)


class FakePipeline:
    def __init__(self, redis: FakeRedis) -> None:
        self._redis = redis
        self._queued: list[Callable[[], Any]] = []

    def set(
        self, name: str, value: int, nx: bool = False, px: int | None = None
    ) -> FakePipeline:
        self._queued.append(lambda: self._redis._set(name, value, nx, px))
        return self

    def incr(self, name: str) -> FakePipeline:
        self._queued.append(lambda: self._redis._incr(name))
        return self

    def pttl(self, name: str) -> FakePipeline:
        self._queued.append(lambda: self._redis._pttl(name))
        return self

    def execute(self) -> list[Any]:
        # One round trip, applied all-or-nothing like MULTI/EXEC.
        self._redis._call()
        results = [command() for command in self._queued]
        self._queued.clear()
        return results


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


# --- the two stores -----------------------------------------------------------


class Backend:
    """Builds limiters on one store and steps time for both of them.

    The in-process limiter takes time through `now=`; the Redis one ignores
    `now` and reads Redis's clock, so `check` moves both together.
    """

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.clock = Clock()
        self.redis = FakeRedis(self.clock) if kind == "redis" else None
        self.store = RedisCounterStore(self.redis) if self.redis is not None else None

    def limiter(
        self, *, limit: int, window_seconds: float, max_keys: int = 8, name: str = "t"
    ) -> limits.RateLimiter:
        return build_limiter(
            name,
            limit=limit,
            window_seconds=window_seconds,
            max_keys=max_keys,
            store=self.store,
        )

    def check(
        self, limiter: limits.RateLimiter, key: str, *, at: float
    ) -> limits.RateLimitResult:
        self.clock.now = at
        return limiter.check(key, now=at)


@pytest.fixture(params=["memory", "redis"])
def backend(request: pytest.FixtureRequest) -> Backend:
    return Backend(request.param)


@pytest.fixture(params=["memory", "redis"])
def relay_backend(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> str:
    """Puts the relay's own limiters on the given store for one test."""
    if request.param == "redis":
        store = RedisCounterStore(FakeRedis(time.monotonic))
        for attr, source in (
            ("room_creation_limiter", limits.room_creation_limiter),
            ("ws_attempt_limiter", limits.ws_attempt_limiter),
        ):
            monkeypatch.setattr(
                limits,
                attr,
                build_limiter(
                    f"relay_{attr}",
                    limit=source.limit,
                    window_seconds=source.window_seconds,
                    max_keys=8,
                    store=store,
                ),
            )
    return request.param


# --- the limiter itself -------------------------------------------------------


def test_limiter_allows_up_to_the_limit_then_denies(backend: Backend) -> None:
    limiter = backend.limiter(limit=2, window_seconds=60)

    assert backend.check(limiter, "ip", at=0.0).allowed
    assert backend.check(limiter, "ip", at=1.0).allowed

    denied = backend.check(limiter, "ip", at=2.0)
    assert not denied.allowed
    assert denied.retry_after_seconds == 58


def test_limiter_keys_are_independent(backend: Backend) -> None:
    limiter = backend.limiter(limit=1, window_seconds=60)

    assert backend.check(limiter, "a", at=0.0).allowed
    assert not backend.check(limiter, "a", at=0.0).allowed
    assert backend.check(limiter, "b", at=0.0).allowed


def test_limiter_budget_returns_in_the_next_window(backend: Backend) -> None:
    limiter = backend.limiter(limit=1, window_seconds=60)

    assert backend.check(limiter, "ip", at=0.0).allowed
    assert not backend.check(limiter, "ip", at=30.0).allowed
    assert backend.check(limiter, "ip", at=60.0).allowed


def test_limiter_reset_restores_the_budget(backend: Backend) -> None:
    limiter = backend.limiter(limit=1, window_seconds=60)

    assert backend.check(limiter, "ip", at=0.0).allowed
    assert not backend.check(limiter, "ip", at=0.0).allowed

    limiter.reset()

    assert backend.check(limiter, "ip", at=0.0).allowed


def test_limiter_evicts_oldest_keys_instead_of_growing() -> None:
    """In-process only: Redis bounds its key set with expiries instead."""
    limiter = FixedWindowLimiter(limit=1, window_seconds=60, max_keys=2)

    assert limiter.check("a", now=0.0).allowed
    assert not limiter.check("a", now=0.0).allowed

    limiter.check("b", now=0.0)
    limiter.check("c", now=0.0)

    assert limiter.tracked_keys == 2
    # "a" was evicted, so its window restarted rather than being remembered.
    assert limiter.check("a", now=0.0).allowed
    assert limiter.tracked_keys == 2


# --- the Redis store specifically ---------------------------------------------


def test_redis_budget_is_shared_between_relay_instances() -> None:
    """The point of the Redis store: two relays draw on one budget, where two
    in-process limiters would each grant the full limit."""
    clock = Clock()
    redis = FakeRedis(clock)

    def instance() -> limits.RateLimiter:
        return build_limiter(
            "room_create",
            limit=2,
            window_seconds=60,
            max_keys=8,
            store=RedisCounterStore(redis),
        )

    first, second = instance(), instance()

    assert first.check("203.0.113.9").allowed
    assert second.check("203.0.113.9").allowed
    assert not first.check("203.0.113.9").allowed
    assert not second.check("203.0.113.9").allowed


def test_redis_counters_are_namespaced_and_expire() -> None:
    clock = Clock()
    redis = FakeRedis(clock)
    store = RedisCounterStore(redis)
    limiter = build_limiter(
        "ws_attempt", limit=5, window_seconds=60, max_keys=8, store=store
    )
    key = "hivelane:ratelimit:ws_attempt:10.0.0.7"

    limiter.check("10.0.0.7")

    assert redis._values == {key: 1}
    assert redis._pttl(key) == 60_000

    # Redis drops idle counters itself, so the key set cannot grow unbounded.
    clock.now = 60.0
    assert list(redis.scan_iter(match="hivelane:*")) == []


def test_redis_counter_left_without_expiry_is_repaired() -> None:
    """A key that somehow lost its TTL would otherwise throttle that IP forever."""
    clock = Clock()
    redis = FakeRedis(clock)
    redis.set_without_expiry("hivelane:ratelimit:t:ip", 99)
    limiter = build_limiter(
        "t", limit=1, window_seconds=60, max_keys=8, store=RedisCounterStore(redis)
    )

    denied = limiter.check("ip")

    assert not denied.allowed
    assert denied.retry_after_seconds == 60
    clock.now = 60.0
    assert limiter.check("ip").allowed


def test_unreachable_redis_falls_back_to_in_process_limits(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = Clock()
    redis = FakeRedis(clock)
    redis.down = True
    store = RedisCounterStore(redis, retry_seconds=30, clock=clock)
    limiter = build_limiter("t", limit=1, window_seconds=60, max_keys=8, store=store)

    with caplog.at_level(logging.WARNING, logger=limits.__name__):
        first = limiter.check("ip", now=0.0)
        second = limiter.check("ip", now=0.0)

    # Still limited, just per process: failing open must not mean unlimited.
    assert first.allowed
    assert not second.allowed
    assert not store.available
    warnings = [r for r in caplog.records if "unreachable" in r.getMessage()]
    assert len(warnings) == 1, "an outage is logged once, not once per request"


def test_fallback_enforces_a_budget_changed_at_runtime() -> None:
    redis = FakeRedis(Clock())
    redis.down = True
    limiter = build_limiter(
        "t", limit=5, window_seconds=60, max_keys=8, store=RedisCounterStore(redis)
    )
    limiter.limit = 1

    assert limiter.check("ip", now=0.0).allowed
    assert not limiter.check("ip", now=0.0).allowed


def test_redis_is_not_retried_on_every_request_during_an_outage(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Each attempt can cost up to REDIS_TIMEOUT_SECONDS on the event loop."""
    clock = Clock()
    redis = FakeRedis(clock)
    store = RedisCounterStore(redis, retry_seconds=30, clock=clock)
    limiter = build_limiter("t", limit=10, window_seconds=60, max_keys=8, store=store)

    redis.down = True
    for _ in range(5):
        limiter.check("ip", now=clock.now)
    assert redis.attempts == 1

    # Once the retry interval passes, Redis is probed again and, when it
    # answers, used again.
    redis.down = False
    clock.now = 31.0
    with caplog.at_level(logging.INFO, logger=limits.__name__):
        assert limiter.check("ip", now=clock.now).allowed
    assert redis.attempts == 2
    assert store.available
    assert any("reachable again" in r.getMessage() for r in caplog.records)


def test_reset_survives_an_unreachable_redis() -> None:
    redis = FakeRedis(Clock())
    redis.down = True
    limiter = build_limiter(
        "t", limit=1, window_seconds=60, max_keys=8, store=RedisCounterStore(redis)
    )

    limiter.reset()  # must not raise


def test_relay_limiters_stay_in_process_without_a_redis_url() -> None:
    if config.REDIS_URL:
        pytest.skip("HIVELANE_REDIS_URL is set in this environment")

    assert limits.BACKEND == "memory"
    assert isinstance(limits.room_creation_limiter, FixedWindowLimiter)
    assert isinstance(limits.ws_attempt_limiter, FixedWindowLimiter)


def test_missing_redis_package_keeps_limits_in_process(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """redis is optional: setting the URL without installing it must not take
    the relay down."""
    monkeypatch.setitem(sys.modules, "redis", None)

    with caplog.at_level(logging.WARNING, logger=limits.__name__):
        client = limits.redis_client("redis://localhost:6379/0")

    assert client is None
    assert any("pip install redis" in r.getMessage() for r in caplog.records)


def test_redis_client_cannot_stall_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The client is blocking and runs on the event loop: it must time out
    quickly and must not retry with backoff."""
    seen: dict[str, Any] = {}

    class Redis:
        @classmethod
        def from_url(cls, url: str, **kwargs: Any) -> str:
            seen.update(kwargs, url=url)
            return "client"

    class Retry:
        def __init__(self, backoff: object, retries: int) -> None:
            self.retries = retries

    package = types.ModuleType("redis")
    package.Redis = Redis  # type: ignore[attr-defined]
    backoff = types.ModuleType("redis.backoff")
    backoff.NoBackoff = object  # type: ignore[attr-defined]
    retry = types.ModuleType("redis.retry")
    retry.Retry = Retry  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "redis", package)
    monkeypatch.setitem(sys.modules, "redis.backoff", backoff)
    monkeypatch.setitem(sys.modules, "redis.retry", retry)

    assert limits.redis_client("rediss://:secret@cache.example:6380/0") == "client"
    assert seen["url"] == "rediss://:secret@cache.example:6380/0"
    assert seen["socket_timeout"] <= 0.5
    assert seen["socket_connect_timeout"] <= 0.5
    assert seen["retry"].retries == 0


# --- HIVELANE_REDIS_URL --------------------------------------------------------


def test_redis_url_is_off_by_default() -> None:
    assert config.load_settings({}).redis_url is None
    assert config.load_settings({"HIVELANE_REDIS_URL": "  "}).redis_url is None


@pytest.mark.parametrize(
    "url",
    [
        "redis://localhost:6379/0",
        "rediss://:secret@cache.example:6380/0",
        "unix:///var/run/redis.sock",
    ],
)
def test_redis_url_is_read(url: str) -> None:
    assert config.load_settings({"HIVELANE_REDIS_URL": url}).redis_url == url


def test_malformed_redis_url_fails_the_boot_without_echoing_it() -> None:
    with pytest.raises(config.ConfigError) as excinfo:
        config.load_settings({"HIVELANE_REDIS_URL": "user:hunter2@cache:6379"})

    assert "HIVELANE_REDIS_URL" in str(excinfo.value)
    assert "hunter2" not in str(excinfo.value)


# --- client_ip ----------------------------------------------------------------


def test_client_ip_uses_socket_peer_by_default(
    monkeypatch: pytest.MonkeyPatch, no_platform_headers: None
) -> None:
    monkeypatch.setattr(config, "TRUST_PROXY_HEADER", False)

    connection = _connection(peer=("10.0.0.7", 51234), forwarded="203.0.113.9")

    assert client_ip(connection) == "10.0.0.7"


def test_client_ip_honours_forwarded_header_when_trusted(
    monkeypatch: pytest.MonkeyPatch, no_platform_headers: None
) -> None:
    monkeypatch.setattr(config, "TRUST_PROXY_HEADER", True)

    connection = _connection(peer=("10.0.0.7", 51234), forwarded="203.0.113.9")

    assert client_ip(connection) == "203.0.113.9"


def test_client_ip_ignores_the_client_supplied_left_of_the_header(
    monkeypatch: pytest.MonkeyPatch, no_platform_headers: None
) -> None:
    """Behind one proxy, everything left of the last entry is whatever the
    caller sent: trusting the leftmost let anyone pick their own IP."""
    monkeypatch.setattr(config, "TRUST_PROXY_HEADER", True)

    spoofed = _connection(
        peer=("10.0.0.7", 51234), forwarded="1.2.3.4, 5.6.7.8, 203.0.113.9"
    )

    assert client_ip(spoofed) == "203.0.113.9"


@pytest.mark.parametrize(
    "hops,forwarded,expected",
    [
        # CDN -> load balancer -> relay: the CDN appended the client.
        (2, "203.0.113.9, 70.41.3.18", "203.0.113.9"),
        (2, "6.6.6.6, 203.0.113.9, 70.41.3.18", "203.0.113.9"),
        # Fewer entries than hops: the leftmost was still written by a proxy.
        (3, "203.0.113.9, 70.41.3.18", "203.0.113.9"),
        # Blank entries do not count as hops.
        (1, "6.6.6.6, , 203.0.113.9 ,", "203.0.113.9"),
    ],
)
def test_trusted_proxy_hops_picks_the_entry_from_the_right(
    monkeypatch: pytest.MonkeyPatch,
    no_platform_headers: None,
    hops: int,
    forwarded: str,
    expected: str,
) -> None:
    monkeypatch.setattr(config, "TRUST_PROXY_HEADER", True)
    monkeypatch.setattr(config, "TRUSTED_PROXY_HOPS", hops)

    assert client_ip(_connection(peer=("10.0.0.7", 1), forwarded=forwarded)) == expected


def test_fly_client_ip_wins_on_fly(
    monkeypatch: pytest.MonkeyPatch, no_platform_headers: None
) -> None:
    monkeypatch.setattr(config, "BEHIND_FLY", True)
    monkeypatch.setattr(config, "TRUST_PROXY_HEADER", True)

    connection = _connection(
        peer=("172.16.0.2", 1),
        forwarded="6.6.6.6, 198.51.100.1",
        extra={"Fly-Client-IP": "203.0.113.9"},
    )

    assert client_ip(connection) == "203.0.113.9"


def test_fly_client_ip_is_ignored_off_fly(
    monkeypatch: pytest.MonkeyPatch, no_platform_headers: None
) -> None:
    monkeypatch.setattr(config, "TRUST_PROXY_HEADER", False)

    connection = _connection(peer=("10.0.0.7", 1), extra={"Fly-Client-IP": "6.6.6.6"})

    assert client_ip(connection) == "10.0.0.7"


def test_cloudflare_header_needs_opt_in(
    monkeypatch: pytest.MonkeyPatch, no_platform_headers: None
) -> None:
    monkeypatch.setattr(config, "TRUST_PROXY_HEADER", False)
    connection = _connection(
        peer=("10.0.0.7", 1), extra={"CF-Connecting-IP": "203.0.113.9"}
    )

    assert client_ip(connection) == "10.0.0.7"

    monkeypatch.setattr(config, "TRUST_CLOUDFLARE", True)
    assert client_ip(connection) == "203.0.113.9"


def test_platform_header_missing_falls_through_to_forwarded(
    monkeypatch: pytest.MonkeyPatch, no_platform_headers: None
) -> None:
    monkeypatch.setattr(config, "BEHIND_FLY", True)
    monkeypatch.setattr(config, "TRUST_CLOUDFLARE", True)
    monkeypatch.setattr(config, "TRUST_PROXY_HEADER", True)

    connection = _connection(peer=("10.0.0.7", 1), forwarded="203.0.113.9")

    assert client_ip(connection) == "203.0.113.9"


def test_client_ip_source_describes_the_order(
    monkeypatch: pytest.MonkeyPatch, no_platform_headers: None
) -> None:
    monkeypatch.setattr(config, "TRUST_PROXY_HEADER", False)
    assert limits.client_ip_source() == "socket"

    monkeypatch.setattr(config, "BEHIND_FLY", True)
    monkeypatch.setattr(config, "TRUST_PROXY_HEADER", True)
    monkeypatch.setattr(config, "TRUSTED_PROXY_HOPS", 2)
    assert limits.client_ip_source() == "fly-client-ip,x-forwarded-for[-2],socket"


@pytest.mark.parametrize(
    "env,expected",
    [
        ({}, (1, False, False)),
        (
            {"TRUSTED_PROXY_HOPS": "2", "TRUST_CLOUDFLARE": "true", "FLY_APP_NAME": "hivelane"},
            (2, True, True),
        ),
    ],
)
def test_proxy_settings_are_read_from_the_environment(
    env: dict[str, str], expected: tuple[int, bool, bool]
) -> None:
    settings = config.load_settings(env)

    assert (
        settings.trusted_proxy_hops,
        settings.trust_cloudflare,
        settings.behind_fly,
    ) == expected


def test_trusted_proxy_hops_must_be_positive() -> None:
    with pytest.raises(config.ConfigError) as excinfo:
        config.load_settings({"TRUSTED_PROXY_HOPS": "0"})

    assert "TRUSTED_PROXY_HOPS" in str(excinfo.value)


def test_client_ip_falls_back_to_peer_when_header_is_empty(
    monkeypatch: pytest.MonkeyPatch, no_platform_headers: None
) -> None:
    monkeypatch.setattr(config, "TRUST_PROXY_HEADER", True)

    connection = _connection(peer=("10.0.0.7", 51234), forwarded="   ")

    assert client_ip(connection) == "10.0.0.7"


def test_client_ip_survives_a_missing_peer(
    monkeypatch: pytest.MonkeyPatch, no_platform_headers: None
) -> None:
    monkeypatch.setattr(config, "TRUST_PROXY_HEADER", False)

    assert client_ip(_connection(peer=None)) == limits.UNKNOWN_IP


# --- POST /rooms --------------------------------------------------------------


def test_room_creation_returns_429_with_retry_after(
    monkeypatch: pytest.MonkeyPatch, relay_backend: str
) -> None:
    monkeypatch.setattr(limits.room_creation_limiter, "limit", 2)
    client = TestClient(app)

    assert client.post("/rooms").status_code == 200
    assert client.post("/rooms").status_code == 200

    response = client.post("/rooms")

    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) > 0


def test_a_different_ip_keeps_its_own_budget(
    monkeypatch: pytest.MonkeyPatch, relay_backend: str
) -> None:
    monkeypatch.setattr(config, "TRUST_PROXY_HEADER", True)
    monkeypatch.setattr(limits.room_creation_limiter, "limit", 1)
    client = TestClient(app)

    flooder = {"X-Forwarded-For": "203.0.113.9"}
    assert client.post("/rooms", headers=flooder).status_code == 200
    assert client.post("/rooms", headers=flooder).status_code == 429

    assert (
        client.post("/rooms", headers={"X-Forwarded-For": "198.51.100.4"}).status_code
        == 200
    )


def test_forwarded_header_cannot_buy_extra_budget_when_untrusted(
    monkeypatch: pytest.MonkeyPatch, relay_backend: str
) -> None:
    monkeypatch.setattr(config, "TRUST_PROXY_HEADER", False)
    monkeypatch.setattr(limits.room_creation_limiter, "limit", 1)
    client = TestClient(app)

    first = client.post("/rooms", headers={"X-Forwarded-For": "203.0.113.1"})
    second = client.post("/rooms", headers={"X-Forwarded-For": "203.0.113.2"})

    assert first.status_code == 200
    assert second.status_code == 429


def test_readyz_reports_the_environment() -> None:
    client = TestClient(app)

    body = client.get("/readyz").json()

    assert body["status"] == "ready"
    assert body["env"] == config.ENV
    assert body["cors_origins_configured"] == len(config.CORS_ORIGINS)


# --- WS /ws/{room_id} ---------------------------------------------------------


def test_websocket_attempts_are_refused_after_the_limit(
    monkeypatch: pytest.MonkeyPatch, relay_backend: str
) -> None:
    client = TestClient(app)
    room_id = client.post("/rooms").json()["room_id"]
    monkeypatch.setattr(limits.ws_attempt_limiter, "limit", 1)

    with client.websocket_connect(f"/ws/{room_id}") as first:
        first.send_json({"type": "join", "name": "adrian", "agent": "claude-code"})

    with client.websocket_connect(f"/ws/{room_id}") as refused:
        frame = refused.receive_json()
        with pytest.raises(WebSocketDisconnect) as excinfo:
            refused.receive_json()

    assert frame["type"] == "rate_limited"
    assert frame["reason"]
    assert frame["retry_after"] > 0
    assert excinfo.value.code == WS_CLOSE_RATE_LIMITED


def test_rate_limited_socket_is_refused_before_the_room_lookup(
    monkeypatch: pytest.MonkeyPatch, relay_backend: str
) -> None:
    """An unknown room must not be distinguishable from a known one once the
    caller is throttled — otherwise the limiter leaks a brute-force oracle."""
    # 0 now means unlimited, so exhaust a budget of one instead.
    monkeypatch.setattr(limits.ws_attempt_limiter, "limit", 1)
    limits.ws_attempt_limiter.check("testclient")
    client = TestClient(app)

    with client.websocket_connect("/ws/ZZZZZZ") as refused:
        frame = refused.receive_json()

    assert frame["type"] == "rate_limited"
