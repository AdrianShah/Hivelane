"""The stdlib-only hook hot path (hivelane.hook_fast / hivelane.hook_json).

Three promises are checked here: the hook process never imports the daemon
stack (probed per adapter in test_adapter_hook_path.py); its output is identical to the pydantic reference implementation it
replaced; and its hand-rolled HTTP client really talks to the daemon's
FastAPI/uvicorn endpoint.
"""

from __future__ import annotations

import io
import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

import pytest

from hivelane import cli, hook_fast, hook_json
from hivelane.adapters import claude_code
from hivelane.events import EventAck, NormalizedEvent
from hivelane.protocol import PeerTouch

HEAVY = {
    "fastapi",
    "uvicorn",
    "websockets",
    "httpx",
    "pydantic",
    "pydantic_core",
    "starlette",
    "anyio",
}
# Stdlib modules the hook deliberately avoids; each is tens of milliseconds on a
# cold Windows start. A regression here is a latency regression, not a bug.
SLOW_STDLIB = {
    "json",
    "re",
    "enum",
    "typing",
    "argparse",
    "pathlib",
    "socket",
    "urllib",
    "http",
    "email",
    "asyncio",
}


# Sockets behind unused_port(): bound but never listening, and kept open for
# the rest of the session so no other process (or test) can take the port
# between picking it and connecting to it.
_HELD_DEAD_PORTS: list[socket.socket] = []


