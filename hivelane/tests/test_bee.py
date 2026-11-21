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
    assert bee.start(4601).startswith("on, ")
    assert seen["argv"] == ["fake-bee", "4601"]
    for stream in ("stdin", "stdout", "stderr"):
        assert seen[stream] is subprocess.DEVNULL
    assert (tmp_path / "bee.pid").read_text() == "4242"


def test_hide_survives_up_until_bee_on(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    monkeypatch.setenv("HIVELANE_HOME", str(tmp_path))
    for var in ("CI", "SSH_CONNECTION", "SSH_TTY", "HIVELANE_BEE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(bee, "command", lambda port: ["fake-bee"])

    class FakeProc:
        pid = 4243

    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kwargs: FakeProc())
    bee.stop(hide=True)
    assert bee.start(4600).startswith("off (you hid it")
    assert bee.start(4600, force=True).startswith("on, ")
    assert not bee.hidden_path().exists()


def test_running_pid_sees_a_live_process_and_not_a_dead_one(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """A real child process, so the liveness probe is exercised on every
    platform (os.kill(pid, 0) always failed on Windows)."""
    import sys

    monkeypatch.setenv("HIVELANE_HOME", str(tmp_path))
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        bee.pid_path().write_text(str(proc.pid))
        assert bee.running_pid() == proc.pid
    finally:
        proc.kill()
        proc.wait()
    assert bee.running_pid() is None


def test_windows_runs_the_ctypes_bee_with_the_stdlib_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """No PowerShell or .NET: the tray bee is bee_win.py on a bare
    interpreter (-I -S), which is what keeps it a few MB."""
    import sys
    from pathlib import Path

    monkeypatch.setenv("HIVELANE_HOME", str(tmp_path))
    monkeypatch.setattr(sys, "platform", "win32")
    argv = bee.command(4600)
    assert argv is not None
    assert argv[1:3] == ["-I", "-S"]
    assert Path(argv[3]).name == "bee_win.py" and Path(argv[3]).is_file()
    assert argv[4] == "4600"


def test_windows_frames_draw_the_logo_bee_and_flap() -> None:
    """The icon is the logo bee drawn in pure Python: an opaque body with the
    brand stripe, transparent corners, wings that move between frames, and a
    state badge only when something needs a look."""
    from hivelane import bee_win

    size = 24
    body, accent, badge = (bee_win.LAYERS.index(name) for name in ("body", "accent", "badge"))
    rest, beat = bee_win.coverage(size, -1), bee_win.coverage(size, 6)
    assert len(rest) == size * size
    assert not any(rest[0])  # a transparent corner
    assert any(px[body] for px in rest) and any(px[accent] for px in rest)
    assert rest != beat
    assert not any(px[badge] for px in rest)
    assert any(px[badge] for px in bee_win.coverage(size, -1, "block"))
    data = bee_win.pixels(rest, "live", bee_win.LIGHT)
    assert len(data) == size * size * 4
    centre = (size // 2 * size + size // 2) * 4
    assert data[centre + 3] == 255
    # Away: the same bee, faded.
    assert bee_win.pixels(bee_win.coverage(size, -1, "off"), "off", bee_win.LIGHT)[centre + 3] < 255


def test_windows_bee_fetch_sends_token_and_host_with_port(tmp_path: Any) -> None:
    """The daemon turns away a Host without its port (421), which left the
    bee stuck on "off"; this is the request it must send."""
    import json
    import socket
    import threading

    from hivelane import bee_win

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    seen: dict[str, bytes] = {}

    def serve() -> None:
        conn, _ = srv.accept()
        with conn:
            seen["req"] = conn.recv(4096)
            body = json.dumps({"state": "live", "label": "", "tip": "t", "root": None}).encode()
            conn.sendall(b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\n\r\n" + body)

    threading.Thread(target=serve, daemon=True).start()
    token = tmp_path / "token"
    token.write_text("sekret\n")

    class Fake:
        pass

    fake = Fake()
    fake.port, fake.token_file = port, str(token)  # type: ignore[attr-defined]
    try:
        assert bee_win.Bee._fetch(fake)["state"] == "live"  # type: ignore[arg-type]
    finally:
        srv.close()
    assert f"Host: 127.0.0.1:{port}\r\n".encode() in seen["req"]
    assert b"X-Hivelane-Token: sekret\r\n" in seen["req"]


def test_a_click_gets_the_room_code_and_dashboard_link() -> None:
    view = bee.bee_state({"repos": [_repo(relay="wss://relay.example/ws")]})
    assert view["room"] == "ABCD2345"
    assert view["url"].endswith("/r/ABCD2345?relay=wss%3A%2F%2Frelay.example%2Fws")
    # No relay known (an older daemon): no link, and the bee falls back to the CLI.
    assert bee.bee_state({"repos": [_repo()]})["url"] is None
    assert bee.bee_state({"repos": []})["room"] is None


def test_windows_menu_shows_the_whole_status_wrapped() -> None:
    from hivelane import bee_win

    tip = "Hivelane · R: stopped (" + "word " * 40 + ")\nCheck the room code & retry."
    lines = bee_win.menu_lines(tip, width=40, most=4)
    assert len(lines) == 4 and all(len(line) <= 40 for line in lines)
    assert lines[-1].endswith("…")
    assert bee_win.menu_lines("a & b") == ["a && b"]
    assert bee_win.menu_lines("") == ["Hivelane"]
