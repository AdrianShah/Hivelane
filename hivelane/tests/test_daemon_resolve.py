"""Daemon side of the owner resolve actions: a ping (nudged) reaches the
agent's next hook output, the daemon log and a desktop notification; a hold
an owner released reads as "the room owner let you go first"; and the
desktop notifier never splices text into a script."""

from __future__ import annotations

from typing import Any

import pytest

from hivelane import daemon as daemon_module
from hivelane import desktop
from hivelane.daemon import ANY_SESSION, RoomConnection
from hivelane.events import EventAck

REL = "src/app.py"


@pytest.fixture
def shown(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(daemon_module.desktop, "notify", lambda title, body: calls.append((title, body)) or True)
    return calls


def test_a_ping_reaches_the_next_hook_the_log_and_the_desktop(
    shown: list[tuple[str, str]], capsys: pytest.CaptureFixture[str]
) -> None:
    conn = RoomConnection("ROOM", "ada")
    conn._dispatch_frame(
        {"type": "nudged", "user_id": "ada", "by": "room owner", "text": "wrap up auth.py", "timestamp": 1.0}
    )

    # Whichever session asks first, in any repo, gets it once.
    assert conn._take_session_notes("chat-1", "repo-a") == "the room owner pinged you: wrap up auth.py"
    assert conn._take_session_notes("chat-2", "repo-b") is None
    assert "the room owner pinged you: wrap up auth.py" in capsys.readouterr().out
    assert shown == [("Hivelane · ROOM", "The room owner pinged you: wrap up auth.py")]


def test_a_ping_without_text_still_says_who(shown: list[tuple[str, str]]) -> None:
    conn = RoomConnection("ROOM", "ada")
    conn._dispatch_frame({"type": "nudged", "user_id": "ada", "text": "", "timestamp": 1.0})
    assert conn._session_notes[ANY_SESSION] == "the room owner pinged you"


def test_an_owner_release_reads_as_go_first(capsys: pytest.CaptureFixture[str]) -> None:
    conn = RoomConnection("ROOM", "ada")
    conn._dispatch_frame(
        {"type": "file_released", "file_path": REL, "repo_id": "", "timestamp": 1.0, "reason": "owner"}
    )
    assert "the room owner let you go first" in capsys.readouterr().out
    note = conn._release_note(conn._local_repo_id(""), REL, "chat-1", EventAck())
    assert note == f"the room owner let you go first on {REL}, it's your turn"


def test_an_ordinary_release_reads_as_before() -> None:
    conn = RoomConnection("ROOM", "ada")
    conn._dispatch_frame({"type": "file_released", "file_path": REL, "repo_id": "", "timestamp": 1.0})
    note = conn._release_note(conn._local_repo_id(""), REL, "chat-1", EventAck())
    assert note == f"{REL} was released to you, it's your turn"


# --- hivelane/desktop.py -----------------------------------------------------------


def test_macos_passes_the_text_as_arguments_never_as_script(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(desktop.shutil, "which", lambda name: f"/usr/bin/{name}")
    hostile = 'x" & (do shell script "rm -rf ~") & "'
    argv = desktop.command_for("Hivelane", hostile, platform="darwin")
    assert argv is not None
    script = [argv[i + 1] for i, part in enumerate(argv) if part == "-e"]
    assert all(hostile not in line for line in script)
    assert argv[-2:] == ["Hivelane", hostile]


def test_linux_uses_notify_send_and_other_platforms_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(desktop.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert desktop.command_for("T", "  body\n  text ", platform="linux") == [
        "notify-send",
        "--app-name=Hivelane",
        "T",
        "body text",
    ]
    assert desktop.command_for("T", "b", platform="win32") is None
    monkeypatch.setattr(desktop.shutil, "which", lambda name: None)
    assert desktop.command_for("T", "b", platform="linux") is None


def test_notify_is_off_by_switch_and_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    spawned: list[list[str]] = []
    monkeypatch.setattr(desktop.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(desktop.sys, "platform", "linux")

    monkeypatch.setenv(desktop.ENV_SWITCH, "0")
    assert desktop.notify("T", "b", spawn=spawned.append) is False
    monkeypatch.setenv(desktop.ENV_SWITCH, "1")
    assert desktop.notify("T", "b", spawn=spawned.append) is True
    assert spawned and spawned[0][0] == "notify-send"

    def broken(_argv: list[str]) -> Any:
        raise OSError("no display")

    assert desktop.notify("T", "b", spawn=broken) is False
