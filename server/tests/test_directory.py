"""Several relay instances: room leases, routing upgrades to a room's owner,
and the lease upkeep that runs for the app's lifespan.

Redis is a FakeRedis (no server, no extra test dependency) with a steppable
clock, so lease expiry is tested without sleeping."""

from __future__ import annotations

import asyncio
import logging
import socket
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse

try:
    from server.directory import (
        FLY_REPLAY_STATUS,
        LEASE_TTL_SECONDS,
        MemoryRoomDirectory,
        RedisRoomDirectory,
        RoomAffinityMiddleware,
        RoomRoute,
        build_room_directory,
        instance_id,
        maintain_leases,
    )
    from server.room_manager import RENEW_GRACE_SECONDS, RoomManager
    from server.store import RedisRoomStore
    from server.tests.fake_redis import Clock, FakeRedis
except ImportError:  # Render rootDir=server
    from directory import (
        FLY_REPLAY_STATUS,
        LEASE_TTL_SECONDS,
        MemoryRoomDirectory,
        RedisRoomDirectory,
        RoomAffinityMiddleware,
        RoomRoute,
        build_room_directory,
        instance_id,
        maintain_leases,
    )
    from room_manager import RENEW_GRACE_SECONDS, RoomManager
    from store import RedisRoomStore
    from tests.fake_redis import Clock, FakeRedis


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def redis(clock: Clock) -> FakeRedis:
    return FakeRedis(clock)


def _directory(redis: FakeRedis, name: str, clock: Clock) -> RedisRoomDirectory:
    return RedisRoomDirectory(redis, name, clock=clock)


# --- instance ids ------------------------------------------------------------


def test_instance_id_prefers_the_fly_machine_id() -> None:
    env = {"FLY_MACHINE_ID": "148e21eb", "SYNERGO_INSTANCE_ID": "relay-1"}
    assert instance_id(env) == "148e21eb"


def test_instance_id_takes_an_explicit_name() -> None:
    assert instance_id({"SYNERGO_INSTANCE_ID": " relay-1 "}) == "relay-1"


def test_instance_id_defaults_to_hostname_and_pid() -> None:
    import os

    assert instance_id({}) == f"{socket.gethostname()}-{os.getpid()}"


# --- one instance --------------------------------------------------------------


def test_memory_directory_owns_every_room() -> None:
    directory = MemoryRoomDirectory("solo")

    assert directory.shared is False
    assert directory.claim("ABCDEFGH") == "solo"
    assert directory.renew("ABCDEFGH") is True
    assert directory.owner("ZZZZZZZZ") == "solo"
    directory.release("ABCDEFGH")
    assert directory.claim("ABCDEFGH") == "solo"


def test_build_room_directory_is_memory_without_redis() -> None:
    directory = build_room_directory({"SYNERGO_INSTANCE_ID": "solo"})

    assert isinstance(directory, MemoryRoomDirectory)
    assert directory.instance_id == "solo"


def test_build_room_directory_falls_back_when_redis_is_unusable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    module = sys.modules[build_room_directory.__module__]
    monkeypatch.setattr(module, "connect_redis", lambda url, purpose: None)

    directory = build_room_directory({"SYNERGO_REDIS_URL": "redis://redis:6379/0"})

    assert isinstance(directory, MemoryRoomDirectory)


# --- leases ----------------------------------------------------------------------


def test_first_claim_wins_and_others_see_the_owner(redis: FakeRedis, clock: Clock) -> None:
    a, b = _directory(redis, "a", clock), _directory(redis, "b", clock)

    assert a.claim("ABCDEFGH") == "a"
    assert b.claim("ABCDEFGH") == "a"
    assert a.claim("ABCDEFGH") == "a"
    assert b.owner("ABCDEFGH") == "a"
    assert redis.strings["synergo:room:ABCDEFGH:owner"] == "a"


