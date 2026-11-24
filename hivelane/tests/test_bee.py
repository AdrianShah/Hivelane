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
    # Off Windows, shutil.which itself breaks under a faked platform.
    monkeypatch.setattr(bee.shutil, "which", lambda name: None)
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


# --- several rooms at once ---------------------------------------------------------


def test_one_room_keeps_its_own_tip_and_lists_itself() -> None:
    view = bee.bee_state({"repos": [_repo(root="/w/api")]})
    assert view["tip"].startswith("Hivelane · ABCD2345 · 2 agents")
    assert [(r["room"], r["name"]) for r in view["rooms"]] == [("ABCD2345", "api")]


def test_several_rooms_are_listed_worst_first_and_counted() -> None:
    alert = {"kind": "warn", "file_path": "src/a.py", "others": ["ana"], "at": time.time()}
    view = bee.bee_state(
        {
            "repos": [
                _repo(room="CLEAR111", root="/w/web"),
                _repo(room="HELD2222", root="/w/api", held=1),
                _repo(room="NEAR3333", root="/w/cli", alert=alert),
                _repo(room="ALSO4444", root="/w/app"),
            ]
        }
    )
    assert [r["room"] for r in view["rooms"]] == ["HELD2222", "NEAR3333", "ALSO4444", "CLEAR111"]
    # The icon, label and a plain click are the worst room's.
    assert (view["state"], view["label"], view["room"], view["root"]) == ("block", "1 held", "HELD2222", "/w/api")
    head, detail = view["tip"].split("\n")
    assert head == "Hivelane · 4 rooms · 2 need a look"
    assert detail.startswith("HELD2222 (api): An edit is held") and detail.endswith("(+1 more)")


def test_several_clear_rooms_say_so() -> None:
    view = bee.bee_state({"repos": [_repo(room="A2345678", root="/w/a"), _repo(room="B2345678", root="/w/b")]})
    assert view["state"] == "live"
    assert view["tip"] == "Hivelane · 2 rooms\nAll clear."


def test_each_room_gets_its_own_link() -> None:
    view = bee.bee_state(
        {"repos": [_repo(room="ONE23456", relay="wss://a.example/ws"), _repo(room="TWO23456", relay="wss://b.example/ws")]}
    )
    urls = {r["room"]: r["url"] for r in view["rooms"]}
    assert urls["ONE23456"].endswith("/r/ONE23456?relay=wss%3A%2F%2Fa.example%2Fws")
    assert urls["TWO23456"].endswith("/r/TWO23456?relay=wss%3A%2F%2Fb.example%2Fws")


def test_windows_menu_rows_and_open_target() -> None:
    from hivelane import bee_win

    assert bee_win.room_row({"room": "R2345678", "name": "a&b", "state": "block", "label": "1 held"}) == "R2345678 · a&&b — 1 held"
    assert bee_win.room_row({"room": "R2345678", "name": "web", "state": "live", "label": ""}).endswith("— all clear")
    # The daemon's link as it is: a local relay's own dashboard stays local.
    assert bee_win.open_target({"url": "http://127.0.0.1:8010/r/R2345678"}) == "http://127.0.0.1:8010/r/R2345678"
    assert bee_win.open_target({"url": None}) == "https://adrianshah.github.io/Hivelane"


# --- bringing it back ------------------------------------------------------------


