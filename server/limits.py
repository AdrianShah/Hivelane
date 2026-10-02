"""Per-IP rate limiting for the unauthenticated relay surface.

By default the counters live in this process. With more than one relay instance
a caller then gets one budget per instance, so the limits are a speed bump
against casual abuse, not a guarantee. Setting SYNERGO_REDIS_URL moves the
counters into Redis so every instance draws on one budget. `redis` is imported
only in that case, so a single-instance deploy needs neither the package
(`pip install redis`) nor the service.

When Redis cannot be reached the limiters fall back to their in-process
counters and log it. Failing closed would let a Redis outage refuse every room
creation and connection; failing fully open would drop the limits exactly when
nobody is watching them.
"""

from __future__ import annotations

import logging
import math
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from starlette.requests import HTTPConnection

try:
    from . import config
except ImportError:  # Render runs with rootDir=server as a flat module tree
    import config

log = logging.getLogger(__name__)

UNKNOWN_IP = "unknown"

# Every limiter key in Redis lives under this prefix, so a shared Redis can be
# inspected or cleared for the relay alone.
REDIS_KEY_PREFIX = "synergo:ratelimit"
# The limiters run on the event loop through a blocking client, so a slow or
# vanished Redis has to cost one short stall rather than hang every request.
REDIS_TIMEOUT_SECONDS = 0.25
# After a failure, stay on the in-process counters this long before trying
# Redis again; otherwise every request pays REDIS_TIMEOUT_SECONDS in an outage.
REDIS_RETRY_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class RateLimitResult:
    allowed: bool
    retry_after_seconds: int


class RateLimiter(Protocol):
    """What the relay needs from a limiter, whichever store holds the counts."""

    limit: int
    window_seconds: float

    def check(self, key: str, now: float | None = None) -> RateLimitResult: ...

    def reset(self) -> None: ...


@dataclass(slots=True)
class _Window:
    started: float
    count: int


class FixedWindowLimiter:
    """In-process fixed-window counter per key, with a bounded key set.

    The key set is capped and evicts least-recently-seen keys first, so a
    flood of distinct source IPs cannot grow the limiter without bound.
    """

    def __init__(self, limit: int, window_seconds: float, max_keys: int) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self.max_keys = max_keys
        self._windows: OrderedDict[str, _Window] = OrderedDict()

    @property
    def tracked_keys(self) -> int:
        return len(self._windows)

    def reset(self) -> None:
        self._windows.clear()

    def check(self, key: str, now: float | None = None) -> RateLimitResult:
        """Count one attempt against `key` and say whether it is allowed."""
        moment = time.monotonic() if now is None else now

        window = self._windows.get(key)
        if window is None or moment - window.started >= self.window_seconds:
            window = _Window(started=moment, count=0)
            self._windows[key] = window
        self._windows.move_to_end(key)

        while len(self._windows) > self.max_keys:
            self._windows.popitem(last=False)

        if window.count >= self.limit:
            remaining = self.window_seconds - (moment - window.started)
            return RateLimitResult(False, max(1, math.ceil(remaining)))

        window.count += 1
        return RateLimitResult(True, 0)


