"""`hivelane up`: hooks + room + daemon in one idempotent command. No real
network and no real daemon: see cli_fakes."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import httpx
import pytest

from hivelane import cli
from hivelane.config import DEFAULT_RELAY
from hivelane.tests.cli_fakes import Machine, machine  # noqa: F401  (fixture)

pytestmark = pytest.mark.usefixtures("machine")


def project_values(m: Machine) -> dict:
    return tomllib.loads(m.project_file().read_text(encoding="utf-8"))


def hivelane_events(m: Machine) -> set[str]:
    hooks = json.loads(m.settings().read_text(encoding="utf-8"))["hooks"]
    return {
        event
        for event, groups in hooks.items()
        for group in groups
        for hook in group.get("hooks", [])
        if "hivelane hook" in hook.get("command", "")
    }


def test_first_run_creates_room_without_project_file_and_remembers_it(
    machine: Machine, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from hivelane import config as cfg

    machine.executables["claude"] = "/usr/bin/claude"
    monkeypatch.setattr(cli, "repo_id_for_root", lambda root: "repo-abc123")

    assert cli.main(["up"]) == 0

    out = capsys.readouterr().out
    assert machine.relay.urls("POST") == ["http://localhost:8000/rooms"]
    assert not machine.project_file().exists()
    assert cfg.remembered_room("repo-abc123") == ("K7Q2XM", DEFAULT_RELAY)
    assert hivelane_events(machine) == {"UserPromptSubmit", "PreToolUse", "PostToolUse", "SubagentStart", "SubagentStop"}

    assert machine.service.called("start_background") == [("start_background", 4600)]
    (_, port, root, config) = machine.service.called("connect_repo")[0]
    assert port == 4600
    assert root == machine.repo.resolve()
    assert (config.room, config.relay, config.name) == ("K7Q2XM", DEFAULT_RELAY, "ada-lovelace")

    assert "created room K7Q2XM" in out
    assert "Claude Code (claude on PATH): added hooks to" in out
    assert "hivelane invite" in out
    assert "connected as ada-lovelace (1 teammate online: sam)" in out
    # The default relay is local but serves no dashboard here: the hosted one, told where the relay is.
    assert "https://adrianshah.github.io/Hivelane/r/K7Q2XM?relay=ws%3A%2F%2Flocalhost%3A8000%2Fws" in out
    assert "pipx install git+https://github.com/AdrianShah/Hivelane && hivelane up --room K7Q2XM\n" in out


def test_save_project_flag_writes_project_file(
    machine: Machine, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["up", "--save-project"]) == 0
    out = capsys.readouterr().out
    assert project_values(machine) == {"room": "K7Q2XM"}
    assert "commit it" in out


def test_second_run_reuses_remembered_room_without_project_file(
    machine: Machine, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "repo_id_for_root", lambda root: "repo-abc123")
    assert cli.main(["up"]) == 0
    settings_bytes = machine.settings().read_bytes()
    capsys.readouterr()

    assert cli.main(["up"]) == 0

    out = capsys.readouterr().out
    assert len(machine.relay.urls("POST")) == 1  # no second room
    assert not machine.project_file().exists()
    assert machine.settings().read_bytes() == settings_bytes
    assert f"joining remembered room K7Q2XM on {DEFAULT_RELAY}" in out
    assert "hooks already in" in out
    assert "wrote" not in out


def test_second_run_reuses_the_project_file_when_present(
    machine: Machine, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["up", "--save-project"]) == 0
    config_bytes = machine.project_file().read_bytes()
    settings_bytes = machine.settings().read_bytes()
    capsys.readouterr()

    assert cli.main(["up"]) == 0

    out = capsys.readouterr().out
    assert len(machine.relay.urls("POST")) == 1  # no second room
    assert machine.project_file().read_bytes() == config_bytes
    assert machine.settings().read_bytes() == settings_bytes
    assert "joining room K7Q2XM from .hivelane.toml" in out
    assert "hooks already in" in out
    assert "wrote" not in out


def test_room_resolution_order_flag_over_project_over_remembered(
    machine: Machine, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from hivelane import config as cfg

    monkeypatch.setattr(cli, "repo_id_for_root", lambda root: "repo-order")
    cfg.remember_room("repo-order", "REM111", "wss://rem.example/ws")
    machine.project_file().write_text('room = "PROJ22"\nrelay = "wss://proj.example/ws"\n', encoding="utf-8")

    # .hivelane.toml beats remembered
    assert cli.main(["up"]) == 0
    assert "joining room PROJ22 from .hivelane.toml" in capsys.readouterr().out
    assert machine.service.called("connect_repo")[-1][3].room == "PROJ22"

    # --room beats .hivelane.toml and remembered
    assert cli.main(["up", "--room", "FLAG33", "--yes"]) == 0
    assert machine.service.called("connect_repo")[-1][3].room == "FLAG33"
    assert cfg.remembered_room("repo-order") == ("FLAG33", "wss://proj.example/ws")


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


def test_relay_flag_is_normalized_used_remembered_and_shared(
    machine: Machine, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from hivelane import config as cfg

    monkeypatch.setattr(cli, "repo_id_for_root", lambda root: "repo-relay")
    assert cli.main(["up", "--relay", "https://relay.example.com"]) == 0

    out = capsys.readouterr().out
    assert machine.relay.urls("POST") == ["https://relay.example.com/rooms"]
    assert not machine.project_file().exists()
    assert cfg.remembered_room("repo-relay") == ("K7Q2XM", "wss://relay.example.com/ws")
    assert machine.service.called("connect_repo")[0][3].relay == "wss://relay.example.com/ws"
    assert "https://adrianshah.github.io/Hivelane/r/K7Q2XM?relay=wss%3A%2F%2Frelay.example.com%2Fws" in out
    assert "hivelane up --room K7Q2XM --relay wss://relay.example.com/ws" in out


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
    from hivelane import config as cfg

    monkeypatch.setenv("HIVELANE_RELAY", "ws://127.0.0.1:9000/ws")
    monkeypatch.setattr(cli, "repo_id_for_root", lambda root: "repo-personal-relay")

    assert cli.main(["up"]) == 0

    assert machine.relay.urls("POST") == ["http://127.0.0.1:9000/rooms"]
    assert not machine.project_file().exists()
    assert cfg.remembered_room("repo-personal-relay") == ("K7Q2XM", "ws://127.0.0.1:9000/ws")


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
    # Anything with a slash is read as an invite link; it has no room code.
    assert "room code" in capsys.readouterr().err
    assert cli.main(["up", "--room", "not a code!"]) == 2
    assert "not a room code" in capsys.readouterr().err


# --- names --------------------------------------------------------------------------


def test_name_flag_wins_and_is_remembered(
    machine: Machine, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    personal = machine.home / ".hivelane" / "config.toml"
    monkeypatch.setattr(cli, "repo_id_for_root", lambda root: "repo-name-test")

    assert cli.main(["up", "--name", "Grace H"]) == 0

    assert machine.service.called("connect_repo")[0][3].name == "grace-h"
    assert tomllib.loads(personal.read_text(encoding="utf-8"))["name"] == "grace-h"
    assert "saved name grace-h" in capsys.readouterr().out

    # A plain re-run keeps the chosen name instead of falling back to git's.
    assert cli.main(["up"]) == 0
    assert machine.service.called("connect_repo")[1][3].name == "grace-h"
    assert cli.main(["up", "--name", "grace-h"]) == 0
    assert "saved name" not in capsys.readouterr().out


def test_derived_name_is_not_saved(machine: Machine) -> None:
    assert cli.main(["up"]) == 0
    assert not (machine.home / ".hivelane" / "config.toml").exists()


def test_configured_name_beats_git(machine: Machine) -> None:
    personal = machine.home / ".hivelane" / "config.toml"
    personal.parent.mkdir(parents=True)
    personal.write_text('name = "linus"\n', encoding="utf-8")
    assert cli.main(["up"]) == 0
    assert machine.service.called("connect_repo")[0][3].name == "linus"


def test_name_env_beats_personal_file(machine: Machine, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HIVELANE_NAME", "Env Name")
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
    assert not (plain / ".hivelane.toml").exists()
    assert not (plain / ".claude").exists()


def test_root_flag_works_outside_git(machine: Machine, tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    assert cli.main(["up", "--root", str(plain)]) == 0
    assert not (plain / ".hivelane.toml").exists()
    assert (plain / ".claude" / "settings.json").exists()
    assert machine.service.called("connect_repo")[0][2] == plain.resolve()


@pytest.mark.parametrize(
    "status, headers, messages",
    [
        (429, {"Retry-After": "1800"}, ["rate-limiting room creation", "about 30 minutes"]),
        (503, {}, ["is full"]),
        (500, {}, ["HTTP 500"]),
    ],
)
def test_room_creation_errors_are_explained(
    machine: Machine,
    capsys: pytest.CaptureFixture[str],
    status: int,
    headers: dict,
    messages: list[str],
) -> None:
    machine.relay.create_status = status
    machine.relay.create_headers = headers

    assert cli.main(["up"]) == 2

    err = capsys.readouterr().err
    for message in messages:
        assert message in err
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
    assert hivelane_events(machine) == {"UserPromptSubmit", "PreToolUse", "PostToolUse", "SubagentStart", "SubagentStop"}
    assert "Claude Code (not detected): added hooks" in out
    assert "Codex (codex on PATH): added hooks to .codex" in out
    assert "Gemini CLI (~/.gemini): added hooks to .gemini" in out
    assert "Aider (aider on PATH): detected, observe-only via `hivelane run aider`" in out


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
        ("ws://hivelane.localhost/ws", True),
        ("wss://relay.hivelane.dev/ws", False),
    ],
)
def test_relay_is_local(relay: str, local: bool) -> None:
    from hivelane.links import relay_is_local

    assert relay_is_local(relay) is local


# --- invite & join ----------------------------------------------------------------------


def test_invite_prints_link_from_remembered_room_and_includes_e2e_key(
    machine: Machine, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from hivelane import config as cfg

    monkeypatch.setattr(cli, "repo_id_for_root", lambda root: "repo-inv-1")
    cfg.remember_room("repo-inv-1", "K7Q2XM", "wss://relay.example/ws")
    cfg.set_personal_secret("room_keys", "K7Q2XM", "super-secret-room-key-1234")

    assert cli.main(["invite"]) == 0
    captured = capsys.readouterr()
    assert (
        captured.out.strip()
        == "https://adrianshah.github.io/Hivelane/j/K7Q2XM?relay=wss%3A%2F%2Frelay.example%2Fws#key=super-secret-room-key-1234"
    )
    assert "hivelane join" in captured.err

    assert cli.main(["invite", "--no-key", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "room": "K7Q2XM",
        "relay": "wss://relay.example/ws",
        "e2e": True,
        "invite": False,
        "owner": False,
        "url": "https://adrianshah.github.io/Hivelane/j/K7Q2XM?relay=wss%3A%2F%2Frelay.example%2Fws",
    }


def test_invite_copy_uses_clipboard_without_failing(
    machine: Machine, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    copied: list[str] = []
    monkeypatch.setattr(cli, "_copy_to_clipboard", lambda text: copied.append(text) or True)
    machine.project_file().write_text('room = "TEAM42"\n', encoding="utf-8")

    assert cli.main(["invite", "--copy"]) == 0
    out = capsys.readouterr()
    assert copied == ["https://adrianshah.github.io/Hivelane/j/TEAM42?relay=ws%3A%2F%2Flocalhost%3A8000%2Fws"]
    assert "copied invite link to clipboard" in out.err


def test_invite_without_room_fails(machine: Machine, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["invite"]) == 2
    assert "no room configured here" in capsys.readouterr().err


def test_join_invite_link_saves_key_remembers_room_installs_hooks_and_connects(
    machine: Machine, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from hivelane import config as cfg

    monkeypatch.setattr(cli, "repo_id_for_root", lambda root: "repo-join-1")
    url = (
        "https://adrianshah.github.io/Hivelane/j/K7Q2XM"
        "?relay=wss%3A%2F%2Frelay.example%2Fws#key=given-by-ada-1234567890"
    )

    assert cli.main(["join", url, "--name", "Sam", "--yes"]) == 0

    out = capsys.readouterr().out
    assert not machine.project_file().exists()
    assert cfg.remembered_room("repo-join-1") == ("K7Q2XM", "wss://relay.example/ws")
    assert cfg.load_config(machine.repo).room_key("K7Q2XM") == "given-by-ada-1234567890"
    assert hivelane_events(machine) == {"UserPromptSubmit", "PreToolUse", "PostToolUse", "SubagentStart", "SubagentStop"}

    (_, port, root, config) = machine.service.called("connect_repo")[0]
    assert port == 4600
    assert root == machine.repo.resolve()
    assert config.room == "K7Q2XM"
    assert config.relay == "wss://relay.example/ws"
    assert config.name == "sam"
    assert config.e2e is True
    assert config.room_key() == "given-by-ada-1234567890"
    assert "joining room K7Q2XM" in out
    assert "saved end-to-end room key" in out


def test_join_accepts_bare_room_code_and_no_hooks_and_foreground(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "repo_id_for_root", lambda root: "repo-join-2")
    assert cli.main(["join", "team-42", "--no-hooks", "--port", "4700", "--foreground"]) == 0
    assert not machine.settings().exists()
    assert not machine.project_file().exists()
    assert machine.service.called("serve") == [("serve", 4700)]
    (_, port, _, config) = machine.service.called("connect_repo")[0]
    assert port == 4700
    assert config.room == "TEAM42"
    assert config.relay == DEFAULT_RELAY


def test_join_rejects_invalid_link(machine: Machine, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["join", "https://example.com/not-an-invite"]) == 2
    assert "no /j/<ROOM>" in capsys.readouterr().err


def test_open_leave_key_and_doctor_work_with_remembered_room(
    machine: Machine, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from hivelane import auth, config as cfg

    monkeypatch.setattr(cli, "repo_id_for_root", lambda root: "repo-ops-1")
    cfg.remember_room("repo-ops-1", "K7Q2XM", DEFAULT_RELAY)

    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url) or True)
    assert cli.main(["open"]) == 0
    assert opened == ["https://adrianshah.github.io/Hivelane/r/K7Q2XM?relay=ws%3A%2F%2Flocalhost%3A8000%2Fws"]

    monkeypatch.setattr("sys.stdin", cli.io.StringIO("my-secret-passphrase-1234\n") if hasattr(cli, "io") else __import__("io").StringIO("my-secret-passphrase-1234\n"))
    assert cli.main(["key", "set"]) == 0
    assert cfg.load_config(machine.repo).room_key("K7Q2XM") == "my-secret-passphrase-1234"

    # doctor recognises remembered room cleanly
    auth.ensure_token()
    checks = {c.label: c for c in cli.run_doctor_checks(None, 4600)}
    assert checks[".hivelane.toml / room"].status == "ok"
    assert "remembered room K7Q2XM" in checks[".hivelane.toml / room"].detail

    assert cli.main(["leave", "--forget"]) == 0
    assert "forgot the remembered room" in capsys.readouterr().out
    assert cfg.remembered_room("repo-ops-1") is None


# --- repo lookup, auto-binding, and resolution precedence -----------------------


def _write_origin(repo: Path, url: str = "git@github.com:acme/widget.git") -> str:
    from hivelane import gitinfo

    (repo / ".git" / "config").write_text(f'[remote "origin"]\n\turl = {url}\n', encoding="utf-8")
    return gitinfo.compute_repo_id(repo)


def test_create_binds_hashed_lookup_key_without_raw_remote_and_informs_owner(
    machine: Machine, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from hivelane import config as cfg
    from hivelane import gitinfo

    # Make git subprocess read .git/config or stub compute_repo_id directly.
    repo_id = "deadbeef01234567"
    monkeypatch.setattr(gitinfo, "compute_repo_id", lambda root: repo_id)
    expected_key = gitinfo.repo_lookup_key(repo_id)

    assert cli.main(["up"]) == 0

    out = capsys.readouterr().out
    assert expected_key != repo_id
    assert len(expected_key) == 64
    assert machine.relay.created_bodies == [{"repo_key": expected_key}]
    assert machine.relay.repo_bindings == {expected_key: "K7Q2XM"}
    assert "teammates who clone this repo will find it automatically" in out
    assert cfg.remembered_room(repo_id) == ("K7Q2XM", DEFAULT_RELAY)


def test_no_repo_lookup_skips_lookup_and_binding(
    machine: Machine, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from hivelane import gitinfo

    repo_id = "deadbeef01234567"
    lookup_key = gitinfo.repo_lookup_key(repo_id)
    machine.relay.repo_bindings[lookup_key] = "TEAM9999"
    monkeypatch.setattr(gitinfo, "compute_repo_id", lambda root: repo_id)

    assert cli.main(["up", "--no-repo-lookup"]) == 0

    out = capsys.readouterr().out
    assert not any(r.method == "GET" and "/rooms/by-repo/" in r.url.path for r in machine.relay.requests)
    assert machine.relay.created_bodies == [{}]
    assert "created room K7Q2XM" in out
    assert "teammates who clone" not in out


def test_teammate_finds_bound_room_by_repo_and_requests_approval(
    machine: Machine, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from hivelane import config as cfg
    from hivelane import gitinfo

    repo_id = "feedface01234567"
    lookup_key = gitinfo.repo_lookup_key(repo_id)
    machine.relay.repo_bindings[lookup_key] = "APQEJN5W"
    monkeypatch.setattr(gitinfo, "compute_repo_id", lambda root: repo_id)

    assert cli.main(["up"]) == 0

    out = capsys.readouterr().out
    assert "found your team's room APQEJN5W for this repo; asking the owner to let you in" in out
    assert machine.relay.created_bodies == []
    assert not machine.project_file().exists()
    assert machine.service.repos[str(machine.repo.resolve())]["via_repo_lookup"] is True
    assert cfg.remembered_room(repo_id) == ("APQEJN5W", DEFAULT_RELAY)


def test_room_resolution_precedence_room_file_remembered_lookup_create(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hivelane import config as cfg
    from hivelane import gitinfo

    repo_id = "0123456789abcdef"
    lookup_key = gitinfo.repo_lookup_key(repo_id)
    machine.relay.repo_bindings[lookup_key] = "LOOK9999"
    monkeypatch.setattr(gitinfo, "compute_repo_id", lambda root: repo_id)

    # 3. Remembered room beats relay lookup (and restores remembered relay).
    cfg.remember_room(repo_id, "REMEM123", "wss://rem.example/ws")
    assert cli.main(["up"]) == 0
    connect_calls = machine.service.called("connect_repo")
    assert connect_calls[-1][3].room == "REMEM123"
    assert connect_calls[-1][3].relay == "wss://rem.example/ws"
    assert machine.service.repos[str(machine.repo.resolve())]["via_repo_lookup"] is False
    assert not any("/rooms/by-repo/" in r.url.path for r in machine.relay.requests)

    # 2. .hivelane.toml beats remembered room.
    machine.project_file().write_text('room = "FILE4567"\n', encoding="utf-8")
    assert cli.main(["up"]) == 0
    assert machine.service.called("connect_repo")[-1][3].room == "FILE4567"

    # 1. --room beats .hivelane.toml.
    assert cli.main(["up", "--room", "FLAG8901", "--yes"]) == 0
    assert machine.service.called("connect_repo")[-1][3].room == "FLAG8901"


# --- worktrees -----------------------------------------------------------------------


def test_uncommitted_claude_settings_get_user_level_hooks_for_worktrees(
    machine: Machine, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A session in a linked worktree reads the hooks of its own checkout:
    when .claude/settings.json is not committed, only user-level hooks reach it."""
    from hivelane import gitinfo

    monkeypatch.setattr(gitinfo, "is_tracked", lambda root, rel: False)
    assert cli.main(["up", "-y"]) == 0
    user_settings = machine.home / ".claude" / "settings.json"
    assert user_settings.is_file()
    assert "PreToolUse" in json.loads(user_settings.read_text(encoding="utf-8"))["hooks"]
    assert "worktrees and their subagents show up" in capsys.readouterr().out
    # Idempotent: a second run reports them, it does not add them again.
    assert cli.main(["up", "-y"]) == 0
    assert "worktrees are covered" in capsys.readouterr().out


@pytest.mark.parametrize("tracked", [True, None])
def test_committed_or_unknown_settings_leave_user_settings_alone(
    machine: Machine, monkeypatch: pytest.MonkeyPatch, tracked: bool | None
) -> None:
    from hivelane import gitinfo

    monkeypatch.setattr(gitinfo, "is_tracked", lambda root, rel: tracked)
    assert cli.main(["up", "-y"]) == 0
    assert not (machine.home / ".claude" / "settings.json").exists()


def test_no_hooks_never_touches_user_settings(machine: Machine, monkeypatch: pytest.MonkeyPatch) -> None:
    from hivelane import gitinfo

    monkeypatch.setattr(gitinfo, "is_tracked", lambda root, rel: False)
    assert cli.main(["up", "-y", "--no-hooks"]) == 0
    assert not (machine.home / ".claude" / "settings.json").exists()
