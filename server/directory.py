"""Which relay instance owns each room, so several instances can serve one deploy.

A room's state (its members' sockets, recent touches, plans) lives in the
memory of one relay process, and a pre-edit check is only right if every member
of the room reaches that process. So rooms are not shared between instances:
each one is pinned to a single instance. The first instance a member reaches
claims a lease on the room, renews it while the room is live, and releases it
when the room is evicted or the instance shuts down.

- MemoryRoomDirectory, the default: one instance, which owns every room.
- RedisRoomDirectory, when SYNERGO_REDIS_URL is set: the lease is the Redis key
  `synergo:room:<code>:owner`, holding the owner's instance id. It is set only
  if absent and expires after LEASE_TTL_SECONDS, so a crashed instance's rooms
  free themselves without anyone cleaning up.

RoomAffinityMiddleware acts on the lease before the WebSocket route runs. On
Fly.io it answers an upgrade for a room another instance owns with a
`fly-replay: instance=<owner>` response. Fly's proxy then replays the original
request to that machine, which performs the upgrade itself. Elsewhere it logs a
warning and serves the room locally. Without Fly, keeping a room's members on
one instance is the load balancer's job: hash on the `/ws/<code>` path (see
docs/self-hosting.md).

When Redis cannot be reached, every instance acts as the owner of the rooms it
is asked for. Two teammates may then land on different halves of a room until
Redis is back; refusing connections instead would turn a Redis outage into a
relay outage. Pre-edit checks fail open either way.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import socket
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, MutableMapping
from dataclasses import dataclass
from typing import Any, Optional, Protocol, TypeVar

log = logging.getLogger(__name__)

REDIS_URL_ENV = "SYNERGO_REDIS_URL"
INSTANCE_ID_ENV = "SYNERGO_INSTANCE_ID"
# Set by Fly.io on every machine. The machine id is also what fly-replay's
# `instance=` field takes, which is why it wins over SYNERGO_INSTANCE_ID.
FLY_MACHINE_ID_ENV = "FLY_MACHINE_ID"
FLY_APP_NAME_ENV = "FLY_APP_NAME"

# A lease outlives two missed renewals, so one slow Redis round trip or a
# stalled event loop does not hand a live room to another instance.
LEASE_TTL_SECONDS = 30.0
RENEW_INTERVAL_SECONDS = 10.0
OWNER_KEY_TEMPLATE = "synergo:room:{code}:owner"

# Every call here runs on the event loop through a blocking client, so a slow
# or vanished Redis must cost one short stall, not a hung relay. Same budget
# as the rate limiters (limits.py).
REDIS_TIMEOUT_SECONDS = 0.25
# After a failure, skip Redis for this long before probing it again.
REDIS_RETRY_SECONDS = 30.0

# Fly's own WebSocket replay example (fly-apps/replicache-websocket) answers the
# upgrade with a 101 status line that carries fly-replay and nothing else: no
# Upgrade or Sec-WebSocket-Accept, so no handshake is actually negotiated, which
# is what Fly's docs ask of the replaying side. The proxy consumes the response
# and replays the original request to the target, which does the real upgrade.
FLY_REPLAY_STATUS = 101

# /ws/<code>, as routed by main.websocket_endpoint.
WS_ROOM_PATH = re.compile(r"/ws/(?P<code>[^/]+)/?")

T = TypeVar("T")


def instance_id(env: Mapping[str, str] = os.environ) -> str:
    """This process's name in the directory: Fly's machine id, an explicit
    SYNERGO_INSTANCE_ID, or hostname-pid (unique per process on one host)."""
    for name in (FLY_MACHINE_ID_ENV, INSTANCE_ID_ENV):
        value = (env.get(name) or "").strip()
        if value:
            return value
    return f"{socket.gethostname()}-{os.getpid()}"


def connect_redis(url: str, *, purpose: str) -> Any | None:
    """A Redis client for `url`, or None when the redis package is missing or
    the URL is unusable (the caller then stays per-instance).

    Opens no connection: this runs at import, possibly before Redis is up.
    Reachability is checked per call by RedisGuard.
    """
    try:
        import redis
        from redis.backoff import NoBackoff
        from redis.retry import Retry
    except ImportError:
        log.warning(
            "%s is set but the redis package is not installed (pip install redis); "
            "%s stay per-instance",
            REDIS_URL_ENV,
            purpose,
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
            decode_responses=True,
        )
    except ValueError as exc:
        # The URL is not echoed: it usually carries a password.
        log.warning("%s could not be used (%s); %s stay per-instance", REDIS_URL_ENV, exc, purpose)
        return None


def _text(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


class RedisGuard:
    """Outage bookkeeping for one Redis-backed component.

    Runs a call against Redis, or its fallback while Redis is considered down.
    An outage is logged once, and Redis is probed again every `retry_seconds`
    rather than on every call, so an outage does not add a timeout to every
    request.
    """

    def __init__(
        self,
        what: str,
        *,
        degraded: str,
        retry_seconds: float = REDIS_RETRY_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.what = what
        self.degraded = degraded
        self.retry_seconds = retry_seconds
        self._clock = clock
        self._down_until: Optional[float] = None

    @property
    def available(self) -> bool:
        return self._down_until is None

    def run(self, call: Callable[[], T], fallback: Callable[[], T]) -> T:
        if self._down_until is not None and self._clock() < self._down_until:
            return fallback()
        try:
            result = call()
        # redis is imported lazily, so its exception types cannot be named
        # here, and no storage failure may turn into a 500 on the relay.
        except Exception as exc:
            first_failure = self._down_until is None
            self._down_until = self._clock() + self.retry_seconds
            if first_failure:
                log.warning(
                    "Redis %s unreachable (%s: %s); %s. Retrying every %.0fs",
                    self.what,
                    type(exc).__name__,
                    exc,
                    self.degraded,
                    self.retry_seconds,
                )
            return fallback()
        if self._down_until is not None:
            self._down_until = None
            log.info("Redis %s reachable again", self.what)
        return result


# --- the directory ------------------------------------------------------------


class RoomDirectory(Protocol):
    """Who owns each room. Codes passed in are already normalized."""

    instance_id: str

    @property
    def shared(self) -> bool:
        """True when other instances consult the same directory."""
        ...

    def claim(self, code: str) -> str:
        """Take the lease if nobody holds it; return the owner's instance id,
        which is this instance's when the claim succeeded or it already owned
        the room."""
        ...

    def renew(self, code: str) -> bool:
        """Extend this instance's lease, or retake it if it lapsed and nobody
        else took it. False means another instance owns the room now."""
        ...

    def take_over(self, code: str, stale_owner: str) -> bool:
        """Replace `stale_owner`'s lease with ours (it is known to be
        unreachable). False if a third instance holds the room meanwhile."""
        ...

    def release(self, code: str) -> None:
        """Drop the lease if this instance holds it; a no-op otherwise."""
        ...

    def owner(self, code: str) -> Optional[str]:
        """The current owner, or None when the room is unclaimed."""
        ...


class MemoryRoomDirectory:
    """Single instance: every room is this instance's."""

    shared = False

    def __init__(self, instance: Optional[str] = None) -> None:
        self.instance_id = instance or instance_id()

    def claim(self, code: str) -> str:
        return self.instance_id

    def renew(self, code: str) -> bool:
        return True

    def take_over(self, code: str, stale_owner: str) -> bool:
        return True

    def release(self, code: str) -> None:
        return None

    def owner(self, code: str) -> Optional[str]:
        return self.instance_id