class RedisCounterStore:
    """Window counters in Redis, shared by every relay instance.

    Holds the one client and the outage state every limiter consults, so an
    outage is logged once and probed once per REDIS_RETRY_SECONDS, not once per
    limiter per request.
    """

    def __init__(
        self,
        client: Any,
        *,
        prefix: str = REDIS_KEY_PREFIX,
        retry_seconds: float = REDIS_RETRY_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.client = client
        self.prefix = prefix
        self.retry_seconds = retry_seconds
        self._clock = clock
        # Set while Redis is considered down: when to probe it again.
        self._down_until: float | None = None

    @property
    def available(self) -> bool:
        return self._down_until is None

    def hit(self, key: str, window_ms: int) -> tuple[int, int] | None:
        """Count one attempt against `key`.

        Returns (attempts in the current window, ms until it ends), or None
        while Redis is unavailable. The window opens on the first hit and gets
        its expiry in the same transaction, so Redis's clock — not each
        instance's — decides when it ends, and no crash between commands can
        leave a counter that never expires.
        """
        if self._down_until is not None and self._clock() < self._down_until:
            return None
        name = f"{self.prefix}:{key}"
        try:
            pipe = self.client.pipeline(transaction=True)
            pipe.set(name, 0, nx=True, px=window_ms)
            pipe.incr(name)
            pipe.pttl(name)
            _, count, ttl_ms = pipe.execute()
            if ttl_ms < 0:
                # Written without an expiry by something else; without one it
                # would throttle this key forever.
                self.client.pexpire(name, window_ms)
                ttl_ms = window_ms
        # The client is imported lazily, so its exception types cannot be named
        # here — and no limiter failure may turn into a 500 on the relay.
        except Exception as exc:
            self._mark_down(exc)
            return None
        if self._down_until is not None:
            self._down_until = None
            log.info("Redis rate-limit store reachable again; limits are shared")
        return int(count), int(ttl_ms)

    def clear(self, namespace: str) -> None:
        """Drop every counter under `namespace`.

        For tests and operators: on a shared Redis this resets the budget for
        every relay instance, not just this one.
        """
        try:
            keys = list(self.client.scan_iter(match=f"{self.prefix}:{namespace}:*"))
            if keys:
                self.client.delete(*keys)
        except Exception as exc:
            log.warning("Could not clear Redis rate-limit counters: %s", exc)

    def _mark_down(self, exc: Exception) -> None:
        first_failure = self._down_until is None
        self._down_until = self._clock() + self.retry_seconds
        if first_failure:
            log.warning(
                "Redis rate-limit store unreachable (%s); falling back to "
                "per-process limits and retrying every %.0fs",
                exc,
                self.retry_seconds,
            )


class RedisFixedWindowLimiter:
    """FixedWindowLimiter semantics with the counters held in Redis.

    While Redis is unreachable the in-process `fallback` enforces the same
    budget, per instance, until Redis answers again.
    """

    def __init__(
        self, store: RedisCounterStore, *, name: str, fallback: FixedWindowLimiter
    ) -> None:
        self.store = store
        self.name = name
        self.fallback = fallback
        self.limit = fallback.limit
        self.window_seconds = fallback.window_seconds

    def check(self, key: str, now: float | None = None) -> RateLimitResult:
        """Count one attempt against `key` and say whether it is allowed.

        `now` only drives the fallback: Redis keeps time for the shared
        windows, so instances whose clocks drift still agree on them.
        """
        window_ms = max(1, math.ceil(self.window_seconds * 1000))
        hit = self.store.hit(f"{self.name}:{key}", window_ms)
        if hit is None:
            # Re-synced on every use so a changed budget applies on both paths.
            self.fallback.limit = self.limit
            self.fallback.window_seconds = self.window_seconds
            return self.fallback.check(key, now)

        count, ttl_ms = hit
        if count > self.limit:
            return RateLimitResult(False, max(1, math.ceil(ttl_ms / 1000)))
        return RateLimitResult(True, 0)

    def reset(self) -> None:
        self.store.clear(self.name)
        self.fallback.reset()


def redis_client(url: str) -> Any | None:
    """A Redis client for `url`, or None to keep the limiters in process.

    Opens no connection: this runs at import, before logging is configured and
    possibly before Redis is up. Reachability is handled per call by
    RedisCounterStore.
    """
    try:
        import redis
        from redis.backoff import NoBackoff
        from redis.retry import Retry
    except ImportError:
        log.warning(
            "SYNERGO_REDIS_URL is set but the redis package is not installed "
            "(pip install redis); rate limits stay per-process"
        )
        return None
    try:
        return redis.Redis.from_url(
            url,
            socket_connect_timeout=REDIS_TIMEOUT_SECONDS,
            socket_timeout=REDIS_TIMEOUT_SECONDS,
            # redis-py retries with backoff by default; on the event loop that
            # turns one timeout into several.
            retry=Retry(NoBackoff(), 0),
        )
    except ValueError as exc:
        log.warning(
            "SYNERGO_REDIS_URL could not be used (%s); rate limits stay per-process",
            exc,
        )
        return None


def build_limiter(
    name: str,
    *,
    limit: int,
    window_seconds: float,
    max_keys: int,
    store: RedisCounterStore | None,
) -> RateLimiter:
    memory = FixedWindowLimiter(
        limit=limit, window_seconds=window_seconds, max_keys=max_keys
    )
    if store is None:
        return memory
    return RedisFixedWindowLimiter(store, name=name, fallback=memory)


_client = redis_client(config.REDIS_URL) if config.REDIS_URL else None
redis_store: RedisCounterStore | None = (
    RedisCounterStore(_client) if _client is not None else None
)
# Which store the limiters were built on: "redis" or "memory".
BACKEND = "redis" if redis_store is not None else "memory"

room_creation_limiter: RateLimiter = build_limiter(
    "room_create",
    limit=config.ROOM_CREATE_LIMIT,
    window_seconds=config.ROOM_CREATE_WINDOW_SECONDS,
    max_keys=config.RATE_LIMIT_MAX_TRACKED_IPS,
    store=redis_store,
)

ws_attempt_limiter: RateLimiter = build_limiter(
    "ws_attempt",
    limit=config.WS_ATTEMPT_LIMIT,
    window_seconds=config.WS_ATTEMPT_WINDOW_SECONDS,
    max_keys=config.RATE_LIMIT_MAX_TRACKED_IPS,
    store=redis_store,
)


def client_ip(connection: HTTPConnection) -> str:
    """Best-known client IP for a request or WebSocket.

    X-Forwarded-For is only consulted when TRUST_PROXY_HEADER is on; any client
    can set that header, so trusting it without a proxy in front would let a
    caller rotate its apparent IP and skip every limit here.
    """
    if config.TRUST_PROXY_HEADER:
        forwarded = connection.headers.get("x-forwarded-for", "")
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    client = connection.client
    if client is None or not client.host:
        return UNKNOWN_IP
    return client.host


def check_room_creation(connection: HTTPConnection) -> RateLimitResult:
    return room_creation_limiter.check(client_ip(connection))


def check_ws_attempt(connection: HTTPConnection) -> RateLimitResult:
    return ws_attempt_limiter.check(client_ip(connection))
