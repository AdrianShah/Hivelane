"""The stdlib-only hook hot path (synergo.hook_fast / synergo.hook_json).

Three promises are checked here: the hook process never imports the daemon
stack; its output is identical to the pydantic reference implementation it
replaced; and its hand-rolled HTTP client really talks to the daemon's
FastAPI/uvicorn endpoint.
"""

from __future__ import annotations

import ast
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

from synergo import cli, hook_fast, hook_json
from synergo.adapters import claude_code
from synergo.events import EventAck, NormalizedEvent
from synergo.protocol import PeerTouch

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


def unused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# --- a stub daemon -------------------------------------------------------------


@pytest.fixture
def stub_daemon() -> Iterator[tuple[int, dict[str, Any]]]:
    """A plain HTTP server answering /events with `state["ack"]` (JSON)."""
    state: dict[str, Any] = {"ack": {"decision": "allow"}, "events": []}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            body = self.rfile.read(int(self.headers.get("content-length") or 0))
            state["events"].append(json.loads(body))
            reply = json.dumps(state["ack"]).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(reply)))
            self.end_headers()
            self.wfile.write(reply)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
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

PROBE = r"""
import sys
before = set(sys.modules)
from synergo import cli
code = cli.main(["hook", "claude-code"])
loaded = sorted(set(sys.modules) - before)
sys.stderr.write("\nLOADED=" + repr(loaded) + "\nCODE=" + repr(code) + "\n")
"""


