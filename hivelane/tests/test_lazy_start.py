"""The hook starts the daemon itself when an agent works in a Hivelane project
and nothing is listening: once per spawn-lock window, silently for the event
that triggered it, and without paying for subprocess on any other path."""

from __future__ import annotations

import ast
import io
import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from hivelane import cli, config, hook_fast, service


def unused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def edit_payload(cwd: Path) -> dict[str, Any]:
    target = cwd / "a.py"
    target.write_text("x\n", encoding="utf-8")
    return {
        "session_id": "s1",
        "cwd": str(cwd),
        "hook_event_name": "PreToolUse",
        "tool_name": "Edit",
        "tool_input": {"file_path": str(target), "old_string": "x", "new_string": "y"},
    }


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A repo with a .hivelane.toml; agents run from a subdirectory of it."""
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (root / ".hivelane.toml").write_text('room = "TEAM42"\n', encoding="utf-8")
    return root / "src"


@pytest.fixture
def spawns(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[int]:
    """A dead daemon port, a private HIVELANE_HOME, and a recording spawn."""
    calls: list[int] = []
    monkeypatch.setenv("HIVELANE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HIVELANE_PORT", str(unused_port()))
    monkeypatch.setattr(hook_fast, "spawn_daemon", lambda port: calls.append(port))
    return calls


def run(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], payload: dict) -> str:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert cli.main(["hook", "claude-code"]) == 0
    return capsys.readouterr().out


def test_constants_match_the_config_module() -> None:
    assert hook_fast.PROJECT_FILE == config.PROJECT_FILE


def test_project_file_is_found_at_or_above_cwd(project: Path, tmp_path: Path) -> None:
    expected = str(project.parent / ".hivelane.toml")
    assert hook_fast.find_project_file(str(project)) == expected
    assert hook_fast.find_project_file(str(project.parent)) == expected
    assert hook_fast.find_project_file(str(tmp_path)) is None
    assert hook_fast.find_project_file("") is None


def test_dead_daemon_in_a_project_is_started_once_and_silently(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    project: Path,
    spawns: list[int],
    tmp_path: Path,
) -> None:
    port = int(os.environ["HIVELANE_PORT"])

    # The triggering event: a spawn, and no "offline" notice for it.
    assert run(monkeypatch, capsys, edit_payload(project)) == ""
    assert spawns == [port]
    assert (tmp_path / "home" / hook_fast.SPAWN_LOCK_FILENAME).exists()

    # Within the lock window nobody spawns again; the usual notice applies.
    second = run(monkeypatch, capsys, edit_payload(project))
    assert json.loads(second) == {"systemMessage": hook_fast.OFFLINE_NOT_RUNNING}
    assert run(monkeypatch, capsys, edit_payload(project)) == ""
    assert spawns == [port]


def test_a_stale_spawn_lock_allows_another_start(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    project: Path,
    spawns: list[int],
    tmp_path: Path,
) -> None:
    lock = tmp_path / "home" / hook_fast.SPAWN_LOCK_FILENAME
    lock.parent.mkdir(parents=True)
    lock.write_text("123", encoding="ascii")
    stale = time.time() - hook_fast.SPAWN_LOCK_SECONDS - 5
    os.utime(lock, (stale, stale))

    assert run(monkeypatch, capsys, edit_payload(project)) == ""
    assert len(spawns) == 1
    assert lock.stat().st_mtime > stale


def test_no_project_file_means_no_start(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    spawns: list[int],
    tmp_path: Path,
) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    out = run(monkeypatch, capsys, edit_payload(plain))
    assert json.loads(out) == {"systemMessage": hook_fast.OFFLINE_NOT_RUNNING}
    assert spawns == []


def test_a_hung_daemon_is_not_respawned(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    project: Path,
    spawns: list[int],
) -> None:
    """Something accepted the connection and never answered: starting a second
    daemon could not bind the port anyway."""

    def hang(event: dict[str, Any], port: int, timeout: float = 2.0) -> Any:
        raise hook_fast.DaemonUnreachable(hook_fast.OFFLINE_NOT_RESPONDING)

    monkeypatch.setattr(hook_fast, "post_event", hang)
    out = run(monkeypatch, capsys, edit_payload(project))
    assert json.loads(out) == {"systemMessage": hook_fast.OFFLINE_NOT_RESPONDING}
    assert spawns == []


def test_a_failed_spawn_falls_back_to_the_notice(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    project: Path,
    spawns: list[int],
) -> None:
    def broken(port: int) -> Any:
        raise OSError("no python here")

    monkeypatch.setattr(hook_fast, "spawn_daemon", broken)
    out = run(monkeypatch, capsys, edit_payload(project))
    assert json.loads(out) == {"systemMessage": hook_fast.OFFLINE_NOT_RUNNING}


def test_unwritable_home_never_spawns(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("", encoding="utf-8")
    monkeypatch.setenv("HIVELANE_HOME", str(blocker))
    assert hook_fast.claim_spawn() is False


# --- the real thing, out of process -----------------------------------------------

PROBE = r"""
import sys
before = set(sys.modules)
from hivelane import cli
code = cli.main(["hook", "claude-code"])
loaded = sorted(set(sys.modules) - before)
sys.stderr.write("\nLOADED=" + repr(loaded) + "\nCODE=" + repr(code) + "\n")
"""

SUBPROCESS_CLOSURE = r"""
import sys
before = set(sys.modules)
import subprocess
print(repr(sorted(set(sys.modules) - before)))
"""


@pytest.fixture
def lazy_port(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[int]:
    """A free port whose lazily started daemon is stopped in teardown."""
    monkeypatch.setenv("HIVELANE_HOME", str(tmp_path / "home"))
    port = unused_port()
    yield port
    deadline = time.monotonic() + 10
    while service.status(port) is None and time.monotonic() < deadline:
        time.sleep(0.1)
    service.stop(port)


def test_hook_really_starts_the_daemon_and_only_then_imports_subprocess(
    project: Path, tmp_path: Path, lazy_port: int
) -> None:
    from hivelane.tests.test_hook_fast import HEAVY, SLOW_STDLIB

    home = tmp_path / "home"
    env = dict(os.environ, HIVELANE_PORT=str(lazy_port), HIVELANE_HOME=str(home))
    result = subprocess.run(
        [sys.executable, "-c", PROBE],
        input=json.dumps(edit_payload(project)).encode(),
        env=env,
        capture_output=True,
        timeout=30,
    )
    stderr = result.stderr.decode(errors="replace")
    assert result.returncode == 0, stderr
    assert "CODE=0" in stderr
    assert result.stdout == b""  # the triggering event stays silent

    loaded_line = next(line for line in stderr.splitlines() if line.startswith("LOADED="))
    loaded = {name.split(".")[0] for name in ast.literal_eval(loaded_line.removeprefix("LOADED="))}
    assert "subprocess" in loaded, "the spawn branch did not run"
    closure = subprocess.run(
        [sys.executable, "-c", SUBPROCESS_CLOSURE], capture_output=True, check=True, timeout=30
    )
    allowed = {name.split(".")[0] for name in ast.literal_eval(closure.stdout.decode())}
    extra = loaded - allowed
    assert not extra & HEAVY, f"hook imported {sorted(extra & HEAVY)}"
    assert not extra & SLOW_STDLIB, f"hook imported slow stdlib {sorted(extra & SLOW_STDLIB)}"

    # The daemon it started comes up on the hook's port, under HIVELANE_HOME.
    deadline = time.monotonic() + 20
    while (status := service.status(lazy_port)) is None:
        assert time.monotonic() < deadline, service.log_tail()
        time.sleep(0.1)
    assert status["mode"] == "multi-repo"
    assert service.read_pid() == status["pid"]