# Each script reads and writes one lease atomically. KEYS[1] is the lease key.
# A missing key reads as false in Lua.

# ARGV: instance id, ttl ms. Returns the owner after the call.
CLAIM_SCRIPT = """
local owner = redis.call('GET', KEYS[1])
if not owner then
  redis.call('SET', KEYS[1], ARGV[1], 'PX', ARGV[2])
  return ARGV[1]
end
if owner == ARGV[1] then
  redis.call('PEXPIRE', KEYS[1], ARGV[2])
end
return owner
"""

# ARGV: instance id, ttl ms. Returns 1 while the lease is ours.
RENEW_SCRIPT = """
local owner = redis.call('GET', KEYS[1])
if owner == ARGV[1] then
  redis.call('PEXPIRE', KEYS[1], ARGV[2])
  return 1
end
if not owner then
  redis.call('SET', KEYS[1], ARGV[1], 'PX', ARGV[2])
  return 1
end
return 0
"""

# ARGV: stale owner, instance id, ttl ms. Returns 1 when the lease is ours.
TAKE_OVER_SCRIPT = """
local owner = redis.call('GET', KEYS[1])
if (not owner) or owner == ARGV[1] or owner == ARGV[2] then
  redis.call('SET', KEYS[1], ARGV[2], 'PX', ARGV[3])
  return 1
end
return 0
"""

