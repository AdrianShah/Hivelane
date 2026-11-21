"""The desktop bee: what it shows, and that starting it never holds a
caller (often an agent's tool call) open."""

from __future__ import annotations

import subprocess
import time
from typing import Any

import pytest

from hivelane import bee
from hivelane.daemon import ALERT_FRESH_SECONDS, fresh_alert


def _repo(**over: Any) -> dict[str, Any]:
    repo: dict[str, Any] = {"room": "ABCD2345", "root": "/r", "connected": True, "peers": ["ana"], "held": 0, "alert": None}
    repo.update(over)
    return repo


def test_no_repos_is_off() -> None:
    assert bee.bee_state({"repos": []})["state"] == "off"


def test_a_connected_room_is_live_and_counts_agents() -> None:
    view = bee.bee_state({"repos": [_repo()]})
    assert (view["state"], view["label"], view["root"]) == ("live", "", "/r")
    assert "2 agents" in view["tip"]


def test_a_held_edit_is_block_with_a_count() -> None:
    view = bee.bee_state({"repos": [_repo(held=2)]})
    assert (view["state"], view["label"]) == ("block", "2 held")


def test_a_warn_names_the_file_and_people() -> None:
    alert = {"kind": "warn", "file_path": "src/a.py", "others": ["ana"], "at": time.time()}
    view = bee.bee_state({"repos": [_repo(alert=alert)]})
    assert (view["state"], view["label"]) == ("warn", "overlap")
    assert "src/a.py (ana)" in view["tip"]


def test_a_ping_shows_as_warn() -> None:
    alert = {"kind": "nudge", "text": "the room owner pinged you", "at": time.time()}
    assert bee.bee_state({"repos": [_repo(alert=alert)]})["label"] == "pinged"


def test_the_worst_repo_wins() -> None:
    view = bee.bee_state({"repos": [_repo(root="/ok"), _repo(root="/bad", held=1), _repo(root="/c", connected=False)]})
    assert (view["state"], view["root"]) == ("block", "/bad")


def test_alerts_go_stale() -> None:
    alert = {"kind": "warn", "at": 100.0}
    assert fresh_alert(alert, 100.0 + ALERT_FRESH_SECONDS - 1) is alert
    assert fresh_alert(alert, 100.0 + ALERT_FRESH_SECONDS + 1) is None
    assert fresh_alert(None, 0) is None


def test_off_by_env_and_headless(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    monkeypatch.setenv("HIVELANE_HOME", str(tmp_path))
    monkeypatch.setenv("HIVELANE_BEE", "0")
    assert bee.start(4600).startswith("off")
    monkeypatch.delenv("HIVELANE_BEE")
    monkeypatch.setenv("CI", "1")
    assert bee.start(4600) == "off (CI)"


def test_start_is_detached_and_silent(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """An agent running `hivelane up` in a tool call must get its answer at
    once: the bee may not inherit the caller's stdout/stderr."""
    monkeypatch.setenv("HIVELANE_HOME", str(tmp_path))
    for var in ("CI", "SSH_CONNECTION", "SSH_TTY", "HIVELANE_BEE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(bee, "command", lambda port: ["fake-bee", str(port)])
    seen: dict[str, Any] = {}

    class FakeProc:
        pid = 4242

    def fake_popen(argv: list[str], **kwargs: Any) -> FakeProc:
        seen.update(kwargs, argv=argv)
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    assert bee.start(4601) == "on"
    assert seen["argv"] == ["fake-bee", "4601"]
    for stream in ("stdin", "stdout", "stderr"):
        assert seen[stream] is subprocess.DEVNULL
    assert (tmp_path / "bee.pid").read_text() == "4242"
