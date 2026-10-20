"""The CLI side of Phase 4 security: plaintext relays need an explicit opt-in,
`synergo up` keeps the owner token and the e2e room key in the personal
config (never the project file, never the share line), `synergo key` and
`synergo room`, and doctor's new checks. No real network and no real daemon:
see cli_fakes."""

from __future__ import annotations

import io
import os
import tomllib

import pytest

from synergo import cli
from synergo import config as cfg
from synergo.tests.cli_fakes import Machine, machine  # noqa: F401  (fixture)

pytestmark = pytest.mark.usefixtures("machine")


def personal(m: Machine) -> dict:
    path = m.home / ".synergo" / "config.toml"
    return tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


# --- transport ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("relay", "refused"),
    [
        ("ws://relay.example.com/ws", True),
        ("ws://192.168.1.20:8000/ws", True),
        ("wss://relay.example.com/ws", False),
        ("ws://localhost:8000/ws", False),
        ("ws://127.0.0.1:8000/ws", False),
        ("ws://[::1]:8000/ws", False),
        ("ws://dev.localhost:8000/ws", False),
    ],
)
def test_transport_rule(relay: str, refused: bool) -> None:
    assert (cfg.transport_problem(relay) is not None) is refused
    assert cfg.transport_problem(relay, insecure=True) is None


def test_up_refuses_a_plaintext_remote_relay(machine: Machine, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["up", "--relay", "ws://relay.example.com/ws"]) == 2
    err = capsys.readouterr().err
    assert "refusing the plaintext relay" in err and "--insecure" in err
    assert machine.service.calls == [] and machine.relay.requests == []


def test_insecure_opts_in_and_the_share_line_carries_it(machine: Machine, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["up", "--relay", "http://relay.lan:8000", "--insecure"]) == 0
    out = capsys.readouterr().out
    assert "synergo up --room K7Q2XM --relay ws://relay.lan:8000/ws --insecure" in out
    # The project file may say so too, for the whole team.
    machine.project_file().write_text(
        'room = "K7Q2XM"\nrelay = "ws://relay.lan:8000/ws"\ninsecure = true\n', encoding="utf-8"
    )
    assert cli.main(["up"]) == 0


def test_join_refuses_a_plaintext_remote_relay(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["join", "TEAM", "--name", "ada", "--server", "ws://relay.example.com/ws"]) == 2
    assert "refusing the plaintext relay" in capsys.readouterr().err


# --- owner token -------------------------------------------------------------------------


def test_creating_a_room_keeps_its_owner_token_privately(machine: Machine, capsys: pytest.CaptureFixture[str]) -> None:
    machine.relay.owner_token = "own-3f9a"
    assert cli.main(["up"]) == 0
    out = capsys.readouterr().out
    assert "you own room K7Q2XM" in out
    assert "own-3f9a" not in out
    assert personal(machine)["owner_tokens"] == {"K7Q2XM": "own-3f9a"}
    assert "owner" not in machine.project_file().read_text(encoding="utf-8")
    path = machine.home / ".synergo" / "config.toml"
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
    (_, _, _, config) = machine.service.called("connect_repo")[0]
    assert config.owner_token() == "own-3f9a"


def test_secrets_in_the_project_file_are_refused(machine: Machine, capsys: pytest.CaptureFixture[str]) -> None:
    machine.project_file().write_text('room = "K7Q2XM"\nroom_keys = { K7Q2XM = "oops" }\n', encoding="utf-8")
    assert cli.main(["up"]) == 2
    assert "must never be in the committed project file" in capsys.readouterr().err


# --- e2e keys ---------------------------------------------------------------------------------


def test_first_e2e_up_generates_the_key_and_shows_it_once(machine: Machine, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["up", "--e2e"]) == 0
    out = capsys.readouterr().out
    key = personal(machine)["room_keys"]["K7Q2XM"]
    assert len(key) >= 32
    assert f"ROOM KEY   {key}" in out
    assert "separate channel" in out
    assert tomllib.loads(machine.project_file().read_text(encoding="utf-8"))["e2e"] is True
    # The share line never carries the key.
    share = [line for line in out.splitlines() if "synergo up --room" in line]
    assert share and all(key not in line for line in share)
    (_, _, _, config) = machine.service.called("connect_repo")[0]
    assert config.e2e and config.room_key() == key

    assert cli.main(["up"]) == 0
    again = capsys.readouterr().out
    assert key not in again
    assert "end-to-end encrypted room (key from" in again


