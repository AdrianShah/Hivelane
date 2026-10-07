"""Harness for end-to-end tests: the real relay and real daemons, in one
process, talking over loopback sockets exactly as they do across machines.

The relay runs on its own thread and event loop, and the daemons share a second
one, so neither side's loop can mask a stall in the other. Both stay off the
main thread so uvicorn installs no signal handlers into the pytest process.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
import uvicorn
from websockets.sync.client import ClientConnection
from websockets.sync.client import connect as ws_connect

from server import limits
from server.main import app as relay_app
from synergo import auth
from synergo.daemon import Daemon

LOOPBACK = "127.0.0.1"
STARTUP_TIMEOUT = 10.0
SHUTDOWN_TIMEOUT = 10.0


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((LOOPBACK, 0))
        return sock.getsockname()[1]


def wait_until(
    predicate: Callable[[], bool], *, what: str, timeout: float = STARTUP_TIMEOUT
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out after {timeout:.0f}s waiting for {what}")


class Relay:
    """server.main:app under uvicorn, as deployed, on a free loopback port."""

    def __init__(self) -> None:
        self.port = free_port()
        self.server = uvicorn.Server(
            uvicorn.Config(
                relay_app,
                host=LOOPBACK,
                port=self.port,
                log_level="warning",
                access_log=False,
                # Bounds teardown if a connection refuses to close.
                timeout_graceful_shutdown=5,
            )
        )
        self.thread = threading.Thread(
            target=lambda: asyncio.run(self.server.serve()),
            name="e2e-relay",
            daemon=True,
        )

    @property
    def http_url(self) -> str:
        return f"http://{LOOPBACK}:{self.port}"

    @property
    def ws_url(self) -> str:
        return f"ws://{LOOPBACK}:{self.port}/ws"

    def start(self) -> None:
        self.thread.start()
        wait_until(
            lambda: self.server.started or not self.thread.is_alive(),
            what="the relay to start",
        )
        assert self.server.started, "relay failed to start"

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(SHUTDOWN_TIMEOUT)


class DaemonPool:
    """Daemons sharing one event loop on a background thread."""

    def __init__(self, daemons: list[Daemon]) -> None:
        self.daemons = daemons
        self.results: list[Any] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._main: asyncio.Task[Any] | None = None
        self._ready = threading.Event()
        self.thread = threading.Thread(
            target=lambda: asyncio.run(self._run_all()),
            name="e2e-daemons",
            daemon=True,
        )

    async def _run_all(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._main = asyncio.current_task()
        self._ready.set()
        self.results = await asyncio.gather(
            *(daemon.run() for daemon in self.daemons), return_exceptions=True
        )

    def start(self) -> None:
        self.thread.start()
        assert self._ready.wait(STARTUP_TIMEOUT), "daemon loop failed to start"

    def request_stop(self) -> None:
        """Ask every daemon to wind down through its normal shutdown path
        (flush, close the relay socket, stop HTTP) instead of being cancelled.
        Daemon.stop() is safe to call from this, the test's, thread."""
        assert self._loop is not None
        for daemon in self.daemons:
            daemon.stop()

    def join(self) -> None:
        self.thread.join(SHUTDOWN_TIMEOUT)
        if self.thread.is_alive() and self._loop and self._main:
            # Last resort, so a broken shutdown fails the test instead of hanging it.
            self._loop.call_soon_threadsafe(self._main.cancel)
            self.thread.join(SHUTDOWN_TIMEOUT)


class DaemonClient:
    """What an agent hook sees: the daemon's loopback HTTP port."""

    def __init__(self, daemon: Daemon, http: httpx.Client) -> None:
        self.daemon = daemon
        # A multi-repo daemon has no single name: each repo carries its own.
        self.name = getattr(daemon, "name", "")
        self.base = f"http://{LOOPBACK}:{daemon.port}"
        self._http = http

    @property
    def headers(self) -> dict[str, str]:
        # What every hook sends: the install's token (the daemon loads it from
        # this test's private SYNERGO_HOME when it starts serving).
        return {auth.TOKEN_HEADER: self.daemon.ensure_token()}

    def status(self) -> dict[str, Any] | None:
        try:
            return self._http.get(f"{self.base}/status", headers=self.headers).json()
        except httpx.TransportError:
            return None

    def post(self, event: dict[str, Any]) -> dict[str, Any]:
        response = self._http.post(f"{self.base}/events", json=event, headers=self.headers)
        response.raise_for_status()
        return response.json()


