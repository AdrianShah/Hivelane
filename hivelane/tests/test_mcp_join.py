"""The MCP server's room_status and join_room tools: an agent with only MCP
access can see whether the repo is connected and connect it, by running
`hivelane up -y` in a child process (never in the server, whose stdout is
the JSON-RPC channel)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from hivelane import auth, mcp_server
from hivelane.tests.test_fallbacks import DOWN_TIMEOUT, StubDaemon, call, dead_port  # noqa: F401  (fixture)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    return root.resolve()


class FakeRunner:
    def __init__(self, stub: StubDaemon | None = None, then: dict[str, Any] | None = None, code: int = 0) -> None:
        self.calls: list[tuple[list[str], str, dict[str, str]]] = []
        self.stub = stub
        self.then = then
        self.code = code

    def __call__(self, argv: list[str], cwd: str, env: dict[str, str]) -> tuple[int, str]:
        self.calls.append((argv, cwd, env))
        if self.stub is not None and self.then is not None:
            self.stub.status = self.then  # what the daemon reports once `up` ran
        return self.code, "hivelane: setting up\n\x1b[32m  ok\x1b[0m connected\n"


def _server(client: Any, repo: Path, runner: FakeRunner) -> mcp_server.McpServer:
    return mcp_server.McpServer(client, agent="codex", cwd=str(repo), runner=runner)


def _text(result: dict[str, Any]) -> str:
    assert result["isError"] is False, result
    return result["content"][0]["text"]


def _connected(repo: Path, room: str = "ABCD2345") -> dict[str, Any]:
    return {
        "repos": [
            {"room": room, "relay": "wss://relay.example.com/ws", "connected": True, "root": str(repo), "peers": ["sam"]}
        ]
    }


@pytest.fixture
def stub() -> Any:
    auth.ensure_token()
    s = StubDaemon(status={"repos": []})
    yield s
    s.close()


def test_room_status_with_the_daemon_down(repo: Path, dead_port: int) -> None:  # noqa: F811
    server = _server(mcp_server.DaemonClient(dead_port, timeout=DOWN_TIMEOUT), repo, FakeRunner())
    text = _text(call(server, "room_status"))
    assert "daemon is not running" in text and "join_room" in text


def test_room_status_reports_the_repo(stub: StubDaemon, repo: Path) -> None:
    stub.status = _connected(repo)
    server = _server(mcp_server.DaemonClient(stub.port), repo, FakeRunner())
    text = _text(call(server, "room_status"))
    assert "Room ABCD2345: connected." in text and "sam" in text
    assert "/r/ABCD2345" in text  # dashboard link
    assert "Join Hivelane room ABCD2345 in this repo" in text  # what to hand a teammate


def test_room_status_outside_git(stub: StubDaemon, tmp_path: Path) -> None:
    server = _server(mcp_server.DaemonClient(stub.port), tmp_path, FakeRunner())
    assert "not inside a git repository" in _text(call(server, "room_status", {"root": str(tmp_path)}))


def test_join_room_runs_up_with_the_link(stub: StubDaemon, repo: Path) -> None:
    runner = FakeRunner(stub, then=_connected(repo))
    server = _server(mcp_server.DaemonClient(stub.port), repo, runner)
    link = "https://adrianshah.github.io/Hivelane/j/ABCD2345?relay=wss%3A%2F%2Frelay.example.com%2Fws"
    text = _text(call(server, "join_room", {"room": link, "name": "ada"}))

    argv, cwd, env = runner.calls[0]
    assert argv == ["up", "-y", "--root", str(repo), "--room", link, "--name", "ada"]
    assert cwd == str(repo)
    assert env["HIVELANE_PORT"] == str(stub.port)  # the daemon this server talks to
    assert "Room ABCD2345: connected." in text
    assert "\x1b[" not in text  # colours stripped for the agent


def test_join_room_without_a_code_lets_up_resolve_it(stub: StubDaemon, repo: Path) -> None:
    runner = FakeRunner(stub, then=_connected(repo))
    server = _server(mcp_server.DaemonClient(stub.port), repo, runner)
    _text(call(server, "join_room"))
    assert runner.calls[0][0] == ["up", "-y", "--root", str(repo)]


def test_join_room_starts_the_daemon_when_down(repo: Path, dead_port: int) -> None:  # noqa: F811
    # `up` itself starts the daemon; the tool only has to run it.
    runner = FakeRunner()
    server = _server(mcp_server.DaemonClient(dead_port, timeout=DOWN_TIMEOUT), repo, runner)
    text = _text(call(server, "join_room", {"room": "abcd-2345"}))
    assert runner.calls[0][0][-2:] == ["--room", "abcd-2345"]
    assert "Connected this repo" in text


def test_join_room_will_not_switch_rooms_without_being_told(stub: StubDaemon, repo: Path) -> None:
    stub.status = _connected(repo, room="OLDROOM1")
    runner = FakeRunner()
    server = _server(mcp_server.DaemonClient(stub.port), repo, runner)
    text = _text(call(server, "join_room", {"room": "NEWROOM2"}))
    assert "already in room OLDROOM1" in text and "switch=true" in text
    assert runner.calls == []

    _text(call(server, "join_room", {"room": "NEWROOM2", "switch": True}))
    assert runner.calls[0][0][-2:] == ["--room", "NEWROOM2"]


def test_join_room_rejects_a_bad_link_without_running_up(stub: StubDaemon, repo: Path) -> None:
    runner = FakeRunner()
    server = _server(mcp_server.DaemonClient(stub.port), repo, runner)
    assert "Not joined" in _text(call(server, "join_room", {"room": "https://x.example/nothing"}))
    assert runner.calls == []


def test_join_room_reports_a_failed_up(stub: StubDaemon, repo: Path) -> None:
    runner = FakeRunner(code=2)
    server = _server(mcp_server.DaemonClient(stub.port), repo, runner)
    assert "failed (exit 2)" in _text(call(server, "join_room", {"room": "ABCD2345"}))


def test_run_cli_runs_this_install_in_a_child(tmp_path: Path) -> None:
    import os

    code, output = mcp_server.run_cli(["--version"], str(tmp_path), dict(os.environ))
    assert code == 0 and output.startswith("hivelane ")
    assert sys.executable  # the same interpreter serves MCP and runs `up`
