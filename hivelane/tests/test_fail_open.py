"""With no daemon listening the hook must exit 0 and never decide anything.

It is not silent, though: coordination stopping without anyone noticing is its
own failure, so an unreachable daemon gets one systemMessage per
OFFLINE_NOTICE_INTERVAL, and nothing else.
"""

from __future__ import annotations

import io
import json
import socket
from pathlib import Path

import pytest

from hivelane import cli, hook_fast


def unused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def edit_payload(tmp_path: Path, event: str = "PreToolUse") -> dict:
    target = tmp_path / "a.py"
    target.write_text("x\n", encoding="utf-8")
    return {
        "session_id": "s1",
        "cwd": str(tmp_path),
        "hook_event_name": event,
        "tool_name": "Edit",
        "tool_input": {"file_path": str(target), "old_string": "x", "new_string": "y"},
    }


@pytest.fixture
def no_daemon(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the hook at a dead port and a private state dir; returns the dir."""
    home = tmp_path / "hivelane-home"
    monkeypatch.setenv("HIVELANE_HOME", str(home))
    monkeypatch.setenv("HIVELANE_PORT", str(unused_port()))
    return home


def run(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], payload: dict) -> str:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert cli.main(["hook", "claude-code"]) == 0
    return capsys.readouterr().out


def test_unreachable_daemon_notifies_once_and_never_decides(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    no_daemon: Path,
) -> None:
    first = run(monkeypatch, capsys, edit_payload(tmp_path))

    assert json.loads(first) == {"systemMessage": hook_fast.OFFLINE_NOT_RUNNING}
    assert "hivelane up" in first
    assert (no_daemon / "hook_state.json").exists()

    # Rate-limited: the next edits proceed with no output at all.
    assert run(monkeypatch, capsys, edit_payload(tmp_path)) == ""
    assert run(monkeypatch, capsys, edit_payload(tmp_path)) == ""


def test_notice_repeats_after_the_interval(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    no_daemon: Path,
) -> None:
    no_daemon.mkdir(parents=True)
    state = no_daemon / "hook_state.json"
    stale = hook_fast.time.time() - hook_fast.OFFLINE_NOTICE_INTERVAL - 1
    state.write_text(json.dumps({"offline_notified_at": stale}), encoding="utf-8")

    out = run(monkeypatch, capsys, edit_payload(tmp_path))

    assert json.loads(out) == {"systemMessage": hook_fast.OFFLINE_NOT_RUNNING}
    assert json.loads(state.read_text(encoding="utf-8"))["offline_notified_at"] > stale


def test_user_prompt_submit_also_notifies(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    no_daemon: Path,
) -> None:
    payload = {
        "session_id": "s1",
        "cwd": str(tmp_path),
        "hook_event_name": "UserPromptSubmit",
        "prompt": "refactor the room registry",
    }
    assert json.loads(run(monkeypatch, capsys, payload)) == {
        "systemMessage": hook_fast.OFFLINE_NOT_RUNNING
    }


def test_post_tool_use_stays_silent_and_keeps_the_notice_for_later(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    no_daemon: Path,
) -> None:
    assert run(monkeypatch, capsys, edit_payload(tmp_path, "PostToolUse")) == ""
    assert not (no_daemon / "hook_state.json").exists()
    # The notice was not spent on an event that would not have shown it.
    assert json.loads(run(monkeypatch, capsys, edit_payload(tmp_path))) == {
        "systemMessage": hook_fast.OFFLINE_NOT_RUNNING
    }


def test_unwritable_state_means_no_notice_rather_than_one_per_edit(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("", encoding="utf-8")
    monkeypatch.setenv("HIVELANE_HOME", str(blocker))
    monkeypatch.setenv("HIVELANE_PORT", str(unused_port()))

    assert run(monkeypatch, capsys, edit_payload(tmp_path)) == ""


def test_corrupt_state_file_is_replaced(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    no_daemon: Path,
) -> None:
    no_daemon.mkdir(parents=True)
    (no_daemon / "hook_state.json").write_text("{not json", encoding="utf-8")

    assert json.loads(run(monkeypatch, capsys, edit_payload(tmp_path))) == {
        "systemMessage": hook_fast.OFFLINE_NOT_RUNNING
    }
    assert run(monkeypatch, capsys, edit_payload(tmp_path)) == ""


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


@pytest.mark.parametrize(
    "argv",
    [
        ["hook", "claude-code", "--bogus"],
        ["hook", "claude-code", "--port"],
        ["hook", "claude-code", "--port", "abc"],
        ["hook", "claude-code", "--port=0"],
    ],
)
def test_bad_hook_flags_fail_open(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    """argparse would exit 2 here, which Claude Code reads as "block the edit"."""
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    assert cli.main(argv) == 0
    assert capsys.readouterr().out == ""
