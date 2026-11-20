"""`hivelane status --line` (the bee in agent apps: Claude Code's status line,
a shell prompt) and `hivelane up`'s offer to put it in Claude Code's status
line without ever replacing one the person set."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hivelane import cli


def repo(root: Path, **extra: object) -> dict[str, object]:
    return {"root": str(root), "room": "K7M2PQ2X", "connected": True, "peers": ["maya", "tomas"], "held": 0, **extra}


def test_the_line_names_the_room_its_agents_and_held_edits(tmp_path: Path) -> None:
    status = {"repos": [repo(tmp_path)]}
    assert cli.format_status_line(status, tmp_path, color=False) == "⬡ ● K7M2PQ2X · 3 agents"
    status = {"repos": [repo(tmp_path, held=1, peers=[])]}
    assert cli.format_status_line(status, tmp_path / "src", color=False) == "⬡ ● K7M2PQ2X · 1 agent · 1 held"
    coloured = cli.format_status_line(status, tmp_path, color=True)
    assert "\x1b[31m●\x1b[0m" in coloured  # red: an edit is held


def test_the_line_picks_the_repo_you_are_in(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    status = {"repos": [repo(a, room="AAAAAAAA"), repo(b, room="BBBBBBBB")]}
    assert "BBBBBBBB" in cli.format_status_line(status, b / "deep" / "dir", color=False)
    assert "AAAAAAAA" in cli.format_status_line(status, tmp_path / "elsewhere", color=False)


def test_the_line_never_fails(tmp_path: Path) -> None:
    assert cli.format_status_line(None, tmp_path, color=False) == "⬡ ○ hivelane off"
    assert cli.format_status_line({"repos": []}, tmp_path, color=False) == "⬡ ○ no room here"
    assert cli.format_status_line({"repos": [repo(tmp_path, connected=False)]}, tmp_path, color=False).endswith(
        "connecting"
    )
    assert cli.format_status_line({"repos": [repo(tmp_path, fatal="room gone")]}, tmp_path, color=False).endswith(
        "stopped"
    )
    # The single-room `hivelane join` daemon's flat shape.
    flat = {"room": "ZZZZZZZZ", "connected": True, "peers": [], "root": str(tmp_path)}
    assert cli.format_status_line(flat, tmp_path, color=False) == "⬡ ● ZZZZZZZZ · 1 agent"


def test_status_line_command_prints_and_exits_zero_without_a_daemon(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class NoDaemon:
        @staticmethod
        def status(port: int, timeout: float) -> None:
            return None

    monkeypatch.setattr(cli, "load_service", lambda: NoDaemon)
    assert cli.main(["status", "--line", "--plain"]) == 0
    assert capsys.readouterr().out.strip() == "⬡ ○ hivelane off"
    assert cli.main(["status", "--prompt-snippet"]) == 0
    assert "hivelane status --line --plain" in capsys.readouterr().out


def settings(root: Path) -> dict[str, object]:
    return json.loads((root / ".claude" / "settings.json").read_text(encoding="utf-8"))


def test_up_adds_the_status_line_only_when_asked(tmp_path: Path) -> None:
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text('{"hooks": {}}', encoding="utf-8")

    cli.offer_status_line(tmp_path, choice=False)
    assert "statusLine" not in settings(tmp_path)
    # Not a terminal and no flag: the default is no.
    cli.offer_status_line(tmp_path, choice=None)
    assert "statusLine" not in settings(tmp_path)

    cli.offer_status_line(tmp_path, choice=True)
    line = settings(tmp_path)["statusLine"]
    assert isinstance(line, dict) and line["type"] == "command"
    assert line["command"].endswith("status --line")
    assert settings(tmp_path)["hooks"] == {}


def test_up_never_replaces_someone_elses_status_line(tmp_path: Path) -> None:
    (tmp_path / ".claude").mkdir()
    mine = {"statusLine": {"type": "command", "command": "~/bin/my-line"}}
    (tmp_path / ".claude" / "settings.json").write_text(json.dumps(mine), encoding="utf-8")
    cli.offer_status_line(tmp_path, choice=True)
    assert settings(tmp_path) == mine
