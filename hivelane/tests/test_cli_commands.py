"""down / leave / doctor / status / open / serve / service / --version, and
console output that survives a cp1252 Windows console."""

from __future__ import annotations

import io
import json
import sys
import time

import pytest

from hivelane import __version__, cli
from hivelane.adapters import claude_code
from hivelane.tests.cli_fakes import Machine, machine  # noqa: F401  (fixture)

pytestmark = pytest.mark.usefixtures("machine")


def set_up_repo(m: Machine, *, room: str = "TEAM42", hooks: bool = True, connect: bool = True) -> None:
    """A repo as `hivelane up` leaves it, without going through `up`."""
    from hivelane.config import load_config

    m.project_file().write_text(f'# settings\nroom = "{room}"\nshare_diffs = false\n', encoding="utf-8")
    if hooks:
        settings = {"hooks": claude_code.hook_settings()}
        m.settings().parent.mkdir(parents=True, exist_ok=True)
        m.settings().write_text(json.dumps(settings), encoding="utf-8")
    if connect:
        m.service.running = True
        m.service.connect_repo(4600, m.repo.resolve(), load_config(m.repo, name="ada"))


# --- down ----------------------------------------------------------------------------


def test_down_stops_the_daemon_and_keeps_hooks(machine: Machine, capsys: pytest.CaptureFixture[str]) -> None:
    set_up_repo(machine)
    settings = machine.settings().read_bytes()
    assert cli.main(["down"]) == 0
    assert machine.service.called("stop") == [("stop", 4600)]
    assert "Hooks stay installed" in capsys.readouterr().out
    assert machine.settings().read_bytes() == settings


def test_down_without_a_daemon(machine: Machine, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["down", "--port", "4700"]) == 0
    assert "no daemon running on port 4700" in capsys.readouterr().out
    # Still asked: stop() is what clears a hung daemon that holds the port.
    assert machine.service.called("stop") == [("stop", 4700)]


def test_down_that_does_not_stop_exits_1(machine: Machine, capsys: pytest.CaptureFixture[str]) -> None:
    machine.service.running = True
    machine.service.stop_result = False
    assert cli.main(["down"]) == 1
    assert "did not stop" in capsys.readouterr().err


# --- leave -----------------------------------------------------------------------------


def test_leave_disconnects_but_keeps_the_room(machine: Machine, capsys: pytest.CaptureFixture[str]) -> None:
    set_up_repo(machine)
    before = machine.project_file().read_text(encoding="utf-8")
    assert cli.main(["leave"]) == 0
    assert machine.service.called("disconnect_repo") == [("disconnect_repo", 4600, machine.repo.resolve())]
    assert machine.service.repos == {}
    assert "disconnected" in capsys.readouterr().out
    assert machine.project_file().read_text(encoding="utf-8") == before


def test_leave_forget_removes_only_the_room(machine: Machine, capsys: pytest.CaptureFixture[str]) -> None:
    set_up_repo(machine)
    machine.project_file().write_text(
        '# settings\nroom = "TEAM42"  # ours\nrelay = "wss://r.example/ws"\n\n[tool.x]\nroom = "keep"\n',
        encoding="utf-8",
    )

    assert cli.main(["leave", "--forget"]) == 0

    assert machine.project_file().read_text(encoding="utf-8") == (
        '# settings\nrelay = "wss://r.example/ws"\n\n[tool.x]\nroom = "keep"\n'
    )
    assert "removed the room" in capsys.readouterr().out


def test_leave_when_not_connected_or_not_running(machine: Machine, capsys: pytest.CaptureFixture[str]) -> None:
    set_up_repo(machine, connect=False)
    assert cli.main(["leave"]) == 0
    assert "nothing to disconnect" in capsys.readouterr().out

    machine.service.running = True
    assert cli.main(["leave", "--forget"]) == 0
    out = capsys.readouterr().out
    assert "was not connected" in out
    assert "removed the room" in out
    assert cli.main(["leave", "--forget"]) == 0
    assert "has no room set" in capsys.readouterr().out


# --- doctor --------------------------------------------------------------------------------


