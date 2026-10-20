"""Fakes and fixtures for the `synergo up/down/leave/doctor/...` tests: a fake
background service (synergo.service's API), a fake relay behind
httpx.MockTransport, and an isolated machine (home, PATH, git identity)."""

from __future__ import annotations

import io
import threading
import time
from dataclasses import dataclass, field
from email.utils import formatdate
from pathlib import Path
from typing import Any, Callable, Optional

import httpx
import pytest

from synergo import cli, detect
from synergo.protocol import PROTOCOL_VERSION


class FakeService:
    """In-memory stand-in for synergo.service."""

    class ServiceError(Exception):
        pass

    def __init__(self) -> None:
        self.running = False
        self.pid = 4242
        self.repos: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[Any, ...]] = []
        # Merged into each repo entry on connect: what the daemon will report.
        self.outcome: dict[str, Any] = {"connected": True, "peers": ["sam"]}
        self.start_error: Optional[str] = None
        self.stop_result = True
        self.connected = threading.Event()

    # status
    def status(self, port: int, timeout: float = 1.0) -> Optional[dict[str, Any]]:
        if not self.running:
            return None
        return {"version": "0.1.0", "pid": self.pid, "port": port, "repos": list(self.repos.values())}

    def is_running(self, port: int) -> bool:
        return self.running

    # lifecycle
    def start_background(self, port: int, *, wait: float = 8.0) -> int:
        self.calls.append(("start_background", port))
        if self.start_error:
            raise self.ServiceError(self.start_error)
        self.running = True
        return self.pid

    def stop(self, port: int, timeout: float = 5.0) -> bool:
        self.calls.append(("stop", port))
        if self.stop_result:
            self.running = False
        return self.stop_result

    def serve(self, port: int) -> int:
        self.calls.append(("serve", port))
        self.running = True
        self.connected.wait(5)
        # run_foreground's attach thread prints its report after connect_repo
        # returns; wait for that thread to finish rather than for a guess.
        for thread in threading.enumerate():
            if thread.name == "synergo-attach" and thread is not threading.current_thread():
                thread.join(5)
        self.running = False
        return 0

    # repos
    def connect_repo(self, port: int, root: Path, config: Any) -> dict[str, Any]:
        self.calls.append(("connect_repo", port, Path(root), config))
        entry: dict[str, Any] = {
            "root": str(root),
            "room": config.room,
            "relay": config.relay,
            "name": config.name,
            "agent": config.agent,
            "connected": False,
            "last_relay_contact": None,
            "connected_since": None,
            "peers": [],
            "queued_events": 0,
            "repo_id": "abc123",
            "fatal": None,
        }
        entry.update(self.outcome)
        self.repos[str(root)] = entry
        self.connected.set()
        return dict(entry)

    def disconnect_repo(self, port: int, root: Path) -> bool:
        self.calls.append(("disconnect_repo", port, Path(root)))
        return self.repos.pop(str(root), None) is not None

    def log_path(self) -> Path:
        return Path("synergo-home") / "daemon.log"

    # The owner endpoints (POST /rooms/decide, /rooms/policy) go through the
    # raw request helper.
    REGISTER_TIMEOUT = 15.0
    replies: dict[str, Any] = {}

    def _request(self, port: int, method: str, path: str, body: Any = None, timeout: float = 1.0) -> Any:
        self.calls.append(("_request", method, path, body))
        return self.replies.get(path, {})

    @staticmethod
    def _error_detail(exc: Any) -> str:
        return str(exc)

    def install_autostart(self, port: int) -> str:
        self.calls.append(("install_autostart", port))
        return f"autostart installed for port {port}"

    def uninstall_autostart(self) -> str:
        self.calls.append(("uninstall_autostart",))
        return "autostart removed"

    def called(self, name: str) -> list[tuple[Any, ...]]:
        return [call for call in self.calls if call[0] == name]


@dataclass
class FakeRelay:
    """Answers POST /rooms, GET /health and GET /readyz like server/main.py."""

    rooms: list[str] = field(default_factory=lambda: ["K7Q2XM", "P3RT8Z"])
    # Returned with a created room when set, as a relay with ownership does.
    owner_token: Optional[str] = None
    create_status: int = 200
    create_headers: dict[str, str] = field(default_factory=dict)
    health_status: int = 200
    readyz: dict[str, Any] = field(
        default_factory=lambda: {
            "status": "ready",
            "protocol_version": PROTOCOL_VERSION,
            "min_supported_protocol": 1,
        }
    )
    clock_offset: float = 0.0
    error: Optional[Callable[[httpx.Request], Exception]] = None
    requests: list[httpx.Request] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.error is not None:
            raise self.error(request)
        headers = {"date": formatdate(time.time() + self.clock_offset, usegmt=True)}
        path = request.url.path
        if request.method == "POST" and path == "/rooms":
            if self.create_status != 200:
                return httpx.Response(
                    self.create_status,
                    json={"detail": "nope"},
                    headers={**headers, **self.create_headers},
                )
            body: dict[str, Any] = {"room_id": self.rooms.pop(0)}
            if self.owner_token:
                body["owner_token"] = self.owner_token
            return httpx.Response(200, json=body, headers=headers)
        if request.method == "GET" and path == "/health":
            return httpx.Response(self.health_status, json={"status": "ok", "rooms": 1}, headers=headers)
        if request.method == "GET" and path == "/readyz":
            return httpx.Response(200, json=self.readyz, headers=headers)
        return httpx.Response(404, headers=headers)

    def urls(self, method: Optional[str] = None) -> list[str]:
        return [str(r.url) for r in self.requests if method is None or r.method == method]


@dataclass
class Machine:
    home: Path
    repo: Path
    executables: dict[str, str]
    service: FakeService
    relay: FakeRelay
    git_name: Optional[str] = "Ada Lovelace"
    os_name: Optional[str] = "osuser"

    def settings(self) -> Path:
        return self.repo / ".claude" / "settings.json"

    def project_file(self) -> Path:
        return self.repo / ".synergo.toml"


@pytest.fixture
def machine(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Machine:
    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    service = FakeService()
    relay = FakeRelay()
    m = Machine(home=home, repo=repo, executables={}, service=service, relay=relay)

    for var in ("SYNERGO_RELAY", "SYNERGO_NAME", "SYNERGO_PORT", cli.DASHBOARD_ENV):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("SYNERGO_HOME", str(home / ".synergo"))
    monkeypatch.setattr(detect, "home", lambda: home)
    monkeypatch.setattr(detect, "which", lambda name: m.executables.get(name))
    monkeypatch.setattr(cli, "git_user_name", lambda root: m.git_name)
    monkeypatch.setattr(cli, "os_user_name", lambda: m.os_name)
    monkeypatch.setattr(cli, "load_service", lambda: service)
    monkeypatch.setattr(
        cli,
        "http_client",
        lambda timeout=cli.HTTP_TIMEOUT: httpx.Client(
            transport=httpx.MockTransport(relay.handler), timeout=timeout
        ),
    )
    monkeypatch.setattr(cli, "CONNECT_WAIT", 0.3)
    monkeypatch.setattr(cli, "POLL_INTERVAL", 0.01)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))  # not a TTY: never prompt
    monkeypatch.chdir(repo)
    return m