def unused_port() -> int:
    """A loopback port nothing listens on (a stopped daemon)."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    _HELD_DEAD_PORTS.append(sock)
    return int(sock.getsockname()[1])


# --- a stub daemon -------------------------------------------------------------


@pytest.fixture
def stub_daemon() -> Iterator[tuple[int, dict[str, Any]]]:
    """A plain HTTP server answering /events with `state["ack"]` (JSON)."""
    state: dict[str, Any] = {"ack": {"decision": "allow"}, "events": [], "tokens": []}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            body = self.rfile.read(int(self.headers.get("content-length") or 0))
            state["events"].append(json.loads(body))
            state["tokens"].append(self.headers.get(hook_fast.TOKEN_HEADER))
            reply = json.dumps(state["ack"]).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(reply)))
            self.end_headers()
            self.wfile.write(reply)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    # A short poll interval: shutdown() otherwise waits up to 0.5 s per test.
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    try:
        yield int(server.server_address[1]), state
    finally:
        server.shutdown()
        server.server_close()


def pre_edit_payload(target: Path) -> dict[str, Any]:
    return {
        "session_id": "s1",
        "cwd": str(target.parent),
        "hook_event_name": "PreToolUse",
        "tool_name": "Edit",
        "tool_input": {"file_path": str(target), "old_string": "x", "new_string": "y"},
    }


# --- 1. the hook process never imports the daemon stack -----------------------
# (the subprocess probe runs for every adapter in test_adapter_hook_path.py)


def test_constants_match_the_daemon() -> None:
    from hivelane import auth
    from hivelane.daemon import DEFAULT_PORT

    assert hook_fast.DEFAULT_PORT == DEFAULT_PORT
    assert hook_fast.TOKEN_FILENAME == auth.TOKEN_FILENAME
    assert hook_fast.TOKEN_HEADER == auth.TOKEN_HEADER
    assert set(claude_code.EVENT_FIELDS) == set(NormalizedEvent.model_fields)


# --- 2. identical output to the pydantic reference ----------------------------


def legacy_normalize(payload: dict[str, Any]) -> Optional[NormalizedEvent]:
    """claude_code.normalize as it was before the fast path (pathlib + pydantic),
    kept verbatim as an independent oracle."""
    event_name = payload.get("hook_event_name")
    session_id = str(payload.get("session_id") or "")
    cwd = str(payload.get("cwd") or "")

    if event_name == "UserPromptSubmit":
        prompt = payload.get("prompt") or payload.get("user_prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return None
        return NormalizedEvent(kind="intent", agent="claude-code", session_id=session_id, cwd=cwd, prompt=prompt)

    if event_name not in ("PreToolUse", "PostToolUse"):
        return None
    tool = payload.get("tool_name")
    if tool not in claude_code.EDIT_TOOLS:
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    value = tool_input.get("file_path") or tool_input.get("notebook_path")
    file_path = value if isinstance(value, str) and value else None
    if file_path is None:
        return None

    def read(path: str) -> Optional[str]:
        try:
            return Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    if event_name == "PreToolUse":
        # Phase 3 addition: Edit/MultiEdit old/new strings as edit hints.
        edits = [tool_input] if tool == "Edit" else tool_input.get("edits") if tool == "MultiEdit" else None
        hints = [
            {"old": e["old_string"], "new": e.get("new_string") if isinstance(e.get("new_string"), str) else ""}
            for e in (edits if isinstance(edits, list) else [])
            if isinstance(e, dict) and isinstance(e.get("old_string"), str) and e["old_string"]
        ]
        return NormalizedEvent(
            kind="pre_edit", agent="claude-code", session_id=session_id, cwd=cwd,
            file_path=file_path, old_content=read(file_path), tool=tool, edit_hints=hints or None,
        )
    return NormalizedEvent(
        kind="post_edit", agent="claude-code", session_id=session_id, cwd=cwd,
        file_path=file_path, new_content=read(file_path), tool=tool,
    )


def legacy_render_response(event: NormalizedEvent, ack: EventAck) -> str:
    """claude_code.render_response as it was before the fast path. Not fully
    independent: it was extended in step with the features it checks (Phase
    5's queue position, suggestion and `hivelane wait` hint; the quoted,
    sanitised teammate prompt), written out longhand here rather than
    calling the code under test. The ACKS below exercise every branch."""
    if event.kind != "pre_edit" or ack.decision == "allow":
        return ""
    named = ack.file_path or event.file_path
    path = named or "this file"
    detail = ack.reason or f"a teammate's agent is already working in {path}"
    if len(ack.others) > 1:
        extra = len(ack.others) - 1
        detail += f" (and {extra} other{'s' if extra > 1 else ''})"
    if ack.decision == "block":
        steps = [f"you're #{ack.queue_position} in line"] if ack.queue_position else []
        steps.append(ack.suggestion or claude_code.CALL_TO_ACTION["block"])
        if named:
            steps.append(f"run `hivelane wait {named}` to wait your turn")
    else:
        steps = [claude_code.CALL_TO_ACTION["warn"]] + ([ack.suggestion] if ack.suggestion else [])
    message = f"{detail}, {'; '.join(steps)}"
    if ack.others and ack.others[0].prompt:
        prompt = ack.others[0].prompt.replace('"', "'")  # these ACKS carry no control characters
        message += f" (their task, in the teammate's own words: \"{prompt}\")"
    if ack.decision == "block":
        return json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": f"Hivelane: {message}",
                }
            }
        )
    return json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": f"Hivelane warning: {message}",
            },
            "systemMessage": f"Hivelane: {message}",
        }
    )


def payload_matrix(tmp: Path) -> dict[str, dict[str, Any]]:
    src = tmp / "server" / "room.py"
    src.parent.mkdir(exist_ok=True)
    src.write_text("def room():\n    return 'héllo ✓'\n", encoding="utf-8")
    notebook = tmp / "analysis.ipynb"
    notebook.write_text('{"cells": []}\n', encoding="utf-8")
    base = {"session_id": "abc123", "cwd": str(tmp), "transcript_path": str(tmp / "t.jsonl")}

    def tool(event: str, name: str, tool_input: Any) -> dict[str, Any]:
        return {**base, "hook_event_name": event, "tool_name": name, "tool_input": tool_input}

    edit_input = {"file_path": str(src), "old_string": "a", "new_string": "b"}
    return {
        "prompt": {**base, "hook_event_name": "UserPromptSubmit", "prompt": "refactor rooms, ünïcode"},
        "prompt-legacy-key": {**base, "hook_event_name": "UserPromptSubmit", "user_prompt": "fix it"},
        "prompt-blank": {**base, "hook_event_name": "UserPromptSubmit", "prompt": "   "},
        "pre-edit": tool("PreToolUse", "Edit", edit_input),
        "pre-write": tool("PreToolUse", "Write", {"file_path": str(src), "content": "x"}),
        "pre-write-new-file": tool("PreToolUse", "Write", {"file_path": str(tmp / "new.py"), "content": "x"}),
        "pre-multiedit": tool("PreToolUse", "MultiEdit", {"file_path": str(src), "edits": []}),
        "pre-multiedit-hints": tool(
            "PreToolUse", "MultiEdit",
            {"file_path": str(src), "edits": [{"old_string": "room", "new_string": "rm"}, {"old_string": ""}]},
        ),
        "pre-notebookedit": tool("PreToolUse", "NotebookEdit", {"notebook_path": str(notebook), "new_source": "1"}),
        "post-edit": tool("PostToolUse", "Edit", edit_input),
        "post-write": tool("PostToolUse", "Write", {"file_path": str(src)}),
        "irrelevant-bash": tool("PreToolUse", "Bash", {"command": "ls"}),
        "irrelevant-read": tool("PreToolUse", "Read", {"file_path": str(src)}),
        "no-file-path": tool("PreToolUse", "Edit", {"old_string": "a"}),
        "tool-input-not-dict": tool("PreToolUse", "Edit", "nope"),
        "session-start": {**base, "hook_event_name": "SessionStart"},
    }


ACKS: dict[str, EventAck] = {
    "allow": EventAck(decision="allow"),
    "warn-one": EventAck(
        decision="warn",
        reason="Sam's cursor touched this file 42s ago",
        file_path="server/room.py",
        others=[PeerTouch(user_id="Sam", agent="cursor", seconds_ago=42.0)],
    ),
    "warn-many-no-reason": EventAck(
        decision="warn",
        others=[
            PeerTouch(user_id="Sam", agent="cursor", seconds_ago=1.0, prompt="split the relay"),
            PeerTouch(user_id="Kai", agent="codex", seconds_ago=2.0),
            PeerTouch(user_id="Ana", agent="claude-code", seconds_ago=3.0),
        ],
    ),
    "block-one-with-task": EventAck(
        decision="block",
        reason="Sam's cursor edited server/room.py 8s ago",
        file_path="server/room.py",
        others=[PeerTouch(user_id="Sam", agent="cursor", seconds_ago=8.4, prompt='rename "room"')],
    ),
    "block-two-missing-reason": EventAck(
        decision="block",
        file_path="server/room.py",
        others=[
            PeerTouch(user_id="Sam", agent="cursor", seconds_ago=8.4),
            PeerTouch(user_id="Kai", agent="codex", seconds_ago=9.0),
        ],
    ),
    "block-nobody-nothing": EventAck(decision="block"),
    "block-queued-with-suggestion": EventAck(
        decision="block",
        reason="Sam's cursor is editing server/room.py",
        file_path="server/room.py",
        queue_position=2,
        suggestion="work on server/tests meanwhile",
        others=[PeerTouch(user_id="Sam", agent="cursor", seconds_ago=1.0)],
    ),
    "warn-with-suggestion": EventAck(
        decision="warn",
        reason="Sam's cursor declared intent on server/room.py",
        file_path="server/room.py",
        suggestion="split the file first",
        others=[PeerTouch(user_id="Sam", agent="cursor", seconds_ago=1.0)],
    ),
}


def test_fast_path_matches_reference_for_every_payload_and_ack(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payloads = payload_matrix(tmp_path)
    for payload_name, payload in payloads.items():
        reference = claude_code.normalize(payload)
        legacy = legacy_normalize(payload)
        assert reference == legacy, payload_name

        for ack_name, ack in ACKS.items():
            posted: list[dict[str, Any]] = []
            wire = json.loads(ack.model_dump_json())

            def fake_post(event: dict[str, Any], port: int, timeout: float = 2.0) -> Any:
                posted.append(event)
                return wire

            monkeypatch.setattr(hook_fast, "post_event", fake_post)
            monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
            stdout = io.StringIO()
            monkeypatch.setattr("sys.stdout", stdout)

            assert cli.main(["hook", "claude-code"]) == 0
            label = f"{payload_name} x {ack_name}"

            if legacy is None:
                assert posted == [], label
                assert stdout.getvalue() == "", label
                continue
            # Same JSON on the wire as the pydantic model would have sent.
            assert posted == [json.loads(legacy.model_dump_json())], label
            expected = legacy_render_response(legacy, ack)
            assert stdout.getvalue() == expected, label
            assert claude_code.render_response(legacy, ack) == expected, label


def test_plural_and_fallback_wording_is_pinned(tmp_path: Path) -> None:
    target = tmp_path / "room.py"
    target.write_text("x\n", encoding="utf-8")
    event = claude_code.normalize(pre_edit_payload(target))
    assert event is not None

    warn = json.loads(claude_code.render_response(event, ACKS["warn-many-no-reason"]))
    assert warn["hookSpecificOutput"]["additionalContext"] == (
        f"Hivelane warning: a teammate's agent is already working in {target} (and 2 others)"
        ", check with them before you overlap (their task, in the teammate's own words: \"split the relay\")"
    )
    block = json.loads(claude_code.render_response(event, ACKS["block-two-missing-reason"]))
    assert block["hookSpecificOutput"]["permissionDecisionReason"] == (
        "Hivelane: a teammate's agent is already working in server/room.py (and 1 other)"
        ", wait, or pick another file; run `hivelane wait server/room.py` to wait your turn"
    )


@pytest.mark.parametrize(
    "wire",
    [
        None,
        [],
        "block",
        {"decision": "maybe"},
        {"decision": "block", "others": "Sam"},
        {},
    ],
)
def test_malformed_acks_are_read_as_allow(wire: Any) -> None:
    event = {"kind": "pre_edit", "file_path": "a.py"}
    assert claude_code.render_ack(event, wire) == ""


# --- 3. the hand-rolled HTTP client against the real daemon -------------------


@pytest.fixture
def live_daemon(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[tuple[Any, int]]:
    """The real Daemon FastAPI app served by uvicorn, without a relay link,
    its token in tmp_path/home (where the hook subprocesses look too)."""
    import uvicorn

    from hivelane.daemon import Daemon

    monkeypatch.setenv("HIVELANE_HOME", str(tmp_path / "home"))
    # Bind first and hand uvicorn the socket: no window in which another
    # process could take a port picked by bind-and-release.
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = int(listener.getsockname()[1])
    daemon = Daemon("TEAM42", "Ada", port=port)
    daemon.ensure_token()
    config = uvicorn.Config(
        daemon.app, host="127.0.0.1", port=port, log_level="warning", lifespan="off", ws="none"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.02)
    try:
        yield daemon, port
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        listener.close()


def test_hook_round_trips_through_real_daemon(live_daemon: tuple[Any, int], tmp_path: Path) -> None:
    daemon, port = live_daemon
    received: list[NormalizedEvent] = []
    ack = ACKS["block-one-with-task"]

    async def handle_event(event: NormalizedEvent) -> EventAck:
        received.append(event)
        return ack

    daemon.handle_event = handle_event
    target = tmp_path / "room.py"
    target.write_text("def room():\n    return 'é'\n", encoding="utf-8")
    payload = pre_edit_payload(target)

    # In-process: the client parses uvicorn's response into the ack's JSON.
    event = claude_code.normalize_payload(payload)
    assert event is not None
    assert hook_fast.post_event(event, port) == json.loads(ack.model_dump_json())
    assert received == [NormalizedEvent(**event)]

    # Out of process, exactly as Claude Code runs it.
    result = subprocess.run(
        [sys.executable, "-m", "hivelane.cli", "hook", "claude-code"],
        input=json.dumps(payload).encode(),
        env=dict(os.environ, HIVELANE_PORT=str(port), HIVELANE_HOME=str(tmp_path / "home")),
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0
    assert result.stdout.decode() == legacy_render_response(received[-1], ack)
    assert received[-1].old_content == "def room():\n    return 'é'\n"


def raw_request(port: int, head: str) -> tuple[int, bytes]:
    """One hand-written HTTP/1.1 exchange: (status, body)."""
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(head.encode("ascii"))
        data = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
    status_line, _, rest = data.partition(b"\r\n")
    return int(status_line.split()[1]), rest.partition(b"\r\n\r\n")[2]


def test_daemon_demands_its_token_and_its_own_host(live_daemon: tuple[Any, int]) -> None:
    daemon, port = live_daemon
    token = daemon.token
    body = '{"kind": "intent", "session_id": "s", "cwd": "/", "prompt": "x"}'

    def post(headers: str) -> int:
        return raw_request(
            port,
            f"POST /events HTTP/1.1\r\n{headers}Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n{body}",
        )[0]

    host = f"Host: 127.0.0.1:{port}\r\n"
    assert post(host + f"X-Hivelane-Token: {token}\r\n") == 200
    assert post(f"Host: localhost:{port}\r\nX-Hivelane-Token: {token}\r\n") == 200
    assert post(host) == 401
    assert post(host + "X-Hivelane-Token: " + "x" * 43 + "\r\n") == 401
    # DNS rebinding: right socket, right token even, wrong name.
    assert post(f"Host: evil.example:{port}\r\nX-Hivelane-Token: {token}\r\n") == 421
    assert post(f"Host: 127.0.0.1:{port + 1}\r\nX-Hivelane-Token: {token}\r\n") == 421
    for path in ("/status", "/repos/remove", "/shutdown"):
        method = "GET" if path == "/status" else "POST"
        status, _ = raw_request(port, f"{method} {path} HTTP/1.1\r\n{host}Content-Length: 0\r\nConnection: close\r\n\r\n")
        assert status == 401, path
    # /health is the one unauthenticated route, and says nothing.
    status, reply = raw_request(port, f"GET /health HTTP/1.1\r\n{host}Connection: close\r\n\r\n")
    assert status == 200 and json.loads(reply) == {"ok": True, "service": "hivelane-daemon"}
    status, _ = raw_request(port, "GET /health HTTP/1.1\r\nHost: evil.example\r\nConnection: close\r\n\r\n")
    assert status == 421


def test_daemon_adopts_a_token_regenerated_on_disk(live_daemon: tuple[Any, int]) -> None:
    from hivelane import auth

    daemon, port = live_daemon
    old = daemon.token
    auth.token_path().unlink()
    new = auth.ensure_token()
    assert new != old
    assert hook_fast.post_event({"kind": "intent", "session_id": "s", "cwd": "/", "prompt": "x"}, port) == {
        "decision": "allow", "reason": "", "file_path": None, "others": [],
        "queue_position": 0, "suggestion": "",
    }
    assert daemon.token == new


def test_hook_with_a_refused_token_says_so_once(
    live_daemon: tuple[Any, int], tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _daemon, port = live_daemon
    # A hook under another HIVELANE_HOME than the daemon's: another token.
    other = tmp_path / "other-home"
    other.mkdir()
    (other / "token").write_text("w" * 43, encoding="ascii")
    monkeypatch.setenv("HIVELANE_HOME", str(other))
    with pytest.raises(hook_fast.DaemonUnreachable) as caught:
        hook_fast.post_event({"kind": "intent", "session_id": "s", "cwd": "/", "prompt": "x"}, port)
    assert str(caught.value) == hook_fast.OFFLINE_UNAUTHORIZED

    target = tmp_path / "a.py"
    target.write_text("x\n", encoding="utf-8")
    monkeypatch.setenv("HIVELANE_PORT", str(port))
    for expected in ({"systemMessage": hook_fast.OFFLINE_UNAUTHORIZED}, None):
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(pre_edit_payload(target))))
        assert cli.main(["hook", "claude-code"]) == 0
        out = capsys.readouterr().out
        assert (json.loads(out) if out else None) == expected  # rate-limited after the first


def test_missing_token_file_fails_open(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HIVELANE_HOME", str(tmp_path / "nowhere"))
    assert hook_fast.read_token() is None
    (tmp_path / "nowhere").mkdir()
    (tmp_path / "nowhere" / "token").write_text("not a token\r\nX-Evil: 1", encoding="ascii")
    # Never written into a header: it would let the file inject request lines.
    assert hook_fast.read_token() is None


def test_unreachable_port_is_reported_quickly() -> None:
    started = time.monotonic()
    with pytest.raises(hook_fast.DaemonUnreachable) as caught:
        hook_fast.post_event({"kind": "intent"}, unused_port())
    # Windows retries a refused loopback connect for seconds; the connect cap
    # keeps a stopped daemon from adding that to every edit.
    assert time.monotonic() - started < hook_fast.CONNECT_TIMEOUT + 1.0
    assert str(caught.value) == hook_fast.OFFLINE_NOT_RUNNING


def test_silent_daemon_is_reported_as_not_responding() -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)  # the kernel accepts; nobody ever answers
        port = int(listener.getsockname()[1])
        with pytest.raises(hook_fast.DaemonUnreachable) as caught:
            hook_fast.post_event({"kind": "intent"}, port, timeout=0.3)
    assert str(caught.value) == hook_fast.OFFLINE_NOT_RESPONDING


def test_not_responding_daemon_notifies(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    def hang(event: dict[str, Any], port: int, timeout: float = 2.0) -> Any:
        raise hook_fast.DaemonUnreachable(hook_fast.OFFLINE_NOT_RESPONDING)

    monkeypatch.setattr(hook_fast, "post_event", hang)
    monkeypatch.setenv("HIVELANE_HOME", str(tmp_path / "home"))
    target = tmp_path / "a.py"
    target.write_text("x\n", encoding="utf-8")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(pre_edit_payload(target))))

    assert cli.main(["hook", "claude-code"]) == 0
    assert json.loads(capsys.readouterr().out) == {"systemMessage": hook_fast.OFFLINE_NOT_RESPONDING}


@pytest.mark.parametrize(
    "raw",
    [
        b'HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: 20\r\n\r\n{"decision":"allow"}',
        b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n6\r\n{"deci\r\ne;x=1\r\nsion":"allow"}\r\n0\r\n\r\n',
        b'HTTP/1.0 200 OK\r\n\r\n{"decision":"allow"}',
    ],
)
def test_response_parsing(raw: bytes) -> None:
    assert hook_fast._parse_response(raw) == {"decision": "allow"}


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"HTTP/1.1 200 OK\r\ncontent-length: 50\r\n\r\n{}",
        b"garbage\r\n\r\n{}",
        # finding 01 #41: a negative length sliced from the end; duplicates
        # took the last value; other transfer codings went undecoded.
        b'HTTP/1.1 200 OK\r\ncontent-length: -3\r\n\r\n{"decision":"block"}xyz',
        b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\ncontent-length: 20\r\n\r\n{}",
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: gzip, chunked\r\n\r\n2\r\n{}\r\n0\r\n\r\n",
    ],
)
def test_bad_responses_raise(raw: bytes) -> None:
    with pytest.raises((hook_fast.BadResponse, ValueError)):
        hook_fast._parse_response(raw)


@pytest.mark.parametrize("status", [b"422 Unprocessable Entity", b"500 Internal Server Error", b"404 Not Found"])
def test_non_200_is_a_notice_not_a_silent_allow(status: bytes) -> None:
    """finding 01 #12: version skew (an old daemon 422-ing a newer hook's
    event) must not silently allow every edit forever."""
    raw = b"HTTP/1.1 " + status + b"\r\ncontent-length: 2\r\n\r\n{}"
    with pytest.raises(hook_fast.DaemonUnreachable) as caught:
        hook_fast._parse_response(raw)
    assert status[:3].decode() in str(caught.value)
    assert not caught.value.not_running


# --- hook_json -----------------------------------------------------------------

JSON_VALUES: list[Any] = [
    None,
    True,
    False,
    0,
    -17,
    2**70,
    1.5,
    -0.0,
    1e300,
    "",
    "plain",
    'quote " backslash \\ slash /',
    "control \x00\x1f\n\t\r\b\f",
    "unicode é ✓ 😀  ",
    [],
    {},
    [1, "two", [3.0, None], {"k": False}],
    {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny"}},
    {"systemMessage": "Hivelane: Sam, wait"},
    {1: "int key", 2.5: "float key", None: "none key"},
    {True: "bool key", False: "other bool key"},
    (1, 2),
]


@pytest.mark.parametrize("value", JSON_VALUES, ids=repr)
def test_hook_json_dumps_matches_json(value: Any) -> None:
    assert hook_json.dumps(value) == json.dumps(value)


@pytest.mark.parametrize("value", JSON_VALUES, ids=repr)
def test_hook_json_loads_matches_json(value: Any) -> None:
    text = json.dumps(value)
    assert hook_json.loads(text) == json.loads(text)
    assert hook_json.loads(text.encode("utf-8")) == json.loads(text.encode("utf-8"))


def test_hook_json_loads_raw_utf8_and_whitespace() -> None:
    raw = '  \n{"prompt": "héllo ✓", "n": [1, 2.5e3, -1], "t": true}\r\n'.encode("utf-8")
    assert hook_json.loads(raw) == json.loads(raw)
    assert hook_json.loads(b"\xef\xbb\xbf" + raw) == json.loads(raw)


@pytest.mark.parametrize("bad", ["", "   ", "not json", '{"a": 1} extra', "{'a': 1}", '{"a": }', b"\xff\xfe"])
def test_hook_json_rejects_bad_input(bad: Any) -> None:
    with pytest.raises(ValueError):
        hook_json.loads(bad)


def test_hook_json_dumps_rejects_non_json_types() -> None:
    with pytest.raises(TypeError):
        hook_json.dumps({"a": object()})
    with pytest.raises(TypeError):
        hook_json.dumps({("tuple", "key"): 1})