def test_lease_expires_without_renewal_and_another_instance_takes_it(
    redis: FakeRedis, clock: Clock
) -> None:
    a, b = _directory(redis, "a", clock), _directory(redis, "b", clock)
    a.claim("ABCDEFGH")

    clock.advance(LEASE_TTL_SECONDS - 1)
    assert b.claim("ABCDEFGH") == "a"

    clock.advance(2)
    assert b.claim("ABCDEFGH") == "b"
    # The old owner finds out on its next renewal.
    assert a.renew("ABCDEFGH") is False
    assert a.claim("ABCDEFGH") == "b"


def test_renewal_keeps_a_live_room_past_the_ttl(redis: FakeRedis, clock: Clock) -> None:
    a, b = _directory(redis, "a", clock), _directory(redis, "b", clock)
    a.claim("ABCDEFGH")

    for _ in range(10):
        clock.advance(10)
        assert a.renew("ABCDEFGH") is True

    assert b.claim("ABCDEFGH") == "a"


def test_renew_retakes_a_lapsed_lease_nobody_claimed(redis: FakeRedis, clock: Clock) -> None:
    a = _directory(redis, "a", clock)
    a.claim("ABCDEFGH")
    clock.advance(LEASE_TTL_SECONDS + 1)
    assert a.owner("ABCDEFGH") is None

    assert a.renew("ABCDEFGH") is True
    assert a.owner("ABCDEFGH") == "a"


def test_release_only_drops_our_own_lease(redis: FakeRedis, clock: Clock) -> None:
    a, b = _directory(redis, "a", clock), _directory(redis, "b", clock)
    a.claim("ABCDEFGH")

    b.release("ABCDEFGH")
    assert b.owner("ABCDEFGH") == "a"

    a.release("ABCDEFGH")
    assert b.owner("ABCDEFGH") is None
    assert b.claim("ABCDEFGH") == "b"


def test_take_over_replaces_a_stale_owner_but_not_a_third_one(
    redis: FakeRedis, clock: Clock
) -> None:
    a, b, c = (_directory(redis, name, clock) for name in "abc")
    a.claim("ABCDEFGH")

    assert b.take_over("ABCDEFGH", "a") is True
    assert c.claim("ABCDEFGH") == "b"
    # c believes a is the stale owner, but b holds the room now.
    assert c.take_over("ABCDEFGH", "a") is False
    assert c.owner("ABCDEFGH") == "b"


