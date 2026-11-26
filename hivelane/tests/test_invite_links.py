"""Invite links on the CLI and daemon side: building and parsing the share
link, keeping its secrets in the personal config (never .hivelane.toml),
`hivelane invite [--rotate]`, and the daemon presenting and remembering the
member token the relay issues."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from hivelane import cli, links
from hivelane import config as cfg
from hivelane import service
from hivelane.daemon import MEMBER_REVOKED, RoomConnection
from hivelane.links import InviteTarget, invite_url, parse_invite_link
from hivelane.tests.cli_fakes import Machine, machine  # noqa: F401  (fixture)

SECRET = "inv-Zq8Tn4xK1pLw0aB7cD2eF5gH9jM3rS6u"
DASH = "https://adrianshah.github.io/Hivelane"


def personal(m: Machine) -> dict:
    path = m.home / ".hivelane" / "config.toml"
    return tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


# --- the share link ---------------------------------------------------------------


@pytest.fixture
def hosted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(links.DASHBOARD_ENV, raising=False)
    monkeypatch.setattr(links, "local_relay_dashboard", lambda relay: None)


@pytest.mark.usefixtures("hosted")
def test_the_share_link_carries_the_invite_in_the_fragment() -> None:
    url = invite_url("wss://relay.example/ws", "k7q2-xm9p", invite=SECRET)

    assert url == f"{DASH}/j/K7Q2XM9P?relay=wss%3A%2F%2Frelay.example%2Fws#invite={SECRET}"


@pytest.mark.usefixtures("hosted")
def test_the_share_link_combines_the_e2e_key_and_the_invite() -> None:
    url = invite_url("wss://relay.example/ws", "K7Q2XM9P", key="pass phrase&x", invite=SECRET)

    assert url.endswith(f"#key=pass%20phrase%26x&invite={SECRET}")
    target = parse_invite_link(url)
    assert target == InviteTarget("K7Q2XM9P", "wss://relay.example/ws", "pass phrase&x", SECRET)


@pytest.mark.usefixtures("hosted")
def test_no_secrets_no_fragment() -> None:
    assert "#" not in invite_url("wss://relay.example/ws", "K7Q2XM9P")


@pytest.mark.parametrize(
    "link,key,invite",
    [
        (f"{DASH}/j/K7Q2XM9P#invite={SECRET}", None, SECRET),
        (f"{DASH}/r/K7Q2XM9P#invite={SECRET}&key=abc", "abc", SECRET),
        (f"{DASH}/j/K7Q2XM9P#key=abc", "abc", None),
        (f"{DASH}/j/K7Q2XM9P#k=abc", "abc", None),
        (f"{DASH}/j/K7Q2XM9P#abc", "abc", None),
        (f"{DASH}/j/K7Q2XM9P#invite=", None, None),
        (f"{DASH}/j/K7Q2XM9P", None, None),
    ],
)
def test_parsing_tells_the_key_and_the_invite_apart(link: str, key: str, invite: str) -> None:
    target = parse_invite_link(link)

    assert (target.room, target.key, target.invite) == ("K7Q2XM9P", key, invite)


def test_a_bare_code_has_no_secrets() -> None:
    assert parse_invite_link("k7q2-xm9p") == InviteTarget("K7Q2XM9P")


# --- `hivelane up` --------------------------------------------------------------------


def test_creating_a_room_saves_the_invite_privately_and_prints_the_share_line(
    machine: Machine, capsys: pytest.CaptureFixture[str]
) -> None:
    machine.relay.owner_token = "own-3f9a"
    machine.relay.invite_secret = SECRET

    assert cli.main(["up"]) == 0

    out = capsys.readouterr().out
    assert personal(machine)["invite_secrets"] == {"K7Q2XM": SECRET}
    assert personal(machine)["owner_tokens"] == {"K7Q2XM": "own-3f9a"}
    assert "the line to share" in out
    share = next(line.strip() for line in out.splitlines() if "/j/K7Q2XM" in line)
    assert share.endswith(f"#invite={SECRET}")
    assert "anyone with just the code waits for you" in out
    (_, _, _, config) = machine.service.called("connect_repo")[0]
    assert config.invite_secret() == SECRET


def test_writing_the_room_to_the_project_file_warns_that_the_code_alone_only_knocks(
    machine: Machine, capsys: pytest.CaptureFixture[str]
) -> None:
    machine.relay.owner_token = "own-3f9a"
    machine.relay.invite_secret = SECRET

    assert cli.main(["up", "--save-project"]) == 0

    out = capsys.readouterr().out
    assert "does not let anyone in" in out
    committed = machine.project_file().read_text(encoding="utf-8")
    assert SECRET not in committed and "own-3f9a" not in committed


def test_up_room_accepts_an_invite_link(
    machine: Machine, capsys: pytest.CaptureFixture[str]
) -> None:
    link = f"{DASH}/j/P3RT8Z?relay=wss%3A%2F%2Frelay.example%2Fws#invite={SECRET}"

    assert cli.main(["up", "--room", link]) == 0

    out = capsys.readouterr().out
    assert "joining room P3RT8Z" in out
    assert "without waiting" in out
    assert personal(machine)["invite_secrets"] == {"P3RT8Z": SECRET}
    assert not machine.project_file().exists()
    (_, _, _, config) = machine.service.called("connect_repo")[0]
    assert (config.room, config.relay) == ("P3RT8Z", "wss://relay.example/ws")
    assert config.invite_secret() == SECRET
    # The registration hands it to the daemon, which presents it on join.
    body = service.registration(machine.repo, config)
    assert body["invite_secret"] == SECRET
    assert body["member_token"] is None


def test_up_room_with_a_bad_link_is_a_config_error(
    machine: Machine, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["up", "--room", "https://example.com/nothing-here"]) == 2
    assert "room code" in capsys.readouterr().err


def test_join_saves_the_invite_from_the_link(
    machine: Machine, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "repo_id_for_root", lambda root: "repo-inv-1")

    assert cli.main(["join", f"{DASH}/j/K7Q2XM#invite={SECRET}", "--yes"]) == 0

    assert personal(machine)["invite_secrets"] == {"K7Q2XM": SECRET}
    assert not machine.project_file().exists()
    (_, _, _, config) = machine.service.called("connect_repo")[0]
    assert config.invite_secret() == SECRET


def test_invite_secrets_in_the_project_file_are_refused(
    machine: Machine, capsys: pytest.CaptureFixture[str]
) -> None:
    machine.project_file().write_text(
        f'room = "K7Q2XM"\ninvite_secrets = {{ K7Q2XM = "{SECRET}" }}\n', encoding="utf-8"
    )

    assert cli.main(["up"]) == 2
    assert "must never be in the committed project file" in capsys.readouterr().err


# --- `hivelane invite` -------------------------------------------------------------------


def _own_room(machine: Machine, secret: str = SECRET) -> None:
    machine.project_file().write_text('room = "K7Q2XM"\n', encoding="utf-8")
    cfg.set_personal_secret("owner_tokens", "K7Q2XM", "own-3f9a")
    machine.relay.owner_token = "own-3f9a"
    machine.relay.invite_secret = secret


def test_invite_prints_the_owners_current_link_from_the_relay(
    machine: Machine, capsys: pytest.CaptureFixture[str]
) -> None:
    _own_room(machine, secret="rotated-on-the-dashboard")
    cfg.set_personal_secret("invite_secrets", "K7Q2XM", "stale-one")

    assert cli.main(["invite", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["url"].endswith("#invite=rotated-on-the-dashboard")
    assert (payload["invite"], payload["owner"]) == (True, True)
    assert personal(machine)["invite_secrets"] == {"K7Q2XM": "rotated-on-the-dashboard"}


def test_invite_rotate_replaces_the_link(
    machine: Machine, capsys: pytest.CaptureFixture[str]
) -> None:
    _own_room(machine)
    machine.relay.rotations = ["brand-new-secret"]

    assert cli.main(["invite", "--rotate"]) == 0

    captured = capsys.readouterr()
    assert captured.out.strip().endswith("#invite=brand-new-secret")
    assert "rotated" in captured.err
    assert personal(machine)["invite_secrets"] == {"K7Q2XM": "brand-new-secret"}
    rotate = [r for r in machine.relay.requests if r.url.path.endswith("/invite/rotate")]
    assert len(rotate) == 1 and rotate[0].headers["x-hivelane-owner-token"] == "own-3f9a"


def test_only_an_owner_may_rotate(machine: Machine, capsys: pytest.CaptureFixture[str]) -> None:
    machine.project_file().write_text('room = "K7Q2XM"\n', encoding="utf-8")

    assert cli.main(["invite", "--rotate"]) == 2
    assert "only an owner" in capsys.readouterr().err


def test_a_non_owner_shares_the_link_they_were_given(
    machine: Machine, capsys: pytest.CaptureFixture[str]
) -> None:
    machine.project_file().write_text('room = "K7Q2XM"\n', encoding="utf-8")
    cfg.set_personal_secret("invite_secrets", "K7Q2XM", SECRET)

    assert cli.main(["invite"]) == 0

    assert capsys.readouterr().out.strip().endswith(f"#invite={SECRET}")
    # Not an owner: nothing asked of the relay.
    assert not [r for r in machine.relay.requests if "/invite" in r.url.path]


def test_without_any_invite_the_link_says_it_will_wait(
    machine: Machine, capsys: pytest.CaptureFixture[str]
) -> None:
    machine.project_file().write_text('room = "K7Q2XM"\n', encoding="utf-8")

    assert cli.main(["invite"]) == 0

    captured = capsys.readouterr()
    assert "#invite=" not in captured.out
    assert "waits for an owner's approval" in captured.err


# --- the daemon --------------------------------------------------------------------------


def test_the_join_presents_the_invite_and_the_member_token() -> None:
    conn = RoomConnection("K7Q2XM", "ada", invite_secret=SECRET, member_token="mem-1")

    payload = json.loads(conn._join_payload())

    assert payload["invite_secret"] == SECRET
    assert payload["member_token"] == "mem-1"
    bare = json.loads(RoomConnection("K7Q2XM", "ada")._join_payload())
    assert "invite_secret" not in bare and "member_token" not in bare


def test_an_issued_member_token_is_kept_in_the_personal_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HIVELANE_HOME", str(tmp_path))
    conn = RoomConnection("K7Q2XM", "ada", invite_secret=SECRET, remember_tokens=True)

    conn._dispatch_frame({"type": "member_token", "member_id": "m1", "token": "mem-42"})

    assert conn.member_token == "mem-42"
    assert json.loads(conn._join_payload())["member_token"] == "mem-42"
    assert cfg.load_config(tmp_path).member_token("K7Q2XM") == "mem-42"


def test_a_revoked_member_forgets_its_token_and_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HIVELANE_HOME", str(tmp_path))
    cfg.set_personal_secret("member_tokens", "K7Q2XM", "mem-42")
    conn = RoomConnection("K7Q2XM", "ada", member_token="mem-42", remember_tokens=True)

    conn._dispatch_frame({"type": "room_rejected", "reason": MEMBER_REVOKED})

    assert conn.fatal is not None and "removed this machine" in conn.fatal
    assert conn.member_token is None
    assert cfg.load_config(tmp_path).member_token("K7Q2XM") is None


def test_room_access_is_kept_only_by_an_owner() -> None:
    frame = {"type": "room_access", "invite_required": True, "invite_secret": SECRET, "members": []}
    owner = RoomConnection("K7Q2XM", "ada", owner_token="own")
    member = RoomConnection("K7Q2XM", "sam")

    owner._dispatch_frame(frame)
    member._dispatch_frame(frame)

    assert owner.room_access == frame
    assert member.room_access is None
