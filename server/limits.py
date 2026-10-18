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
    A limit of 0 allows everything (and tracks nothing); max_keys of 0 does
    not bound the key set.
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
        if self.limit <= 0:
            return RateLimitResult(True, 0)
        moment = time.monotonic() if now is None else now

        window = self._windows.get(key)
        if window is None or moment - window.started >= self.window_seconds:
            window = _Window(started=moment, count=0)
            self._windows[key] = window
        self._windows.move_to_end(key)

        while self.max_keys > 0 and len(self._windows) > self.max_keys:
            self._windows.popitem(last=False)

        if window.count >= self.limit:
            remaining = self.window_seconds - (moment - window.started)
            return RateLimitResult(False, max(1, math.ceil(remaining)))

        window.count += 1
        return RateLimitResult(True, 0)


class TokenBucket:
    """Steady-rate budget with bursts: `rate_per_minute` tokens refill evenly
    over each minute, up to `capacity` (default: one minute's worth).

    In-process on purpose: it guards one room's fan-out, and a room lives in
    one relay process.
    """

    def __init__(
        self,
        rate_per_minute: float,
        capacity: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.rate_per_second = rate_per_minute / 60.0
        self.capacity = float(rate_per_minute if capacity is None else capacity)
        self._clock = clock
        self._tokens = self.capacity
        self._updated = clock()

    def take(self) -> bool:
        """Spend one token if there is one. False means: drop the message."""
        now = self._clock()
        elapsed = max(0.0, now - self._updated)
        self._updated = now
        self._tokens = min(self.capacity, self._tokens + elapsed * self.rate_per_second)
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False


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
        if self.limit <= 0:  # unlimited: no round trip to Redis
            return RateLimitResult(True, 0)
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


def _forwarded_client(header: str, trusted_hops: int) -> str:
    """The X-Forwarded-For entry written by the outermost proxy we trust.

    Each proxy appends the address it received the connection from, so the
    rightmost `trusted_hops` entries are written by our own proxies and
    everything to their left came from the client, who can put anything there.
    The leftmost entry is therefore the one a caller controls; the entry
    `trusted_hops` from the right is the first address a proxy of ours saw.
    A header shorter than that (fewer proxies than configured) yields its
    leftmost entry, which a trusted proxy still wrote.
    """
    entries = [entry.strip() for entry in header.split(",")]
    entries = [entry for entry in entries if entry]
    if not entries:
        return ""
    return entries[max(0, len(entries) - trusted_hops)]


def client_ip(connection: HTTPConnection) -> str:
    """Best-known client IP for a request or WebSocket.

    In order:
    - Fly-Client-IP when running on Fly.io (FLY_APP_NAME is set), which Fly's
      edge overwrites on every request;
    - CF-Connecting-IP when TRUST_CLOUDFLARE is on, for origins reachable only
      through Cloudflare;
    - with TRUST_PROXY_HEADER, the X-Forwarded-For entry TRUSTED_PROXY_HOPS
      from the right (see _forwarded_client);
    - otherwise the socket peer.

    Every header here is client-settable when no proxy strips it, so each is
    opt-in: trusting one without the matching proxy in front would let a
    caller rotate its apparent IP and skip every limit here.
    """
    headers = connection.headers
    if config.BEHIND_FLY:
        fly = headers.get("fly-client-ip", "").strip()
        if fly:
            return fly
    if config.TRUST_CLOUDFLARE:
        cloudflare = headers.get("cf-connecting-ip", "").strip()
        if cloudflare:
            return cloudflare
    if config.TRUST_PROXY_HEADER:
        forwarded = _forwarded_client(
            headers.get("x-forwarded-for", ""), config.TRUSTED_PROXY_HOPS
        )
        if forwarded:
            return forwarded
    client = connection.client
    if client is None or not client.host:
        return UNKNOWN_IP
    return client.host


def client_ip_source() -> str:
    """Where client_ip looks first, for /readyz and the startup log."""
    sources = []
    if config.BEHIND_FLY:
        sources.append("fly-client-ip")
    if config.TRUST_CLOUDFLARE:
        sources.append("cf-connecting-ip")
    if config.TRUST_PROXY_HEADER:
        sources.append(f"x-forwarded-for[-{config.TRUSTED_PROXY_HOPS}]")
    sources.append("socket")
    return ",".join(sources)


def check_room_creation(connection: HTTPConnection) -> RateLimitResult:
    return room_creation_limiter.check(client_ip(connection))


def check_ws_attempt(connection: HTTPConnection) -> RateLimitResult:
    return ws_attempt_limiter.check(client_ip(connection))
