"""Dependency-free per-IP rate limiting for the unauthenticated relay surface.

State lives in this process only. With more than one relay instance a caller
gets one budget per instance, so these limits are a speed bump against casual
abuse — not a guarantee. A shared counter (Redis, or the relay's own store)
is the fix when the relay scales out.
"""

from __future__ import annotations

import math
import time
from collections import OrderedDict
from dataclasses import dataclass

from starlette.requests import HTTPConnection

try:
    from . import config
except ImportError:  # Render runs with rootDir=server as a flat module tree
    import config

UNKNOWN_IP = "unknown"


@dataclass(frozen=True, slots=True)
class RateLimitResult:
    allowed: bool
    retry_after_seconds: int


@dataclass(slots=True)
class _Window:
    started: float
    count: int


class FixedWindowLimiter:
    """Fixed-window counter per key, with a bounded key set.

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


room_creation_limiter = FixedWindowLimiter(
    limit=config.ROOM_CREATE_LIMIT,
    window_seconds=config.ROOM_CREATE_WINDOW_SECONDS,
    max_keys=config.RATE_LIMIT_MAX_TRACKED_IPS,
)

ws_attempt_limiter = FixedWindowLimiter(
    limit=config.WS_ATTEMPT_LIMIT,
    window_seconds=config.WS_ATTEMPT_WINDOW_SECONDS,
    max_keys=config.RATE_LIMIT_MAX_TRACKED_IPS,
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