def doctor_lines(out: str) -> dict[str, str]:
    """Check label -> its line, keyed by the label text after the mark."""
    lines: dict[str, str] = {}
    for line in out.splitlines():
        for label in (
            "Python >= 3.12",
            "Git repository",
            ".hivelane.toml",
            "Agent hooks installed",
            "Daemon running",
            "Repo connected",
            "Relay reachable",
            "Relay protocol compatible",
            "Clock in sync with the relay",
        ):
            if label in line and not line.strip().startswith("fix:"):
                lines[label] = line
    return lines


def test_doctor_all_green(machine: Machine, capsys: pytest.CaptureFixture[str]) -> None:
    set_up_repo(machine)

    assert cli.main(["doctor"]) == 0

    out = capsys.readouterr().out
    lines = doctor_lines(out)
    assert len(lines) == 9
    assert all("✔" in line for line in lines.values()), out
    assert "All checks passed." in out
    assert "fix:" not in out
    assert [str(u) for u in (r.url for r in machine.relay.requests)] == [
        "http://localhost:8000/health",
        "http://localhost:8000/readyz",
    ]


def test_doctor_mixed_results(machine: Machine, capsys: pytest.CaptureFixture[str]) -> None:
    set_up_repo(machine, hooks=False)
    machine.relay.readyz = {"protocol_version": 9, "min_supported_protocol": 9}
    machine.relay.clock_offset = -120.0

    assert cli.main(["doctor"]) == 1

    out = capsys.readouterr().out
    lines = doctor_lines(out)
    for passing in ("Python >= 3.12", "Git repository", ".hivelane.toml", "Daemon running", "Repo connected", "Relay reachable"):
        assert "✔" in lines[passing], passing
    for failing in ("Agent hooks installed", "Relay protocol compatible", "Clock in sync with the relay"):
        assert "✖" in lines[failing], failing
    assert "fix: run `hivelane up` (or `hivelane hook install claude-code`)" in out
    assert "relay needs protocol >= 9" in out
    assert "fix: upgrade hivelane" in out
    assert "ahead of the relay" in out
    assert "fix: sync your system clock" in out
    assert "3 checks failed." in out