class Spectator:
    """A dashboard-style observer of the room: the only outside view of when
    the relay has actually recorded something, rather than merely been sent it."""

    def __init__(self, relay: Relay, room: str) -> None:
        self._socket: ClientConnection = ws_connect(f"{relay.ws_url}/{room}")
        self._socket.send(
            json.dumps({"type": "join", "name": None, "role": "spectator"})
        )

    def wait_for(
        self, predicate: Callable[[dict[str, Any]], bool], *, what: str
    ) -> dict[str, Any]:
        deadline = time.monotonic() + STARTUP_TIMEOUT
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"relay never broadcast {what}")
            try:
                frame = json.loads(self._socket.recv(timeout=remaining))
            except TimeoutError:
                continue
            # Room-wide events arrive batched in a feed; catch-up arrives bare.
            if frame.get("type") == "synergo_feed":
                events = frame.get("events") or []
            else:
                events = [frame]
            for event in events:
                if predicate(event):
                    return event

    def close(self) -> None:
        self._socket.close()


class Cluster:
    """A relay plus any number of daemons, torn down in the only order that
    lets each daemon exit cleanly: flag it, then close its relay connection."""

    def __init__(self) -> None:
        self.relay = Relay()
        self.http = httpx.Client(timeout=5.0, trust_env=False)
        self._pool: DaemonPool | None = None
        self._spectators: list[Spectator] = []

    def create_room(self) -> str:
        response = self.http.post(f"{self.relay.http_url}/rooms")
        response.raise_for_status()
        return response.json()["room_id"]

    def spectate(self, room: str) -> Spectator:
        spectator = Spectator(self.relay, room)
        self._spectators.append(spectator)
        return spectator

    def start_daemons(
        self, room: str, specs: list[dict[str, Any]]
    ) -> list[DaemonClient]:
        """Start one daemon per spec (Daemon keyword arguments plus `name`) and
        wait until every one is connected and sees all the others."""
        assert self._pool is None, "start all daemons in one call"
        daemons = []
        for spec in specs:
            kwargs = dict(spec)
            name = kwargs.pop("name")
            daemons.append(
                Daemon(room, name, server=self.relay.ws_url, port=free_port(), **kwargs)
            )
        clients = self.run_daemons(daemons)
        names = {client.name for client in clients}

        def all_connected() -> bool:
            for client in clients:
                status = client.status()
                if not status or not status["connected"]:
                    return False
                if not names - {client.name} <= set(status["peers"]):
                    return False
            return True

        wait_until(all_connected, what="every daemon to join and see its peers")
        return clients

    def run_daemons(self, daemons: list[Daemon]) -> list[DaemonClient]:
        """Start already-built daemons (any mode) and wait for their HTTP side;
        what "ready" means beyond that is up to the test."""
        assert self._pool is None, "start all daemons in one call"
        self._pool = DaemonPool(daemons)
        self._pool.start()
        clients = [DaemonClient(daemon, self.http) for daemon in daemons]
        wait_until(
            lambda: all(client.status() is not None for client in clients),
            what="every daemon's HTTP endpoint",
        )
        return clients

    def close(self) -> None:
        for spectator in self._spectators:
            spectator.close()
        if self._pool is not None:
            self._pool.request_stop()
        self.relay.stop()
        if self._pool is not None:
            self._pool.join()
        self.http.close()

        assert not self.relay.thread.is_alive(), "relay thread did not stop"
        if self._pool is not None:
            assert not self._pool.thread.is_alive(), "daemon thread did not stop"
            errors = [r for r in self._pool.results if isinstance(r, BaseException)]
            assert not errors, f"daemon run() raised: {errors!r}"
            assert self._pool.results == [0] * len(self._pool.daemons), (
                f"daemons exited uncleanly: {self._pool.results!r}"
            )


@pytest.fixture
def cluster(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Cluster]:
    # No captions from the network: the relay reads the key per call.
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    # The daemons' token (and anything else they keep) stays out of the real
    # home; a test that needs its own home sets it after this.
    monkeypatch.setenv("SYNERGO_HOME", str(tmp_path_factory.mktemp("synergo-home")))
    # The limiters are process-global; earlier tests must not have spent this
    # test's budget for 127.0.0.1.
    limits.room_creation_limiter.reset()
    limits.ws_attempt_limiter.reset()

    cluster = Cluster()
    try:
        cluster.relay.start()
        yield cluster
    finally:
        cluster.close()