# ARGV: instance id. Deletes the lease only if it is ours.
RELEASE_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


class RedisRoomDirectory:
    """Room leases in Redis, shared by every relay instance."""

    shared = True

    def __init__(
        self,
        client: Any,
        instance: str,
        *,
        ttl_seconds: float = LEASE_TTL_SECONDS,
        retry_seconds: float = REDIS_RETRY_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.client = client
        self.instance_id = instance
        self.ttl_ms = max(1, int(ttl_seconds * 1000))
        self._guard = RedisGuard(
            "room directory",
            degraded="every instance serves the rooms it is asked for",
            retry_seconds=retry_seconds,
            clock=clock,
        )

    @staticmethod
    def key(code: str) -> str:
        return OWNER_KEY_TEMPLATE.format(code=code)

    def claim(self, code: str) -> str:
        def call() -> str:
            owner = self.client.eval(CLAIM_SCRIPT, 1, self.key(code), self.instance_id, self.ttl_ms)
            return _text(owner) or self.instance_id

        return self._guard.run(call, lambda: self.instance_id)

    def renew(self, code: str) -> bool:
        return self._guard.run(
            lambda: bool(int(self.client.eval(RENEW_SCRIPT, 1, self.key(code), self.instance_id, self.ttl_ms))),
            lambda: True,
        )

    def take_over(self, code: str, stale_owner: str) -> bool:
        return self._guard.run(
            lambda: bool(
                int(
                    self.client.eval(
                        TAKE_OVER_SCRIPT, 1, self.key(code), stale_owner, self.instance_id, self.ttl_ms
                    )
                )
            ),
            lambda: True,
        )

    def release(self, code: str) -> None:
        self._guard.run(
            lambda: self.client.eval(RELEASE_SCRIPT, 1, self.key(code), self.instance_id),
            lambda: None,
        )

    def owner(self, code: str) -> Optional[str]:
        return self._guard.run(lambda: _text(self.client.get(self.key(code))), lambda: None)


def build_room_directory(env: Mapping[str, str] = os.environ) -> RoomDirectory:
    """Redis leases when SYNERGO_REDIS_URL is set (and usable), else memory."""
    me = instance_id(env)
    url = (env.get(REDIS_URL_ENV) or "").strip()
    if not url:
        return MemoryRoomDirectory(me)
    client = connect_redis(url, purpose="room leases")
    if client is None:
        return MemoryRoomDirectory(me)
    log.info("Room leases kept in Redis; this instance is %s", me)
    return RedisRoomDirectory(client, me)


# --- routing WebSocket upgrades -------------------------------------------------


@dataclass(frozen=True, slots=True)
class RoomRoute:
    """Where a connection for `code` should be served."""

    code: str
    owner: str
    local: bool


class RoomRouter(Protocol):
    """What the middleware needs from the room manager."""

    def route(self, room_id: str) -> RoomRoute: ...

    def take_over(self, room_id: str, stale_owner: str) -> bool: ...


Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

# Distinct (room, owner) pairs warned about; bounded so a flood of codes cannot
# grow it, and cleared when full (a repeated warning is harmless).
_MAX_WARNED = 1024


def _header(scope: Scope, name: bytes) -> Optional[str]:
    for key, value in scope.get("headers") or ():
        if key.lower() == name:
            return value.decode("latin-1")
    return None


def fly_replay_enabled(env: Mapping[str, str] = os.environ) -> bool:
    return bool((env.get(FLY_APP_NAME_ENV) or "").strip())


class RoomAffinityMiddleware:
    """Sends each room's WebSocket upgrades to the instance that owns the room.

    Pure ASGI, so it can answer the upgrade with a plain HTTP response before
    any route accepts it (the WebSocket Denial Response extension, which
    uvicorn implements). Every other request passes straight through.
    """

    def __init__(
        self,
        app: ASGIApp,
        router: Callable[[], RoomRouter],
        fly_replay: Optional[bool] = None,
    ) -> None:
        self.app = app
        self._router = router
        self.fly_replay = fly_replay_enabled() if fly_replay is None else fly_replay
        self._warned: set[tuple[str, str]] = set()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") == "websocket":
            match = WS_ROOM_PATH.fullmatch(scope.get("path") or "")
            if match is not None and await self._replay(scope, receive, send, match.group("code")):
                return
        await self.app(scope, receive, send)

    async def _replay(self, scope: Scope, receive: Receive, send: Send, raw_code: str) -> bool:
        """Answer with fly-replay when another instance should serve this
        connection. False means: serve it here."""
        router = self._router()
        try:
            route = router.route(raw_code)
        except Exception:
            log.exception("Routing a connection for room %r failed; serving it here", raw_code)
            return False
        if route.local:
            return False

        if _header(scope, b"fly-replay-failed") is not None:
            # Fly sent the request back: the owner is gone or not answering.
            # Serving the room here only helps if the next teammate comes here
            # too, so the lease moves with it.
            moved = router.take_over(route.code, route.owner)
            log.warning(
                "Fly could not reach instance %s for room %s; serving it here%s",
                route.owner,
                route.code,
                " and taking it over" if moved else "",
            )
            return False
        if _header(scope, b"fly-replay-src") is not None:
            # Already replayed once (the lease moved in between). Replaying
            # again could bounce the request between machines.
            log.warning(
                "Room %s was replayed here but instance %s owns it; serving it here",
                route.code,
                route.owner,
            )
            return False
        if not self.fly_replay:
            self._warn_unrouted(route)
            return False
        if "websocket.http.response" not in (scope.get("extensions") or {}):
            log.warning(
                "Room %s belongs to instance %s, but this server cannot answer a "
                "WebSocket upgrade with fly-replay; serving it here",
                route.code,
                route.owner,
            )
            return False

        message = await receive()
        if message.get("type") != "websocket.connect":
            return True  # the client went away before the handshake
        log.info("Replaying room %s to instance %s", route.code, route.owner)
        await send(
            {
                "type": "websocket.http.response.start",
                "status": FLY_REPLAY_STATUS,
                "headers": [
                    (
                        b"fly-replay",
                        # prefer_self: if the owner cannot be reached, Fly sends
                        # the request back here with fly-replay-failed set.
                        f"instance={route.owner};fallback=prefer_self".encode("latin-1"),
                    ),
                ],
            }
        )
        await send({"type": "websocket.http.response.body", "body": b""})
        return True

    def _warn_unrouted(self, route: RoomRoute) -> None:
        key = (route.code, route.owner)
        if key in self._warned:
            return
        if len(self._warned) >= _MAX_WARNED:
            self._warned.clear()
        self._warned.add(key)
        log.warning(
            "Room %s belongs to relay instance %s, but this connection reached "
            "instance %s. Serving it here splits the room: route /ws/<code> by "
            "room code at the load balancer (docs/self-hosting.md#scaling-out)",
            route.code,
            route.owner,
            getattr(self._router(), "instance_id", "?"),
        )


# --- lease upkeep ---------------------------------------------------------------


class LeaseHolder(Protocol):
    @property
    def shares_rooms(self) -> bool: ...

    def renew_leases(self) -> None: ...

    def release_leases(self) -> None: ...


@contextlib.asynccontextmanager
async def maintain_leases(
    holder: Callable[[], LeaseHolder],
    *,
    interval: float = RENEW_INTERVAL_SECONDS,
) -> AsyncIterator[None]:
    """For the app's lifespan: renew this instance's room leases every
    `interval` seconds, and release them at shutdown so a redeployed
    instance's rooms move to a live one immediately rather than after the TTL."""

    async def renew_forever() -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                holder().renew_leases()
            except Exception:
                log.exception("Renewing room leases failed; retrying in %.0fs", interval)

    task = asyncio.create_task(renew_forever()) if holder().shares_rooms else None
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        try:
            holder().release_leases()
        except Exception:
            log.exception("Releasing room leases at shutdown failed")
