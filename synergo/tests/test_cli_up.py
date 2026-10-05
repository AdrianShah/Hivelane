"""`synergo up`: hooks + room + daemon in one idempotent command. No real
network and no real daemon: see cli_fakes."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import httpx
import pytest

from synergo import cli
from synergo.config import DEFAULT_RELAY
from synergo.tests.cli_fakes import Machine, machine  # noqa: F401  (fixture)

pytestmark = pytest.mark.usefixtures("machine")


def project_values(m: Machine) -> dict:
    return tomllib.loads(m.project_file().read_text(encoding="utf-8"))


def synergo_events(m: Machine) -> set[str]:
    hooks = json.loads(m.settings().read_text(encoding="utf-8"))["hooks"]
    return {
        event
        for event, groups in hooks.items()
        for group in groups
        for hook in group.get("hooks", [])
        if "synergo hook" in hook.get("command", "")
    }


def test_first_run_creates_room_writes_config_installs_hooks_and_connects(
    machine: Machine, capsys: pytest.CaptureFixture[str]
) -> None:
    machine.executables["claude"] = "/usr/bin/claude"

    assert cli.main(["up"]) == 0

    out = capsys.readouterr().out
    assert machine.relay.urls("POST") == ["http://localhost:8000/rooms"]
    assert project_values(machine) == {"room": "K7Q2XM"}  # default relay is not recorded
    assert synergo_events(machine) == {"UserPromptSubmit", "PreToolUse", "PostToolUse"}

    assert machine.service.called("start_background") == [("start_background", 4600)]
    (_, port, root, config) = machine.service.called("connect_repo")[0]
    assert port == 4600
    assert root == machine.repo.resolve()
    assert (config.room, config.relay, config.name) == ("K7Q2XM", DEFAULT_RELAY, "ada-lovelace")

    assert "created room K7Q2XM" in out
    assert "Claude Code (claude on PATH): added hooks to" in out
    assert "commit it" in out
    assert "connected as ada-lovelace (1 teammate online: sam)" in out
    assert "http://localhost:5173/r/K7Q2XM" in out
    assert "pipx install synergo && synergo up --room K7Q2XM\n" in out


def test_second_run_reuses_the_room_and_changes_nothing(
    machine: Machine, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["up"]) == 0
    config_bytes = machine.project_file().read_bytes()
    settings_bytes = machine.settings().read_bytes()
    capsys.readouterr()

    assert cli.main(["up"]) == 0

    out = capsys.readouterr().out
    assert len(machine.relay.urls("POST")) == 1  # no second room
    assert machine.project_file().read_bytes() == config_bytes
    assert machine.settings().read_bytes() == settings_bytes
    assert "joining room K7Q2XM from .synergo.toml" in out
    assert "hooks already in" in out
    assert "wrote" not in out


def test_room_flag_overrides_the_file_and_is_recorded(machine: Machine) -> None:
    machine.project_file().write_text('# team room\nroom = "OLD123"  # pinned\nshare_diffs = false\n', encoding="utf-8")

    assert cli.main(["up", "--room", "new456", "--yes"]) == 0

    assert machine.relay.requests == []
    text = machine.project_file().read_text(encoding="utf-8")
    assert '# team room\nroom = "NEW456"  # pinned\nshare_diffs = false\n' == text
    assert machine.service.called("connect_repo")[0][3].room == "NEW456"


def test_room_flag_switch_can_be_declined(
    machine: Machine, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    machine.project_file().write_text('room = "OLD123"\n', encoding="utf-8")
    monkeypatch.setattr(cli, "confirm", lambda question, **kw: "Switch" not in question)

    assert cli.main(["up", "--room", "NEW456"]) == 1

    assert "kept room OLD123" in capsys.readouterr().err
    assert project_values(machine) == {"room": "OLD123"}
    assert machine.service.calls == []


def test_relay_flag_is_normalized_used_recorded_and_shared(
    machine: Machine, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["up", "--relay", "https://relay.example.com"]) == 0

    out = capsys.readouterr().out
    assert machine.relay.urls("POST") == ["https://relay.example.com/rooms"]
    assert project_values(machine) == {"room": "K7Q2XM", "relay": "wss://relay.example.com/ws"}
    assert machine.service.called("connect_repo")[0][3].relay == "wss://relay.example.com/ws"
    assert "https://synergo.dev/r/K7Q2XM" in out
    assert "synergo up --room K7Q2XM --relay wss://relay.example.com/ws" in out


def test_relay_from_the_project_file_is_used_and_shared(
    machine: Machine, capsys: pytest.CaptureFixture[str]
) -> None:
    machine.project_file().write_text('room = "TEAM42"\nrelay = "wss://r.example/ws"\n', encoding="utf-8")

    assert cli.main(["up"]) == 0

    out = capsys.readouterr().out
    assert machine.service.called("connect_repo")[0][3].relay == "wss://r.example/ws"
    assert "--room TEAM42 --relay wss://r.example/ws" in out


def test_new_room_on_a_personal_relay_records_the_relay(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SYNERGO_RELAY", "ws://127.0.0.1:9000/ws")

    assert cli.main(["up"]) == 0

    assert machine.relay.urls("POST") == ["http://127.0.0.1:9000/rooms"]
    assert project_values(machine) == {"room": "K7Q2XM", "relay": "ws://127.0.0.1:9000/ws"}


def test_dashboard_env_overrides_the_room_url(
    machine: Machine, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(cli.DASHBOARD_ENV, "https://dash.test/")
    assert cli.main(["up"]) == 0
    assert "https://dash.test/r/K7Q2XM" in capsys.readouterr().out


@pytest.mark.parametrize("bad", ["not a url", "ftp://x/ws", "https://"])
def test_bad_relay_flag_is_a_config_error(machine: Machine, bad: str) -> None:
    assert cli.main(["up", "--relay", bad]) == 2
    assert machine.relay.requests == []


def test_bad_room_flag_is_a_config_error(machine: Machine, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["up", "--room", "a/b"]) == 2
    assert "not a room code" in capsys.readouterr().err


# --- names --------------------------------------------------------------------------


def test_name_flag_wins_and_is_remembered(machine: Machine, capsys: pytest.CaptureFixture[str]) -> None:
    personal = machine.home / ".synergo" / "config.toml"

    assert cli.main(["up", "--name", "Grace H"]) == 0

    assert machine.service.called("connect_repo")[0][3].name == "grace-h"
    assert tomllib.loads(personal.read_text(encoding="utf-8")) == {"name": "grace-h"}
    assert "saved name grace-h" in capsys.readouterr().out

    # A plain re-run keeps the chosen name instead of falling back to git's.
    assert cli.main(["up"]) == 0
    assert machine.service.called("connect_repo")[1][3].name == "grace-h"
    assert cli.main(["up", "--name", "grace-h"]) == 0
    assert "saved name" not in capsys.readouterr().out


def test_derived_name_is_not_saved(machine: Machine) -> None:
    assert cli.main(["up"]) == 0
    assert not (machine.home / ".synergo" / "config.toml").exists()


def test_configured_name_beats_git(machine: Machine) -> None:
    personal = machine.home / ".synergo" / "config.toml"
    personal.parent.mkdir(parents=True)
    personal.write_text('name = "linus"\n', encoding="utf-8")
    assert cli.main(["up"]) == 0
    assert machine.service.called("connect_repo")[0][3].name == "linus"


def test_name_env_beats_personal_file(machine: Machine, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SYNERGO_NAME", "Env Name")
    assert cli.main(["up"]) == 0
    assert machine.service.called("connect_repo")[0][3].name == "env-name"


def test_os_user_when_git_has_no_name(machine: Machine) -> None:
    machine.git_name = None
    assert cli.main(["up"]) == 0
    assert machine.service.called("connect_repo")[0][3].name == "osuser"


@pytest.mark.parametrize(
    "git_name, os_name, expected",
    [
        ("Ada Lovelace", "x", ("ada-lovelace", "git user.name")),
        ("José María Ñúñez-García the Third", "x", ("jose-maria-nunez-gar", "git user.name")),
        ("  Lin   Wu_  ", "x", ("lin-wu", "git user.name")),
        ("李小龙", "bruce", ("bruce", "OS user name")),  # nothing ASCII survives
        ("Admin", "Administrator", None),  # both blocked by the relay
        (None, "Administrator", None),
        ("rootbeer", "pat", ("pat", "OS user name")),  # blocked substring
    ],
)
def test_derived_name_rules(
    machine: Machine, git_name: str | None, os_name: str | None, expected: tuple[str, str] | None
) -> None:
    machine.git_name, machine.os_name = git_name, os_name
    if expected is None:
        with pytest.raises(cli.ConfigError, match="pass --name"):
            cli.resolve_name(None, None, machine.repo)
    else:
        assert cli.resolve_name(None, None, machine.repo) == expected


@pytest.mark.parametrize("bad", ["!!!", "a" * 21, "SysAdmin"])
def test_invalid_explicit_name_is_refused(machine: Machine, bad: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["up", "--name", bad]) == 2
    assert "can't be used as a display name" in capsys.readouterr().err
    assert machine.service.calls == []
    assert not machine.settings().exists()


def test_derived_names_are_fixed_points_of_the_relay_sanitizer() -> None:
    from server.sanitize import MAX_LENGTH, sanitize_name

    assert MAX_LENGTH == cli.NAME_MAX_LENGTH
    for raw in ("Ada Lovelace", "José  García", "x_y-z 9"):
        name = cli.normalize_name(raw)
        assert sanitize_name(name) == name


def test_blocklist_mirrors_the_relay() -> None:
    from server.sanitize import BLOCKLIST

    assert set(cli.NAME_BLOCKLIST) == BLOCKLIST


# --- failures ---------------------------------------------------------------------------


def test_outside_git_is_a_helpful_error(
    machine: Machine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.chdir(plain)
    monkeypatch.setattr(cli, "find_git_root", lambda start=None: None)

    assert cli.main(["up"]) == 2

    err = capsys.readouterr().err
    assert "not inside a git repository" in err
    assert "--root" in err
    assert machine.relay.requests == []
    assert not (plain / ".synergo.toml").exists()
    assert not (plain / ".claude").exists()


def test_root_flag_works_outside_git(machine: Machine, tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    assert cli.main(["up", "--root", str(plain)]) == 0
    assert (plain / ".synergo.toml").exists()
    assert (plain / ".claude" / "settings.json").exists()
    assert machine.service.called("connect_repo")[0][2] == plain.resolve()


@pytest.mark.parametrize(
    "status, headers, message",
    [
        (429, {"Retry-After": "1800"}, "rate-limiting room creation"),
        (429, {"Retry-After": "1800"}, "about 30 minutes"),
        (503, {}, "is full"),
        (500, {}, "HTTP 500"),
    ],
)
def test_room_creation_errors_are_explained(
    machine: Machine, capsys: pytest.CaptureFixture[str], status: int, headers: dict, message: str
) -> None:
    machine.relay.create_status = status
    machine.relay.create_headers = headers

    assert cli.main(["up"]) == 2

    assert message in capsys.readouterr().err
    assert not machine.project_file().exists()
    assert machine.service.calls == []


def test_unreachable_relay_is_explained(machine: Machine, capsys: pytest.CaptureFixture[str]) -> None:
    machine.relay.error = lambda request: httpx.ConnectError("connection refused", request=request)
    assert cli.main(["up"]) == 2
    err = capsys.readouterr().err
    assert "cannot reach the relay at http://localhost:8000" in err
    assert "--room CODE" in err


def test_relay_timeout_is_explained(machine: Machine, capsys: pytest.CaptureFixture[str]) -> None:
    machine.relay.error = lambda request: httpx.ReadTimeout("slow", request=request)
    assert cli.main(["up"]) == 2
    assert "did not answer" in capsys.readouterr().err


def test_fatal_repo_exits_1_with_the_reason(machine: Machine, capsys: pytest.CaptureFixture[str]) -> None:
    machine.service.outcome = {"connected": False, "fatal": "room K7Q2XM does not exist on this relay"}

    assert cli.main(["up"]) == 1

    out = capsys.readouterr().out
    assert "the relay refused this repo: room K7Q2XM does not exist" in out
    assert "Share with teammates" not in out


def test_slow_connection_is_reported_but_not_fatal(machine: Machine, capsys: pytest.CaptureFixture[str]) -> None:
    machine.service.outcome = {"connected": False}
    assert cli.main(["up"]) == 0
    out = capsys.readouterr().out
    assert "still connecting" in out
    assert "Share with teammates" in out


def test_daemon_start_failure_exits_1(machine: Machine, capsys: pytest.CaptureFixture[str]) -> None:
    machine.service.start_error = "port 4600 is taken by another program"
    assert cli.main(["up"]) == 1
    assert "could not start the background daemon: port 4600 is taken" in capsys.readouterr().err


def test_unparseable_settings_are_left_alone(machine: Machine, capsys: pytest.CaptureFixture[str]) -> None:
    machine.settings().parent.mkdir()
    machine.settings().write_text("{broken", encoding="utf-8")

    assert cli.main(["up"]) == 2

    assert machine.settings().read_text(encoding="utf-8") == "{broken"
    assert "not valid JSON" in capsys.readouterr().err
    assert machine.service.calls == []


def test_invalid_project_config_stops_before_changing_anything(
    machine: Machine, capsys: pytest.CaptureFixture[str]
) -> None:
    machine.project_file().write_text("room = [\n", encoding="utf-8")
    assert cli.main(["up"]) == 2
    assert "not valid TOML" in capsys.readouterr().err
    assert not machine.settings().exists()
    assert machine.relay.requests == []


# --- hooks & agents ---------------------------------------------------------------------


def test_no_hooks_flag(machine: Machine, capsys: pytest.CaptureFixture[str]) -> None:
    machine.executables["claude"] = "/usr/bin/claude"
    assert cli.main(["up", "--no-hooks"]) == 0
    assert not machine.settings().exists()
    assert "--no-hooks" in capsys.readouterr().out


def test_claude_code_is_hooked_even_when_not_detected(
    machine: Machine, capsys: pytest.CaptureFixture[str]
) -> None:
    machine.executables["codex"] = "/usr/bin/codex"
    machine.executables["aider"] = "/usr/bin/aider"
    (machine.home / ".gemini").mkdir()

    assert cli.main(["up"]) == 0

    out = capsys.readouterr().out
    assert synergo_events(machine) == {"UserPromptSubmit", "PreToolUse", "PostToolUse"}
    assert "Claude Code (not detected): added hooks" in out
    assert "Codex (codex on PATH): added hooks to .codex" in out
    assert "Gemini CLI (~/.gemini): added hooks to .gemini" in out
    assert "Aider (aider on PATH): detected, observe-only via `synergo run aider`" in out


def test_declining_the_undetected_primary_adapter(
    machine: Machine, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "confirm", lambda question, **kw: False)
    assert cli.main(["up"]) == 0
    assert not machine.settings().exists()
    assert "Claude Code: hooks not installed" in capsys.readouterr().out


def test_confirm_never_prompts_without_a_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(prompt: str = "") -> str:
        raise AssertionError("prompted")

    monkeypatch.setattr("builtins.input", explode)
    assert cli.confirm("ok?", assume_yes=False) is True
    assert cli.confirm("ok?", assume_yes=False, default=False) is False
    assert cli.confirm("ok?", assume_yes=True, default=False) is True


# --- foreground --------------------------------------------------------------------------


def test_foreground_serves_in_process_and_attaches_the_repo(
    machine: Machine, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["up", "--foreground"]) == 0

    out = capsys.readouterr().out
    assert machine.service.called("serve") == [("serve", 4600)]
    assert machine.service.called("start_background") == []
    assert machine.service.called("connect_repo")[0][2] == machine.repo.resolve()
    assert "in the foreground" in out
    assert "connected as ada-lovelace" in out


def test_foreground_refuses_when_a_daemon_already_runs(
    machine: Machine, capsys: pytest.CaptureFixture[str]
) -> None:
    machine.service.running = True
    assert cli.main(["up", "--foreground"]) == 2
    assert "already running on port 4600" in capsys.readouterr().err
    assert machine.service.called("serve") == []


# --- helpers ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("ws://localhost:8000/ws", "ws://localhost:8000/ws"),
        ("ws://localhost:8000", "ws://localhost:8000/ws"),
        ("wss://relay.example.com/ws/", "wss://relay.example.com/ws"),
        ("https://relay.example.com", "wss://relay.example.com/ws"),
        ("http://10.0.0.5:8000/", "ws://10.0.0.5:8000/ws"),
        ("HTTPS://relay.example.com/custom/ws", "wss://relay.example.com/custom/ws"),
    ],
)
def test_normalize_relay(raw: str, expected: str) -> None:
    assert cli.normalize_relay(raw) == expected


@pytest.mark.parametrize(
    "relay, origin",
    [
        ("ws://localhost:8000/ws", "http://localhost:8000"),
        ("wss://relay.example.com/ws", "https://relay.example.com"),
        ("wss://relay.example.com/custom/ws/", "https://relay.example.com/custom"),
    ],
)
def test_http_origin(relay: str, origin: str) -> None:
    assert cli.http_origin(relay) == origin


@pytest.mark.parametrize(
    "relay, local",
    [
        ("ws://localhost:8000/ws", True),
        ("ws://127.0.0.1:8000/ws", True),
        ("ws://[::1]:8000/ws", True),
        ("ws://synergo.localhost/ws", True),
        ("wss://relay.synergo.dev/ws", False),
    ],
)
def test_relay_is_local(relay: str, local: bool) -> None:
    assert cli.relay_is_local(relay) is local
