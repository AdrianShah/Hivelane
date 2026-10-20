"""Which relay instance owns each room, so several instances can serve one deploy.

A room's state (its members' sockets, recent touches, plans) lives in the
memory of one relay process, and a pre-edit check is only right if every member
of the room reaches that process. So rooms are not shared between instances:
each one is pinned to a single instance. The first instance a member reaches
claims a lease on the room, renews it while the room is live, and releases it
when the room is evicted or the instance shuts down.

- MemoryRoomDirectory, the default: one instance, which owns every room.
- RedisRoomDirectory, when HIVELANE_REDIS_URL is set: the lease is the Redis key
  `hivelane:room:<code>:owner`, holding the owner's instance id. It is set only
  if absent and expires after LEASE_TTL_SECONDS, so a crashed instance's rooms
  free themselves without anyone cleaning up.

RoomAffinityMiddleware acts on the lease before the WebSocket route runs. On
Fly.io it answers an upgrade for a room another instance owns with a
`fly-replay: instance=<owner>` response. Fly's proxy then replays the original
request to that machine, which performs the upgrade itself. Elsewhere it logs a
warning and serves the room locally. Without Fly, keeping a room's members on
one instance is the load balancer's job: hash on the `/ws/<code>` path (see
docs/self-hosting.md).

Fly's replay headers are not trusted as sent. Fly's docs (docs.fly.io,
"Dynamic request routing") say what the proxy adds on a replay (fly-replay-src:
instance, region, t, state) and on a fallback (fly-replay-failed: instance,
app, region, replay_source, reason, elapsed_ms), but not that it strips those
headers from client requests, so a client can send them itself. Hence:

- every replay this instance issues carries `state=<t>.<mac>`, an HMAC over
  the room code and time keyed by a secret every instance shares
  (HIVELANE_REPLAY_SECRET, else one derived from HIVELANE_REDIS_URL, which every
  instance sharing leases already has). fly-replay-src is honoured ("already
  replayed once: serve it here") only with a valid, fresh state; otherwise
  the request is routed as if the header were absent.
- fly-replay-failed never moves a lease by itself: its replay_source must be
  this instance, and the owner must fail a direct health probe over Fly's
  private network (<owner>.vm.<app>.internal) before take_over runs. Take-overs
  are also limited to one per room per TAKE_OVER_INTERVAL_SECONDS. A forged
  header can at most get its own sender served here (splitting itself off).

With a shared directory, routing calls Redis; the middleware runs those calls
in a worker thread so a slow Redis never stalls the event loop.

When Redis cannot be reached, every instance acts as the owner of the rooms it
is asked for. Two teammates may then land on different halves of a room until
Redis is back; refusing connections instead would turn a Redis outage into a
relay outage. Pre-edit checks fail open either way.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import logging
import os
import re
import socket
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, MutableMapping
from dataclasses import dataclass
from typing import Any, Optional, Protocol, TypeVar

log = logging.getLogger(__name__)

REDIS_URL_ENV = "HIVELANE_REDIS_URL"
INSTANCE_ID_ENV = "HIVELANE_INSTANCE_ID"
# Set by Fly.io on every machine. The machine id is also what fly-replay's
# `instance=` field takes, which is why it wins over HIVELANE_INSTANCE_ID.
FLY_MACHINE_ID_ENV = "FLY_MACHINE_ID"
FLY_APP_NAME_ENV = "FLY_APP_NAME"

# A lease outlives two missed renewals, so one slow Redis round trip or a
# stalled event loop does not hand a live room to another instance.
LEASE_TTL_SECONDS = 30.0
RENEW_INTERVAL_SECONDS = 10.0
OWNER_KEY_TEMPLATE = "hivelane:room:{code}:owner"

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

# Shared key for the `state` on replays (see the module docstring).
REPLAY_SECRET_ENV = "HIVELANE_REPLAY_SECRET"
# A replay's state is accepted for this long after it was issued.
REPLAY_STATE_MAX_AGE_SECONDS = 30.0
# At most one take-over per room per this long, however many fallbacks arrive.
TAKE_OVER_INTERVAL_SECONDS = 30.0
# How long the owner's health probe over Fly's private network may take.
OWNER_PROBE_TIMEOUT_SECONDS = 1.0
# Room codes with a recent take-over remembered (bounded; oldest dropped).
_MAX_TAKE_OVERS = 4096

T = TypeVar("T")


def instance_id(env: Mapping[str, str] = os.environ) -> str:
    """This process's name in the directory: Fly's machine id, an explicit
    HIVELANE_INSTANCE_ID, or hostname-pid (unique per process on one host)."""
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
    """Redis leases when HIVELANE_REDIS_URL is set (and usable), else memory."""
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


def replay_secret(env: Mapping[str, str] = os.environ) -> Optional[bytes]:
    """The key replay states are signed with, shared by every instance:
    HIVELANE_REPLAY_SECRET, or one derived from HIVELANE_REDIS_URL (the instances
    that replay to each other all share it, and it carries Redis's password)."""
    explicit = (env.get(REPLAY_SECRET_ENV) or "").strip()
    if explicit:
        return hashlib.sha256(b"hivelane-replay-secret\0" + explicit.encode()).digest()
    url = (env.get(REDIS_URL_ENV) or "").strip()
    if url:
        return hashlib.sha256(b"hivelane-replay-redis\0" + url.encode()).digest()
    return None


def parse_fly_fields(value: Optional[str]) -> dict[str, str]:
    """"a=1;b=2" (fly-replay-src, fly-replay-failed) as a dict; {} for
    anything else."""
    fields: dict[str, str] = {}
    for part in (value or "").split(";"):
        key, sep, val = part.strip().partition("=")
        if sep and key:
            fields[key.strip().lower()] = val.strip()
    return fields


def sign_replay_state(secret: bytes, code: str, now: float) -> str:
    stamp = str(int(now))
    mac = hmac.new(secret, f"{code}|{stamp}".encode(), hashlib.sha256).hexdigest()[:32]
    return f"{stamp}.{mac}"


def replay_state_valid(secret: bytes, code: str, state: str, now: float) -> bool:
    stamp, _, mac = state.partition(".")
    if not stamp.isdigit() or not mac:
        return False
    if abs(now - int(stamp)) > REPLAY_STATE_MAX_AGE_SECONDS:
        return False
    expected = hmac.new(secret, f"{code}|{stamp}".encode(), hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(expected, mac)


OwnerProbe = Callable[[str], Awaitable[bool]]


def fly_owner_probe(env: Mapping[str, str] = os.environ) -> Optional[OwnerProbe]:
    """On Fly: a check that machine `owner` answers /health over the private
    network. None elsewhere (fly-replay-failed is then never acted on)."""
    app = (env.get(FLY_APP_NAME_ENV) or "").strip()
    if not app:
        return None
    port = (env.get("PORT") or "8000").strip() or "8000"

    async def reachable(owner: str) -> bool:
        if not re.fullmatch(r"[0-9a-zA-Z_-]{1,64}", owner):
            return False
        try:
            import httpx
        except ImportError:  # pragma: no cover - httpx ships with the relay
            return False
        url = f"http://{owner}.vm.{app}.internal:{port}/health"
        try:
            async with httpx.AsyncClient(
                timeout=OWNER_PROBE_TIMEOUT_SECONDS, follow_redirects=False, trust_env=False
            ) as client:
                await client.get(url)
            return True  # any HTTP answer: the machine is up
        except Exception:
            return False

    return reachable


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
        *,
        secret: Optional[bytes] = None,
        owner_probe: Optional[OwnerProbe] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.app = app
        self._router = router
        self.fly_replay = fly_replay_enabled() if fly_replay is None else fly_replay
        self.secret = replay_secret() if secret is None else secret
        self.owner_probe = fly_owner_probe() if owner_probe is None else owner_probe
        self._clock = clock
        self._warned: set[tuple[str, str]] = set()
        # Room code -> when this instance last took it over.
        self._took_over: dict[str, float] = {}

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
        # A shared directory means Redis round trips: off the event loop.
        offload = bool(getattr(router, "shares_rooms", False))
        try:
            if offload:
                route = await asyncio.to_thread(router.route, raw_code)
            else:
                route = router.route(raw_code)
        except Exception:
            log.exception("Routing a connection for room %r failed; serving it here", raw_code[:16])
            return False
        if route.local:
            return False

        failed = _header(scope, b"fly-replay-failed")
        if failed is not None:
            # Fly says it sent the request back: the owner is gone or not
            # answering. Serving the room here only helps if the next teammate
            # comes here too, so the lease moves with it, but only once the
            # owner is confirmed unreachable: a client can send this header.
            moved = False
            if await self._may_take_over(route, failed, router):
                if offload:
                    moved = await asyncio.to_thread(router.take_over, route.code, route.owner)
                else:
                    moved = router.take_over(route.code, route.owner)
                if moved:
                    self._note_take_over(route.code)
            log.warning(
                "Replay to instance %s for room %s came back; serving it here%s",
                route.owner,
                route.code,
                " and taking it over" if moved else "",
            )
            return False
        src = _header(scope, b"fly-replay-src")
        if src is not None and self._replayed_by_us(route.code, src):
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
        # prefer_self: if the owner cannot be reached, Fly sends the request
        # back here with fly-replay-failed set. state: proves to the target
        # that this replay came from an instance of ours (see _replayed_by_us).
        value = f"instance={route.owner};fallback=prefer_self"
        if self.secret is not None:
            value += f";state={sign_replay_state(self.secret, route.code, self._clock())}"
        await send(
            {
                "type": "websocket.http.response.start",
                "status": FLY_REPLAY_STATUS,
                "headers": [(b"fly-replay", value.encode("latin-1"))],
            }
        )
        await send({"type": "websocket.http.response.body", "body": b""})
        return True

    def _replayed_by_us(self, code: str, header: str) -> bool:
        """True when fly-replay-src carries a state one of our instances
        signed for this room just now. Without a shared secret (no Redis,
        no HIVELANE_REPLAY_SECRET) nothing can be checked, and the header is
        taken as sent: such a deploy has a single instance and never replays."""
        if self.secret is None:
            return True
        state = parse_fly_fields(header).get("state", "")
        if replay_state_valid(self.secret, code, state, self._clock()):
            return True
        log.warning("Ignoring a fly-replay-src header for room %s with no valid state", code)
        return False

    async def _may_take_over(self, route: RoomRoute, header: str, router: RoomRouter) -> bool:
        fields = parse_fly_fields(header)
        me = getattr(router, "instance_id", None)
        source = fields.get("replay_source")
        if not source or (me is not None and source != me):
            log.warning(
                "Ignoring a fly-replay-failed header for room %s that this instance "
                "did not cause",
                route.code,
            )
            return False
        last = self._took_over.get(route.code)
        if last is not None and self._clock() - last < TAKE_OVER_INTERVAL_SECONDS:
            return False
        if self.owner_probe is None:
            return False
        try:
            if await self.owner_probe(route.owner):
                log.warning(
                    "Room %s: replay came back, but instance %s answers its health "
                    "check; keeping its lease",
                    route.code,
                    route.owner,
                )
                return False
        except Exception:
            log.exception("Probing instance %s failed", route.owner)
            return False
        return True

    def _note_take_over(self, code: str) -> None:
        self._took_over[code] = self._clock()
        while len(self._took_over) > _MAX_TAKE_OVERS:
            self._took_over.pop(next(iter(self._took_over)))

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