def test_describe_says_how_to_bring_it_back(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    monkeypatch.setenv("HIVELANE_HOME", str(tmp_path))
    for var in ("CI", "SSH_CONNECTION", "SSH_TTY", "HIVELANE_BEE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("DISPLAY", ":0")
    assert bee.describe() == "off (`hivelane bee on` starts it)"
    bee.stop(hide=True)
    assert bee.describe() == "hidden (`hivelane bee on` brings it back)"
    monkeypatch.setenv("HIVELANE_BEE", "0")
    assert bee.describe() == "off (HIVELANE_BEE=0)"


# --- macOS: the same bee, drawn to PNG ----------------------------------------------


def test_mac_runs_the_renderer_then_the_menu_bar_script(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    import sys
    from pathlib import Path

    monkeypatch.setenv("HIVELANE_HOME", str(tmp_path))
    monkeypatch.setattr(bee.shutil, "which", lambda name: "/usr/bin/osascript" if name == "osascript" else None)
    monkeypatch.setattr(sys, "platform", "darwin")
    argv = bee.command(4600)
    assert argv is not None
    assert argv[1:3] == ["-I", "-S"]
    assert Path(argv[3]).name == "bee_mac.py" and Path(argv[3]).is_file()
    assert Path(argv[4]) == tmp_path / "bee.js" and "bee_mac.py" in Path(argv[4]).read_text(encoding="utf-8")
    assert argv[5] == "4600"


def test_mac_frames_are_pngs_of_every_state_and_pose(tmp_path: Any) -> None:
    import struct
    import zlib

    from hivelane import bee_mac

    out = bee_mac.render(tmp_path)
    assert (out / "done").exists()
    size = bee_mac.POINTS * bee_mac.SCALE
    for state in bee_mac.STATES:
        poses = bee_mac.POSES if state in bee_mac.ANIMATED else 1
        for pose in range(poses):
            for palette in ("light", "dark"):
                data = (out / bee_mac.frame_name(state, palette, pose)).read_bytes()
                assert data[:8] == b"\x89PNG\r\n\x1a\n"
                assert struct.unpack(">II", data[16:24]) == (size, size)
    # Pixels survive the round trip: the clear bee has opaque ink somewhere.
    raw = (out / bee_mac.frame_name("live", "light", 0)).read_bytes()
    idat_len = struct.unpack(">I", raw[33:37])[0]
    rows = zlib.decompress(raw[41 : 41 + idat_len])
    assert len(rows) == size * (1 + size * 4)
    assert any(rows[i] == 255 for i in range(4, len(rows), 4) if i % (1 + size * 4))
    # Drawn once: a second start reuses the frames as they are.
    before = (out / "done").stat().st_mtime_ns
    assert bee_mac.render(tmp_path) == out
    assert (out / "done").stat().st_mtime_ns == before


# --- repo-lookup approval flows in the bee -------------------------------------------


def test_joiner_waiting_for_owner_approval_shows_amber_bee() -> None:
    view = bee.bee_state(
        {
            "repos": [
                _repo(
                    room="APQEJN5W",
                    connected=False,
                    awaiting_approval={"request_id": "req-1", "reason": "the owner approves joins", "since": time.time()},
                )
            ]
        }
    )
    assert view["state"] == "warn"
    assert view["label"] == "waiting"
    assert "waiting for the owner to let you in" in view["tip"]
    assert view["join_requests"] == []


def test_owner_gets_amber_bee_and_deduplicated_join_requests() -> None:
    req = {"request_id": "req-ana", "name": "Ana", "agent": "claude-code", "timestamp": time.time()}
    view = bee.bee_state(
        {
            "repos": [
                _repo(room="APQEJN5W", root="/w/api", owner=True, join_requests=[req]),
                _repo(room="APQEJN5W", root="/w/web", owner=True, join_requests=[req]),
            ]
        }
    )
    assert view["state"] == "warn"
    assert view["label"] == "1 waiting"
    assert view["join_requests"] == [
        {"request_id": "req-ana", "room": "APQEJN5W", "name": "Ana", "agent": "claude-code"}
    ]
    assert all(r["join_requests"] == view["join_requests"] for r in view["rooms"])


def test_windows_join_row_and_mac_script_include_approval_actions() -> None:
    from hivelane import bee_win

    assert (
        bee_win.join_row({"name": "Ana & Sam", "room": "APQEJN5W"})
        == "Ana && Sam wants to join APQEJN5W"
    )
    assert "wants to join" in bee.MAC_SCRIPT
    assert "Let in" in bee.MAC_SCRIPT
    assert "Turn away" in bee.MAC_SCRIPT
    assert "/rooms/decide" in bee.MAC_SCRIPT
    assert "Open the dashboard" in bee.MAC_SCRIPT
    assert "Hide the bee" in bee.MAC_SCRIPT


def test_a_person_asking_again_is_announced_once_an_hour() -> None:
    """Each ask has a new request id (the daemon asks again whenever the
    relay's 5-minute hold runs out); the owner hears about the person once."""
    from hivelane import bee_win

    announced: dict[str, float] = {}

    def ask(rid: str, name: str = "ana", room: str = "R2345678") -> dict[str, Any]:
        return {"request_id": rid, "room": room, "name": name, "agent": "claude-code"}

    assert bee_win.requests_to_announce([ask("a")], announced, now=0.0) == [ask("a")]
    for i, t in enumerate((360.0, 720.0, 1080.0, 1440.0, 1800.0)):
        assert bee_win.requests_to_announce([ask(f"r{i}")], announced, now=t) == []
    # Someone else, or the same person in another room, is news.
    assert bee_win.requests_to_announce([ask("b", name="sam")], announced, now=1800.0) != []
    assert bee_win.requests_to_announce([ask("c", room="OTHER234")], announced, now=1800.0) != []
    # Still asking an hour on: one reminder.
    assert bee_win.requests_to_announce([ask("d")], announced, now=bee_win.REMIND_SECONDS) == [ask("d")]


def test_mac_bee_announces_a_repeated_ask_once(tmp_path: Any) -> None:
    """Drive the real MAC_SCRIPT's notifyRequests through six asks."""
    import shutil
    import subprocess

    if shutil.which("osascript") is None:
        pytest.skip("macOS only")
    harness = tmp_path / "harness.js"
    script = bee.MAC_SCRIPT.split("function run(argv)")[0]
    harness.write_text(
        "ObjC.import('Cocoa');\n"
        + script
        + """
let said = [];
notify = (text) => said.push(text);
for (let i = 0; i < 6; i++) {
  view = { room: 'R2345678', join_requests: [{ request_id: 'r' + i, room: 'R2345678', name: 'ana', agent: 'claude-code' }] };
  notifyRequests();
  view = { room: 'R2345678', join_requests: [] };  // the gap between asks
  notifyRequests();
}
view = { room: 'R2345678', join_requests: [{ request_id: 'x', room: 'R2345678', name: 'sam', agent: 'codex' }] };
notifyRequests();
JSON.stringify(said);
""",
        encoding="utf-8",
    )
    out = subprocess.run(["osascript", "-l", "JavaScript", str(harness)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == '["ana wants to join R2345678","sam wants to join R2345678"]'
