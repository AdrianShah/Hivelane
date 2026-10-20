"""MCP server, file watcher and git pre-commit hook fallbacks."""

from __future__ import annotations

import io
import json
import shutil
import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from hivelane import githook, mcp_server, watch
from hivelane.cli import main


class StubDaemon:
    def __init__(self, ack: dict[str, Any] | None = None, status: dict[str, Any] | None = None) -> None:
        self.events: list[dict[str, Any]] = []
        self.ack = ack or {"decision": "allow", "reason": "", "others": []}
        self.status = status or {"repos": [{"room": "TEAM42", "connected": True, "root": "/r", "peers": ["bob"]}]}
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def _send(self, obj: Any) -> None:
                body = json.dumps(obj).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                self._send(stub.status)

            def do_POST(self) -> None:
                n = int(self.headers.get("Content-Length", 0))
                stub.events.append(json.loads(self.rfile.read(n)))
                self._send(stub.ack)

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        # A short poll interval: shutdown() otherwise waits up to 0.5 s.
        threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        ).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def stub():
    s = StubDaemon()
    yield s
    s.close()


# Windows retries a refused loopback SYN for ~2 s; a short client timeout
# keeps the daemon-down tests fast (the result is the same DaemonDown).
DOWN_TIMEOUT = 0.25