def test_a_teammate_sets_the_key_they_were_given(machine: Machine, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    machine.project_file().write_text('room = "K7Q2XM"\ne2e = true\n', encoding="utf-8")
    monkeypatch.setattr("sys.stdin", io.StringIO("given-by-ada-1234567890\n"))
    assert cli.main(["key", "set"]) == 0
    assert personal(machine)["room_keys"] == {"K7Q2XM": "given-by-ada-1234567890"}
    assert cli.main(["key", "show"]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip().endswith("given-by-ada-1234567890")
    assert "out of band" in captured.err
    assert cli.main(["up"]) == 0
    assert "given-by-ada" not in capsys.readouterr().out
    # The environment wins over the personal file.
    monkeypatch.setenv(cfg.ROOM_KEY_ENV, "from-env")
    assert cfg.load_config(machine.repo).room_key() == "from-env"
    assert cli.main(["key", "show", "other1"]) == 0  # env applies to any room


def test_joining_an_e2e_room_without_its_key_asks_for_the_teammates_key(
    machine: Machine, capsys: pytest.CaptureFixture[str]
) -> None:
    machine.project_file().write_bytes(b'room = "K7Q2XM"\ne2e = true\n')
    assert cli.main(["up"]) == 2
    err = capsys.readouterr().err
    assert "synergo key set K7Q2XM" in err
    assert "room_keys" not in personal(machine)  # no fresh key was made up
    assert machine.service.called("connect_repo") == []


def test_the_bare_env_key_never_seeds_a_new_room(
    machine: Machine, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(cfg.ROOM_KEY_ENV, "another-rooms-key")
    assert cli.main(["up", "--e2e"]) == 2
    assert "SYNERGO_ROOM_KEY_K7Q2XM" in capsys.readouterr().err


def test_key_show_without_a_key_fails(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["key", "show", "NOPE"]) == 1
    assert "no key for room NOPE" in capsys.readouterr().err


# --- room owner commands -------------------------------------------------------------------------


def test_room_commands_go_to_the_daemon(machine: Machine, capsys: pytest.CaptureFixture[str]) -> None:
    machine.service.running = True
    machine.service.replies = {"/rooms/decide": {"room": "K7Q2XM"}, "/rooms/policy": {"room": "K7Q2XM"}}
    assert cli.main(["room", "approve", "req1"]) == 0
    assert cli.main(["room", "deny", "req2"]) == 0
    assert cli.main(["room", "require-approval", "on"]) == 0
    assert machine.service.called("_request") == [
        ("_request", "POST", "/rooms/decide", {"request_id": "req1", "approve": True}),
        ("_request", "POST", "/rooms/decide", {"request_id": "req2", "approve": False}),
        ("_request", "POST", "/rooms/policy", {"root": str(machine.repo.resolve()), "require_approval": True}),
    ]
    out = capsys.readouterr().out
    assert "approved join request req1 in room K7Q2XM" in out
    assert "holds newcomers until you approve" in out


def test_status_shows_requests_waits_and_encryption() -> None:
    status = {
        "version": "0.1.0", "pid": 1, "port": 4600,
        "repos": [{
            "root": "/r", "room": "K7Q2XM", "relay": "wss://x/ws", "name": "ada", "agent": "claude-code",
            "connected": False, "peers": [], "e2e": True, "e2e_key": True, "owner": True,
            "require_approval": True, "awaiting_approval": {"request_id": "r1", "reason": "x"},
            "join_requests": [{"request_id": "abc", "name": "eve", "agent": "cursor"}],
        }],
    }
    text = "\n".join(cli.format_status(status, 4600) or [])
    assert "end-to-end encrypted" in text
    assert "joins need your approval" in text
    assert "waiting for an owner to approve" in text
    assert "`synergo room approve abc`" in text


def test_up_reports_a_held_join(machine: Machine, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "APPROVAL_WAIT", 0.2)
    machine.service.outcome = {"connected": False, "awaiting_approval": {"request_id": "r", "reason": ""}}
    assert cli.main(["up"]) == 0
    out = capsys.readouterr().out
    assert out.count("waiting for an owner to approve this join (room K7Q2XM)") == 1
    assert "still waiting for an owner to approve" in out


# --- doctor ------------------------------------------------------------------------------------------


def test_doctor_checks_token_transport_and_key(machine: Machine, capsys: pytest.CaptureFixture[str]) -> None:
    from synergo import auth

    machine.project_file().write_text(
        'room = "K7Q2XM"\nrelay = "ws://relay.lan:8000/ws"\ne2e = true\n', encoding="utf-8"
    )
    auth.ensure_token()
    checks = {c.label: c for c in cli.run_doctor_checks(None, 4600)}
    assert checks["Daemon token readable"].status == "ok"
    assert checks["Relay transport encrypted"].status == "fail"
    assert checks["End-to-end room key"].status == "fail"
    assert "synergo key set K7Q2XM" in checks["End-to-end room key"].fix

    machine.project_file().write_text(
        'room = "K7Q2XM"\nrelay = "ws://relay.lan:8000/ws"\ninsecure = true\n', encoding="utf-8"
    )
    checks = {c.label: c for c in cli.run_doctor_checks(None, 4600)}
    assert checks["Relay transport encrypted"].status == "warn"
    assert not checks["Relay transport encrypted"].failed

    auth.token_path().write_text("bad", encoding="ascii")
    checks = {c.label: c for c in cli.run_doctor_checks(None, 4600)}
    assert checks["Daemon token readable"].status == "fail"