def test_doctor_with_nothing_set_up(
    machine: Machine, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    import httpx

    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.chdir(plain)
    monkeypatch.setattr(cli, "find_git_root", lambda start=None: None)
    machine.relay.error = lambda request: httpx.ConnectError("refused", request=request)

    assert cli.main(["doctor"]) == 1

    out = capsys.readouterr().out
    lines = doctor_lines(out)
    assert "✔" in lines["Python >= 3.12"]
    for failing in ("Git repository", ".hivelane.toml", "Agent hooks installed", "Daemon running", "Relay reachable"):
        assert "✖" in lines[failing], failing
    # Checks that cannot run are skipped, not failed.
    for skipped in ("Repo connected", "Relay protocol compatible", "Clock in sync with the relay"):
        assert lines[skipped].strip().startswith("-"), skipped
    assert "5 checks failed." in out


def test_doctor_reports_a_fatal_repo(machine: Machine, capsys: pytest.CaptureFixture[str]) -> None:
    machine.service.outcome = {"connected": False, "fatal": "room TEAM42 does not exist"}
    set_up_repo(machine)
    assert cli.main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "✖" in doctor_lines(out)["Repo connected"]
    assert "room TEAM42 does not exist" in out


def test_doctor_accepts_global_hooks(machine: Machine, capsys: pytest.CaptureFixture[str]) -> None:
    set_up_repo(machine, hooks=False)
    global_settings = machine.home / ".claude" / "settings.json"
    global_settings.parent.mkdir()
    global_settings.write_text(json.dumps({"hooks": claude_code.hook_settings()}), encoding="utf-8")
    assert cli.main(["doctor"]) == 0
    assert str(global_settings) in doctor_lines(capsys.readouterr().out)["Agent hooks installed"]


SINGLE_ROOM = {
    "version": "0.1.0",
    "pid": 7,
    "port": 4600,
    "mode": "single-room",
    "repos": [{"root": None, "room": "JOIN01", "relay": "ws://x/ws", "name": "ada", "agent": "cursor", "connected": True, "fatal": None, "peers": []}],
}


@pytest.mark.parametrize(
    "status, code, text",
    [
        (SINGLE_ROOM, 0, "single-room `hivelane join` daemon in room JOIN01"),
        (
            {**SINGLE_ROOM, "repos": [{**SINGLE_ROOM["repos"][0], "connected": False, "fatal": "Name taken"}]},
            1,
            "single-room `hivelane join` daemon on port 4600: Name taken",
        ),
        # A daemon from before multi-repo mode: no `repos` at all.
        ({"connected": True, "room": "OLD", "port": 4600}, 0, "single-room `hivelane join` daemon in room OLD"),
    ],
)
def test_doctor_understands_a_single_room_daemon(
    machine: Machine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    status: dict,
    code: int,
    text: str,
) -> None:
    set_up_repo(machine, connect=False)
    machine.service.running = True
    monkeypatch.setattr(machine.service, "status", lambda port, timeout=1.0: status)
    assert cli.main(["doctor"]) == code
    assert text in doctor_lines(capsys.readouterr().out)["Repo connected"]


def test_up_refuses_to_hide_behind_a_single_room_daemon(
    machine: Machine, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    machine.service.running = True
    monkeypatch.setattr(machine.service, "status", lambda port, timeout=1.0: SINGLE_ROOM)
    assert cli.main(["up"]) == 1
    assert "single-room `hivelane join` daemon" in capsys.readouterr().err
    assert machine.service.called("connect_repo") == []


def test_status_of_a_single_room_mode_daemon(
    machine: Machine, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(machine.service, "status", lambda port, timeout=1.0: SINGLE_ROOM)
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "(pid 7, port 4600, single room (`hivelane join`))" in out
    assert "(no root)" in out
    assert "room JOIN01 on ws://x/ws as ada (cursor)" in out


# --- console encoding ------------------------------------------------------------------------


def cp1252_stdout(monkeypatch: pytest.MonkeyPatch) -> io.BytesIO:
    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding="cp1252", errors="strict"))
    return raw


def test_doctor_on_a_cp1252_console_uses_ascii_marks(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_up_repo(machine, hooks=False)
    raw = cp1252_stdout(monkeypatch)

    assert cli.main(["doctor"]) == 1

    sys.stdout.flush()
    out = raw.getvalue().decode("cp1252")
    assert "[ok] Python >= 3.12" in out
    assert "[x] Agent hooks installed" in out
    assert "✔" not in out


def test_say_never_crashes_on_unencodable_text(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding="ascii", errors="strict"))
    cli.say("done ✔ \u2014 李 → next")
    sys.stdout.flush()
    assert raw.getvalue().decode("ascii") .splitlines() == ["done [ok] - ? -> next"]


def test_marks_use_unicode_when_the_console_can(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(io.BytesIO(), encoding="utf-8"))
    assert cli.mark("ok") == "✔"
    assert cli.mark("fail") == "✖"
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(io.BytesIO(), encoding="cp1252"))
    assert cli.mark("ok") == "[ok]"
    assert cli.mark("fail") == "[x]"


def test_up_on_a_cp1252_console(machine: Machine, monkeypatch: pytest.MonkeyPatch) -> None:
    raw = cp1252_stdout(monkeypatch)
    assert cli.main(["up"]) == 0
    sys.stdout.flush()
    out = raw.getvalue().decode("cp1252")
    assert "[ok] created room K7Q2XM" in out


# --- status --------------------------------------------------------------------------------