@pytest.fixture
def dead_port() -> Iterator[int]:
    """A port nothing listens on, held bound (not listening) for the whole
    test so no other process can take it in the meantime."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        yield s.getsockname()[1]


def rpc(server: mcp_server.McpServer, lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = io.StringIO()
    server.serve(io.StringIO("".join(json.dumps(m) + "\n" for m in lines)), out)
    return [json.loads(line) for line in out.getvalue().splitlines()]


def call(server: mcp_server.McpServer, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    reply = rpc(server, [{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args or {}}}])
    return reply[0]["result"]


# --- MCP ---------------------------------------------------------------------


def test_mcp_handshake(stub: StubDaemon, tmp_path: Path) -> None:
    server = mcp_server.McpServer(mcp_server.DaemonClient(stub.port), agent="codex", cwd=str(tmp_path))
    replies = rpc(server, [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "ping"},
        {"jsonrpc": "2.0", "id": 4, "method": "nope"},
    ])
    assert [r["id"] for r in replies] == [1, 2, 3, 4]
    init = replies[0]["result"]
    assert init["protocolVersion"] == "2025-06-18"
    assert init["serverInfo"]["name"] == "hivelane" and "tools" in init["capabilities"]
    assert {t["name"] for t in replies[1]["result"]["tools"]} == {"check_file", "declare_intent", "who_is_here", "report_edit"}
    assert replies[2]["result"] == {}
    assert replies[3]["error"]["code"] == -32601
    unknown = rpc(server, [{"jsonrpc": "2.0", "id": 9, "method": "initialize", "params": {"protocolVersion": "1999-01-01"}}])
    assert unknown[0]["result"]["protocolVersion"] == mcp_server.PROTOCOL_VERSION


def test_mcp_tools(stub: StubDaemon, tmp_path: Path) -> None:
    server = mcp_server.McpServer(mcp_server.DaemonClient(stub.port), agent="codex", cwd=str(tmp_path))
    assert "OK" in call(server, "check_file", {"path": "a.py"})["content"][0]["text"]
    assert stub.events[-1]["kind"] == "pre_edit" and stub.events[-1]["file_path"] == str(tmp_path.resolve() / "a.py")
    assert stub.events[-1]["agent"] == "codex"

    stub.ack = {"decision": "block", "reason": "bob is editing a.py", "others": [
        {"user_id": "bob", "agent": "cursor", "seconds_ago": 4, "prompt": "fix login"}]}
    text = call(server, "check_file", {"path": "a.py"})["content"][0]["text"]
    assert "BLOCK" in text and "bob" in text and "fix login" in text
    stub.ack = {"decision": "allow", "others": []}

    call(server, "declare_intent", {"text": "refactor auth", "files": ["a.py", "b.py"]})
    assert stub.events[-2]["kind"] == "intent"
    assert stub.events[-2]["prompt"] == "refactor auth\nFiles: a.py, b.py"
    # With files, the intent is also sent as a plan.
    assert stub.events[-1]["kind"] == "plan"
    assert stub.events[-1]["summary"] == "refactor auth"
    assert stub.events[-1]["files"] == ["a.py", "b.py"]

    (tmp_path / "a.py").write_text("x = 1\n", newline="")
    call(server, "report_edit", {"path": "a.py"})
    assert stub.events[-1]["kind"] == "post_edit" and stub.events[-1]["new_content"] == "x = 1\n"

    text = call(server, "who_is_here")["content"][0]["text"]
    assert "TEAM42" in text and "connected" in text and "bob" in text


def test_mcp_daemon_down_is_tool_error(tmp_path: Path, dead_port: int) -> None:
    client = mcp_server.DaemonClient(dead_port, timeout=DOWN_TIMEOUT)
    server = mcp_server.McpServer(client, cwd=str(tmp_path))
    result = call(server, "check_file", {"path": "a.py"})
    assert result["isError"] is True and "not reachable" in result["content"][0]["text"]
    assert call(server, "who_is_here")["isError"] is True


def test_mcp_print_helpers(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["mcp", "--print-config", "--agent", "codex"]) == 0
    cfg = json.loads(capsys.readouterr().out)
    assert cfg["mcpServers"]["hivelane"]["args"] == ["mcp", "--agent", "codex"]
    assert main(["mcp", "--agents-md"]) == 0
    out = capsys.readouterr().out
    assert "check_file" in out and "declare_intent" in out


# --- watcher -----------------------------------------------------------------


def test_watch_create_modify_delete(tmp_path: Path) -> None:
    posted: list[dict[str, Any]] = []
    (tmp_path / "keep.txt").write_text("one\n", newline="")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "secret.log").write_text("x", newline="")
    w = watch.Watcher(tmp_path, posted.append, agent="aider", ignore=["*.log"])
    w.prime()
    assert w.poll() == []

    (tmp_path / "new.py").write_text("print(1)\n", newline="")
    (tmp_path / "node_modules" / "dep.js").write_text("x", newline="")
    (tmp_path / "secret.log").write_text("changed", newline="")
    (tmp_path / "bin.dat").write_bytes(b"\0\1\2")
    events = w.poll()
    assert [Path(e["file_path"]).name for e in events] == ["new.py"]
    assert events[0]["old_content"] == "" and events[0]["new_content"] == "print(1)\n"
    assert events[0]["kind"] == "post_edit" and events[0]["agent"] == "aider" and events[0]["tool"] == "watch:aider"

    p = tmp_path / "keep.txt"
    p.write_text("one\ntwo\n", newline="")
    import os
    os.utime(p, (1, 1))  # force an mtime change even on coarse clocks
    events = w.poll()
    assert len(events) == 1 and events[0]["old_content"] == "one\n" and events[0]["new_content"] == "one\ntwo\n"

    (tmp_path / "new.py").unlink()
    events = w.poll()
    assert len(events) == 1 and events[0]["old_content"] == "print(1)\n" and events[0]["new_content"] == ""
    assert len(posted) == 3


# --- githook -----------------------------------------------------------------

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def git_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    return tmp_path


@needs_git
def test_githook_chains_and_restores(tmp_path: Path) -> None:
    root = git_repo(tmp_path)
    hooks = root / ".githooks"
    hooks.mkdir()
    subprocess.run(["git", "config", "core.hooksPath", ".githooks"], cwd=root, check=True)
    original = b"#!/usr/bin/env bash\r\necho graphify\n\xe2\x9c\x93\n"
    (hooks / "pre-commit").write_bytes(original)

    assert "chains" in githook.install(root)
    assert githook.is_ours(hooks / "pre-commit")
    assert (hooks / githook.PREV_NAME).read_bytes() == original
    assert "already" in githook.install(root)
    assert (hooks / githook.PREV_NAME).read_bytes() == original

    githook.uninstall(root)
    assert (hooks / "pre-commit").read_bytes() == original
    assert not (hooks / githook.PREV_NAME).exists()
    assert githook.uninstall(root) == "not installed"
    assert (hooks / "pre-commit").read_bytes() == original


@needs_git
def test_githook_fresh_install(tmp_path: Path) -> None:
    root = git_repo(tmp_path)
    githook.install(root)
    hook = githook.hooks_dir(root) / "pre-commit"
    assert githook.is_ours(hook)
    githook.uninstall(root)
    assert not hook.exists()


@needs_git
def test_githook_run_daemon_down(tmp_path: Path, dead_port: int) -> None:
    root = git_repo(tmp_path)
    client = mcp_server.DaemonClient(dead_port, timeout=DOWN_TIMEOUT)
    assert githook.run(root, dead_port, strict=True, client=client) == 0


@needs_git
def test_githook_run_warns(tmp_path: Path) -> None:
    root = git_repo(tmp_path)
    (root / "a.py").write_text("x", newline="")
    subprocess.run(["git", "add", "a.py"], cwd=root, check=True)
    stub = StubDaemon(
        ack={"decision": "warn", "reason": "bob edited a.py", "others": [{"user_id": "bob", "agent": "cursor"}]},
        status={"repos": [{"root": str(root), "peers": ["bob"], "connected": True}]},
    )
    try:
        lines: list[str] = []
        assert githook.run(root, stub.port, say=lines.append) == 0
        assert lines and "a.py" in lines[0] and "bob" in lines[0]
        assert stub.events[0]["kind"] == "pre_edit"
        assert githook.run(root, stub.port, strict=True, say=lines.append) == 1
    finally:
        stub.close()