def run_probe(payload: dict[str, Any], port: int, home: Path) -> tuple[set[str], str]:
    env = dict(os.environ, SYNERGO_PORT=str(port), SYNERGO_HOME=str(home))
    result = subprocess.run(
        [sys.executable, "-c", PROBE],
        input=json.dumps(payload).encode(),
        env=env,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    stderr = result.stderr.decode(errors="replace")
    loaded_line = next(line for line in stderr.splitlines() if line.startswith("LOADED="))
    assert "CODE=0" in stderr
    loaded = ast.literal_eval(loaded_line.removeprefix("LOADED="))
    return {name.split(".")[0] for name in loaded}, result.stdout.decode()


@pytest.mark.parametrize("scenario", ["allow", "block", "no-daemon"])
def test_hook_never_imports_the_daemon_stack(
    scenario: str, stub_daemon: tuple[int, dict[str, Any]], tmp_path: Path
) -> None:
    port, state = stub_daemon
    target = tmp_path / "room.py"
    target.write_text("def room():\n    pass\n", encoding="utf-8")
    if scenario == "block":
        state["ack"] = {
            "decision": "block",
            "reason": "Sam's cursor edited room.py 8s ago",
            "file_path": "room.py",
            "others": [{"user_id": "Sam", "agent": "cursor", "seconds_ago": 8.0, "prompt": "x"}],
        }
    if scenario == "no-daemon":
        port = unused_port()

    roots, stdout = run_probe(pre_edit_payload(target), port, tmp_path / "home")

    assert not roots & HEAVY, f"hook imported {sorted(roots & HEAVY)}"
    assert not roots & SLOW_STDLIB, f"hook imported slow stdlib {sorted(roots & SLOW_STDLIB)}"
    # The scenario really exercised the path it claims to.
    if scenario == "allow":
        assert stdout == ""
    elif scenario == "block":
        assert json.loads(stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
    else:
        assert json.loads(stdout) == {"systemMessage": hook_fast.OFFLINE_NOT_RUNNING}


def test_constants_match_the_daemon() -> None:
    from synergo.daemon import DEFAULT_PORT

    assert hook_fast.DEFAULT_PORT == DEFAULT_PORT
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
        return NormalizedEvent(
            kind="pre_edit", agent="claude-code", session_id=session_id, cwd=cwd,
            file_path=file_path, old_content=read(file_path), tool=tool,
        )
    return NormalizedEvent(
        kind="post_edit", agent="claude-code", session_id=session_id, cwd=cwd,
        file_path=file_path, new_content=read(file_path), tool=tool,
    )


def legacy_render_response(event: NormalizedEvent, ack: EventAck) -> str:
    """claude_code.render_response as it was before the fast path, verbatim."""
    if event.kind != "pre_edit" or ack.decision == "allow":
        return ""
    path = ack.file_path or event.file_path or "this file"
    detail = ack.reason or f"a teammate's agent is already working in {path}"
    if len(ack.others) > 1:
        extra = len(ack.others) - 1
        detail += f" (and {extra} other{'s' if extra > 1 else ''})"
    message = f"{detail} — {claude_code.CALL_TO_ACTION[ack.decision]}"
    if ack.others and ack.others[0].prompt:
        message += f' (their task: "{ack.others[0].prompt}")'
    if ack.decision == "block":
        return json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": f"Synergo: {message}",
                }
            }
        )
    return json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": f"Synergo warning: {message}",
            },
            "systemMessage": f"Synergo: {message}",
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
        "prompt": {**base, "hook_event_name": "UserPromptSubmit", "prompt": "refactor rooms — ünïcode"},
        "prompt-legacy-key": {**base, "hook_event_name": "UserPromptSubmit", "user_prompt": "fix it"},
        "prompt-blank": {**base, "hook_event_name": "UserPromptSubmit", "prompt": "   "},
        "pre-edit": tool("PreToolUse", "Edit", edit_input),
        "pre-write": tool("PreToolUse", "Write", {"file_path": str(src), "content": "x"}),
        "pre-write-new-file": tool("PreToolUse", "Write", {"file_path": str(tmp / "new.py"), "content": "x"}),
        "pre-multiedit": tool("PreToolUse", "MultiEdit", {"file_path": str(src), "edits": []}),
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

            def fake_post(event: dict[str, Any], port: int) -> Any:
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
        f"Synergo warning: a teammate's agent is already working in {target} (and 2 others)"
        ' — check with them before you overlap (their task: "split the relay")'
    )
    block = json.loads(claude_code.render_response(event, ACKS["block-two-missing-reason"]))
    assert block["hookSpecificOutput"]["permissionDecisionReason"] == (
        "Synergo: a teammate's agent is already working in server/room.py (and 1 other)"
        " — wait, or pick another file"
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
def live_daemon() -> Iterator[tuple[Any, int]]:
    """The real Daemon FastAPI app served by uvicorn, without a relay link."""
    import uvicorn

    from synergo.daemon import Daemon

    port = unused_port()
    daemon = Daemon("TEAM42", "Ada", port=port)
    config = uvicorn.Config(
        daemon.app, host="127.0.0.1", port=port, log_level="warning", lifespan="off", ws="none"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
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
        [sys.executable, "-m", "synergo.cli", "hook", "claude-code"],
        input=json.dumps(payload).encode(),
        env=dict(os.environ, SYNERGO_PORT=str(port), SYNERGO_HOME=str(tmp_path / "home")),
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0
    assert result.stdout.decode() == legacy_render_response(received[-1], ack)
    assert received[-1].old_content == "def room():\n    return 'é'\n"


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
    def hang(event: dict[str, Any], port: int) -> Any:
        raise hook_fast.DaemonUnreachable(hook_fast.OFFLINE_NOT_RESPONDING)

    monkeypatch.setattr(hook_fast, "post_event", hang)
    monkeypatch.setenv("SYNERGO_HOME", str(tmp_path / "home"))
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
        b"HTTP/1.1 500 Internal Server Error\r\ncontent-length: 2\r\n\r\n{}",
        b"HTTP/1.1 200 OK\r\ncontent-length: 50\r\n\r\n{}",
        b"garbage\r\n\r\n{}",
    ],
)
def test_bad_responses_raise(raw: bytes) -> None:
    with pytest.raises((hook_fast.BadResponse, ValueError)):
        hook_fast._parse_response(raw)


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
    {"systemMessage": "Synergo: Sam — wait"},
    {1: "int key", 2.5: "float key", True: "bool key", None: "none key"},
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