def test_status_prints_every_repo(machine: Machine, capsys: pytest.CaptureFixture[str]) -> None:
    set_up_repo(machine)
    entry = machine.service.repos[str(machine.repo.resolve())]
    entry.update(connected_since=time.time() - 125, last_relay_contact=time.time() - 3, queued_events=2)
    machine.service.repos["/elsewhere"] = {
        "root": "/elsewhere",
        "room": "OTHER1",
        "relay": "wss://r.example/ws",
        "name": "ada",
        "agent": "claude-code",
        "connected": False,
        "fatal": "room OTHER1 does not exist",
        "peers": [],
    }

    assert cli.main(["status"]) == 0

    out = capsys.readouterr().out
    assert "Hivelane daemon 0.1.0 (pid 4242, port 4600)" in out
    assert f"✔ {machine.repo.resolve()}" in out
    assert "room TEAM42 on ws://localhost:8000/ws as ada (claude-code)" in out
    assert "connected since 2m ago; 1 teammate online: sam; 2 queued events; last relay contact" in out
    assert "✖ /elsewhere" in out
    assert "stopped: room OTHER1 does not exist" in out


def test_status_with_no_repos(machine: Machine, capsys: pytest.CaptureFixture[str]) -> None:
    machine.service.running = True
    assert cli.main(["status"]) == 0
    assert "no repos connected" in capsys.readouterr().out


def test_status_of_a_legacy_single_room_daemon(
    machine: Machine, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    legacy = {"connected": True, "room": "TEAM42", "name": "ada", "agent": "cursor", "server": "ws://x/ws/TEAM42", "port": 4600, "peers": []}
    monkeypatch.setattr(machine.service, "status", lambda port, timeout=1.0: legacy)
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "single room" in out
    assert "room TEAM42 on ws://x/ws/TEAM42 as ada (cursor)" in out


def test_status_falls_back_to_json_for_unknown_shapes(
    machine: Machine, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(machine.service, "status", lambda port, timeout=1.0: {"something": ["new"]})
    assert cli.main(["status"]) == 0
    assert json.loads(capsys.readouterr().out) == {"something": ["new"]}


def test_status_without_a_daemon(machine: Machine, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["status"]) == 1
    assert "hivelane up" in capsys.readouterr().out


# --- open / serve / service / version ------------------------------------------------------------


def test_open_launches_the_room_url(
    machine: Machine, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url) or True)
    set_up_repo(machine, connect=False)

    assert cli.main(["open"]) == 0

    assert opened == ["http://localhost:5173/r/TEAM42"]
    assert "http://localhost:5173/r/TEAM42" in capsys.readouterr().out


def test_open_with_a_remote_relay(machine: Machine, monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url) or True)
    machine.project_file().write_text('room = "TEAM42"\nrelay = "wss://relay.hivelane.dev/ws"\n', encoding="utf-8")
    assert cli.main(["open"]) == 0
    assert opened == ["https://adrianshah.github.io/Hivelane/r/TEAM42?relay=wss%3A%2F%2Frelay.hivelane.dev%2Fws"]


def test_open_without_a_room(machine: Machine, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr("webbrowser.open", lambda url: pytest.fail("opened a browser"))
    assert cli.main(["open"]) == 2
    assert "run `hivelane up` first" in capsys.readouterr().err


def test_serve_runs_the_daemon_in_the_foreground(machine: Machine, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(machine.service, "serve", lambda port: machine.service.calls.append(("serve", port)) or 0)
    assert cli.main(["serve", "--port", "4700"]) == 0
    assert machine.service.called("serve") == [("serve", 4700)]


def test_service_install_and_uninstall(machine: Machine, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["service", "install"]) == 0
    assert "autostart installed for port 4600" in capsys.readouterr().out
    assert cli.main(["service", "uninstall"]) == 0
    assert "autostart removed" in capsys.readouterr().out
    assert [c[0] for c in machine.service.calls] == ["install_autostart", "uninstall_autostart"]


def test_service_error_is_reported(
    machine: Machine, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(port: int) -> str:
        raise machine.service.ServiceError("launchctl is not available")

    monkeypatch.setattr(machine.service, "install_autostart", fail)
    assert cli.main(["service", "install"]) == 1
    assert "could not install autostart: launchctl is not available" in capsys.readouterr().err


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"hivelane {__version__}"


@pytest.mark.parametrize(
    "argv",
    [["--help"], ["up", "--help"], ["doctor", "--help"], ["service", "--help"], ["leave", "--help"]],
)
def test_help_works(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 0
    assert "usage: hivelane" in capsys.readouterr().out