def test_redis_outage_serves_locally_and_logs_once(
    redis: FakeRedis, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    a = _directory(redis, "a", clock)
    b = RedisRoomDirectory(redis, "b", clock=clock, retry_seconds=5)
    a.claim("ABCDEFGH")
    redis.down = True

    with caplog.at_level(logging.WARNING):
        assert b.claim("ABCDEFGH") == "b"
        assert b.claim("ABCDEFGH") == "b"
        assert b.renew("ABCDEFGH") is True
    assert sum("room directory unreachable" in r.getMessage() for r in caplog.records) == 1
    # Probed again only after the retry interval, not on every call.
    calls = redis.calls
    b.claim("ABCDEFGH")
    assert redis.calls == calls

    redis.down = False
    clock.advance(6)  # past b's retry interval, inside a's lease
    assert b.claim("ABCDEFGH") == "a"


# --- the room manager on top -------------------------------------------------------


class Cluster:
    """Relay instances sharing one Redis, each with its own manager."""

    def __init__(self, redis: FakeRedis, clock: Clock) -> None:
        self.redis = redis
        self.clock = clock

    def manager(self, name: str) -> RoomManager:
        return RoomManager(
            store=RedisRoomStore(self.redis),
            directory=RedisRoomDirectory(self.redis, name, clock=self.clock),
        )


@pytest.fixture
def cluster(redis: FakeRedis, clock: Clock) -> Cluster:
    return Cluster(redis, clock)


def test_a_code_created_on_one_instance_is_routed_to_whoever_claims_it(
    cluster: Cluster,
) -> None:
    a, b = cluster.manager("a"), cluster.manager("b")
    code = a.create_room()

    # The first connection lands on b: b claims the room.
    assert b.route(code) == RoomRoute(code, "b", True)
    assert b.get(code) is not None
    # Later connections reaching a are sent to b.
    assert a.route(code.lower()) == RoomRoute(code, "b", False)
    assert b.route(code) == RoomRoute(code, "b", True)
    assert b.leases_held == 1 and a.leases_held == 0


@pytest.mark.parametrize("code", ["ZZZZZZZZ", "../../etc", "AB"])
def test_unknown_codes_are_left_to_the_route_and_never_claimed(
    cluster: Cluster, code: str
) -> None:
    a = cluster.manager("a")

    route = a.route(code)

    assert route.local is True
    assert a.leases_held == 0
    assert not any(key.endswith(":owner") for key in cluster.redis.strings)


def test_single_instance_routes_everything_locally_without_redis() -> None:
    manager = RoomManager(directory=MemoryRoomDirectory("solo"))
    code = manager.create_room()

    assert manager.route(code) == RoomRoute(code, "solo", True)
    assert manager.shares_rooms is False


def _join(manager: RoomManager, code: str, who: str = "ada") -> Any:
    """What the WebSocket route does for a member: route, revive, add."""
    assert manager.route(code).local
    room = manager.get(code)
    assert room is not None
    sock = object()
    room.add_member(who, sock, "claude-code")
    return room


def test_evicting_an_owned_room_releases_it_and_retires_the_code(cluster: Cluster) -> None:
    a, b = cluster.manager("a"), cluster.manager("b")
    code = a.create_room()
    room = _join(a, code)
    room.remove_member("ada")

    a.cleanup_if_empty(code)

    assert a.leases_held == 0
    assert b.get(code) is None
    # Unknown now, so left to the route to refuse; nobody claims it.
    assert b.route(code) == RoomRoute(code, "b", True)
    assert b.leases_held == 0


def test_a_stray_copy_on_a_non_owner_never_retires_the_code(cluster: Cluster) -> None:
    a, b = cluster.manager("a"), cluster.manager("b")
    code = a.create_room()
    _join(a, code)
    # b served a stray connection (no sticky routing) and its copy emptied.
    assert b.route(code) == RoomRoute(code, "a", False)
    assert b.get(code) is not None

    b.cleanup_if_empty(code)

    assert a.get(code) is not None
    assert cluster.manager("c").get(code) is not None
    assert cluster.manager("c").route(code) == RoomRoute(code, "a", False)


def test_leases_follow_the_sockets(cluster: Cluster, clock: Clock) -> None:
    a, b = cluster.manager("a"), cluster.manager("b")
    busy, abandoned, unused = a.create_room(), a.create_room(), a.create_room()
    a._rooms.clear()  # as after a restart: nothing live until a member connects
    _join(a, busy)
    _join(a, abandoned).remove_member("ada")  # everyone here left
    a.route(unused)  # claimed, but the connection never revived the room

    start = time.monotonic()
    for step in range(1, 7):
        clock.advance(10)
        a.renew_leases(now=start + step * RENEW_GRACE_SECONDS)

    # Past the TTL, the busy room is still a's; the others are free to move.
    assert b.route(busy) == RoomRoute(busy, "a", False)
    assert b.route(abandoned) == RoomRoute(abandoned, "b", True)
    assert b.route(unused) == RoomRoute(unused, "b", True)
    assert a.leases_held == 1


def test_a_room_served_without_the_lease_takes_it_once_the_owner_lets_go(
    cluster: Cluster, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    """No sticky routing: teammates landed on both a and b. When a's copy
    empties, its lease lapses and b, which still has members, takes it."""
    a, b = cluster.manager("a"), cluster.manager("b")
    code = a.create_room()
    room_a = _join(a, code)
    assert b.route(code) == RoomRoute(code, "a", False)
    room_b = b.get(code)
    assert room_b is not None
    room_b.add_member("sam", object(), "codex")

    with caplog.at_level(logging.WARNING):
        b.renew_leases()
        b.renew_leases()
    assert sum("they cannot see each other's edits" in r.getMessage() for r in caplog.records) == 1

    room_a.remove_member("ada")
    a.renew_leases(now=time.monotonic() + RENEW_GRACE_SECONDS)
    b.renew_leases()

    assert b.leases_held == 1
    assert cluster.manager("c").route(code) == RoomRoute(code, "b", False)
    # a's empty copy expires later; the code stays valid for b's members.
    a.cleanup_if_empty(code)
    assert cluster.manager("c").get(code) is not None


def test_release_leases_hands_rooms_over_at_shutdown(cluster: Cluster) -> None:
    a, b = cluster.manager("a"), cluster.manager("b")
    code = a.create_room()
    _join(a, code)

    a.release_leases()

    assert b.route(code) == RoomRoute(code, "b", True)


def test_renew_leases_notices_a_lost_room(
    cluster: Cluster, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    a, b = cluster.manager("a"), cluster.manager("b")
    code = a.create_room()
    _join(a, code)
    clock.advance(LEASE_TTL_SECONDS + 1)  # a stalled past its lease
    assert b.route(code).local

    with caplog.at_level(logging.WARNING):
        a.renew_leases()

    assert a.leases_held == 0
    assert "now owned by another relay instance" in caplog.text


# --- the middleware -------------------------------------------------------------


class FakeRouter:
    def __init__(self, route: RoomRoute) -> None:
        self.next_route = route
        self.instance_id = "here"
        self.routed: list[str] = []
        self.taken_over: list[tuple[str, str]] = []

    def route(self, room_id: str) -> RoomRoute:
        self.routed.append(room_id)
        return self.next_route

    def take_over(self, room_id: str, stale_owner: str) -> bool:
        self.taken_over.append((room_id, stale_owner))
        return True


def _app(router: FakeRouter, *, fly_replay: bool) -> FastAPI:
    app = FastAPI()

    @app.websocket("/ws/{room_id}")
    async def endpoint(socket: WebSocket, room_id: str) -> None:
        await socket.accept()
        await socket.send_json({"served_here": room_id})
        await socket.close()

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    app.add_middleware(RoomAffinityMiddleware, router=lambda: router, fly_replay=fly_replay)
    return app


REMOTE = RoomRoute("ABCDEFGH", "machine-b", False)


def test_on_fly_an_upgrade_for_another_instances_room_is_replayed() -> None:
    router = FakeRouter(REMOTE)
    client = TestClient(_app(router, fly_replay=True))

    with pytest.raises(WebSocketDenialResponse) as denied:
        with client.websocket_connect("/ws/abcd-efgh"):
            pass

    response = denied.value
    assert response.status_code == FLY_REPLAY_STATUS
    assert response.headers["fly-replay"] == "instance=machine-b;fallback=prefer_self"
    assert "sec-websocket-accept" not in response.headers
    assert router.routed == ["abcd-efgh"]


def test_local_rooms_are_served_here() -> None:
    router = FakeRouter(RoomRoute("ABCDEFGH", "here", True))
    client = TestClient(_app(router, fly_replay=True))

    with client.websocket_connect("/ws/ABCDEFGH") as ws:
        assert ws.receive_json() == {"served_here": "ABCDEFGH"}


def test_off_fly_a_remote_room_is_served_here_with_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    router = FakeRouter(REMOTE)
    client = TestClient(_app(router, fly_replay=False))

    with caplog.at_level(logging.WARNING):
        for _ in range(2):
            with client.websocket_connect("/ws/ABCDEFGH") as ws:
                assert ws.receive_json() == {"served_here": "ABCDEFGH"}

    warnings = [r for r in caplog.records if "route /ws/<code> by room code" in r.getMessage()]
    assert len(warnings) == 1


def test_a_request_fly_already_replayed_is_never_replayed_again() -> None:
    router = FakeRouter(REMOTE)
    client = TestClient(_app(router, fly_replay=True))

    with client.websocket_connect(
        "/ws/ABCDEFGH", headers={"fly-replay-src": "instance=machine-a;region=iad;t=1"}
    ) as ws:
        assert ws.receive_json() == {"served_here": "ABCDEFGH"}
    assert router.taken_over == []


def test_a_failed_replay_takes_the_room_over_and_serves_it_here() -> None:
    router = FakeRouter(REMOTE)
    client = TestClient(_app(router, fly_replay=True))

    with client.websocket_connect("/ws/ABCDEFGH", headers={"fly-replay-failed": "machine stopped"}) as ws:
        assert ws.receive_json() == {"served_here": "ABCDEFGH"}
    assert router.taken_over == [("ABCDEFGH", "machine-b")]


def test_http_and_other_paths_pass_through_untouched() -> None:
    router = FakeRouter(REMOTE)
    client = TestClient(_app(router, fly_replay=True))

    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/ws/ABCDEFGH").status_code in (403, 404)
    assert router.routed == []


def test_a_routing_failure_serves_the_room_here() -> None:
    class Broken(FakeRouter):
        def route(self, room_id: str) -> RoomRoute:
            raise RuntimeError("boom")

    client = TestClient(_app(Broken(REMOTE), fly_replay=True))

    with client.websocket_connect("/ws/ABCDEFGH") as ws:
        assert ws.receive_json() == {"served_here": "ABCDEFGH"}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def uvicorn_replaying() -> Iterator[int]:
    """The middleware under a real uvicorn, which is what Fly's proxy talks to."""
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            _app(FakeRouter(REMOTE), fly_replay=True),
            host="127.0.0.1",
            port=port,
            log_level="warning",
            lifespan="off",
        )
    )
    thread = threading.Thread(target=lambda: asyncio.run(server.serve()), daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started
    yield port
    server.should_exit = True
    thread.join(10)


def test_uvicorn_sends_the_replay_as_a_bare_status_line(uvicorn_replaying: int) -> None:
    request = (
        "GET /ws/ABCDEFGH HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{uvicorn_replaying}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n"
    )
    with socket.create_connection(("127.0.0.1", uvicorn_replaying), timeout=5) as conn:
        conn.sendall(request.encode())
        raw = b""
        while b"\r\n\r\n" not in raw:
            chunk = conn.recv(4096)
            if not chunk:
                break
            raw += chunk

    head = raw.split(b"\r\n\r\n", 1)[0].decode("latin-1").lower()
    assert head.startswith(f"http/1.1 {FLY_REPLAY_STATUS} ")
    assert "fly-replay: instance=machine-b;fallback=prefer_self" in head
    # Nothing was negotiated: Fly's proxy replays the request to machine-b.
    assert "sec-websocket-accept" not in head


# --- lease upkeep -------------------------------------------------------------------


class FakeHolder:
    def __init__(self, shares_rooms: bool) -> None:
        self.shares_rooms = shares_rooms
        self.renewals = 0
        self.released = 0

    def renew_leases(self) -> None:
        self.renewals += 1
        if self.renewals == 2:
            raise RuntimeError("one bad tick must not stop the loop")

    def release_leases(self) -> None:
        self.released += 1


def test_maintain_leases_renews_until_shutdown_then_releases() -> None:
    holder = FakeHolder(shares_rooms=True)

    async def run() -> None:
        async with maintain_leases(lambda: holder, interval=0.01):
            await asyncio.sleep(0.2)

    asyncio.run(run())

    assert holder.renewals >= 3
    assert holder.released == 1


def test_maintain_leases_does_nothing_for_a_single_instance() -> None:
    holder = FakeHolder(shares_rooms=False)

    async def run() -> None:
        async with maintain_leases(lambda: holder, interval=0.01):
            await asyncio.sleep(0.05)

    asyncio.run(run())

    assert holder.renewals == 0
    assert holder.released == 1


def test_the_relay_app_runs_lease_upkeep_in_its_lifespan(monkeypatch: pytest.MonkeyPatch) -> None:
    try:
        from server import main
    except ImportError:  # Render rootDir=server
        import main  # type: ignore[no-redef]

    holder: Any = FakeHolder(shares_rooms=False)
    monkeypatch.setattr(main, "room_manager", holder)

    with TestClient(main.app):
        pass

    assert holder.released == 1
