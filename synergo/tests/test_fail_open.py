"""With no daemon listening the hook must stay silent and exit 0."""

from __future__ import annotations

import io
import json
import socket
from pathlib import Path

import pytest

from synergo import cli


def unused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_hook_fails_open_without_daemon(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    target = tmp_path / "a.py"
    target.write_text("x\n", encoding="utf-8")
    payload = {
        "session_id": "s1",
        "cwd": str(tmp_path),
        "hook_event_name": "PreToolUse",
        "tool_name": "Edit",
        "tool_input": {"file_path": str(target), "old_string": "x", "new_string": "y"},
    }
    monkeypatch.setenv("SYNERGO_PORT", str(unused_port()))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))

    assert cli.main(["hook", "claude-code"]) == 0
    assert capsys.readouterr().out == ""


def test_hook_fails_open_on_garbage_stdin(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("not json at all"))
    assert cli.main(["hook", "claude-code"]) == 0
    assert capsys.readouterr().out == ""


def test_hook_fails_open_on_empty_stdin(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert cli.main(["hook", "claude-code"]) == 0
    assert capsys.readouterr().out == ""
