"""The wait queue end to end: a blocked agent is told its place in line,
`hivelane wait` (the real CLI, against a real daemon and relay) returns once
the teammate's edit ages out, the next edit is the waiting agent's turn, and
the teammate's agent heard that someone was waiting."""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from hivelane import cli

if TYPE_CHECKING:
    from .conftest import Cluster

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="needs git: path keys hang off the repo root"
)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "server").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    for name in ("room.py", "other.py"):
        (repo / "server" / name).write_text(f"# {name}\n", encoding="utf-8")
    return repo


def _event(kind: str, *, session: str, cwd: Path, file_path: Path, **fields: Any) -> dict[str, Any]:
    return {
        "kind": kind,
        "agent": "claude-code",
        "session_id": session,
        "cwd": str(cwd),
        "file_path": str(file_path),
        **fields,
    }


def _edit(client: Any, session: str, repo: Path, path: Path, spectator: Any) -> None:
    client.post(_event("pre_edit", session=session, cwd=repo, file_path=path, old_content="# room.py\n"))
    client.post(_event("post_edit", session=session, cwd=repo, file_path=path, new_content="# room.py\nx = 1\n"))
    rel = path.relative_to(repo).as_posix()
    spectator.wait_for(
        lambda e: e.get("type") == "activity_update"
        and e.get("user_id") == client.name
        and any(c["file_path"] == rel for c in e.get("changes", [])),
        what=f"{client.name}'s edit to {rel}",
    )


def test_hivelane_wait_hands_the_file_over_in_turn(
    cluster: Cluster, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(tmp_path)
    room_py = repo / "server" / "room.py"
    room = cluster.create_room()
    spectator = cluster.spectate(room)
    windows = {"block_window_seconds": 2.0, "warn_window_seconds": 10.0}
    adrian, sam = cluster.start_daemons(
        room,
        [
            {"name": "adrian", "batch_seconds": 0.05, "settings": windows},
            {"name": "sam", "agent": "cursor", "batch_seconds": 0.05, "settings": windows},
        ],
    )
    spectator.wait_for(
        lambda e: e.get("type") == "room_settings" and e.get("block_window_seconds") == 2.0,
        what="the room adopting the 2 s block window",
    )

    _edit(adrian, "s-adrian", repo, room_py, spectator)
    blocked = sam.post(_event("pre_edit", session="s-sam", cwd=repo / "server", file_path=room_py))
    assert blocked["decision"] == "block", blocked
    assert blocked["queue_position"] == 1
    assert blocked["suggestion"].startswith("wait ~")
    decision = spectator.wait_for(
        lambda e: e.get("type") == "edit_decision" and e.get("user_id") == "sam",
        what="sam's block on the feed",
    )
    assert decision["queue_position"] == 1

    # What the agent was told to run, from where it runs.
    monkeypatch.chdir(repo / "server")
    started = time.monotonic()
    code = cli.main(["wait", "server/room.py", "--port", str(sam.daemon.port), "--timeout", "20"])
    waited = time.monotonic() - started
    out = capsys.readouterr().out
    assert code == 0, out
    assert "server/room.py is free" in out
    assert waited < 15, f"waited {waited:.1f}s for a 2 s block window"

    # sam's turn: let through, with the note that it is its turn.
    turn = sam.post(_event("pre_edit", session="s-sam", cwd=repo / "server", file_path=room_py))
    assert turn["decision"] != "block", turn
    assert "server/room.py was released to you" in turn["reason"]

    # adrian's agent heard sam was waiting, on its next edit anywhere.
    heads_up = adrian.post(
        _event("pre_edit", session="s-adrian", cwd=repo, file_path=repo / "server" / "other.py")
    )
    assert heads_up["decision"] == "warn", heads_up
    assert "sam's agent is waiting for server/room.py" in heads_up["reason"]


def test_hivelane_wait_times_out_while_the_file_is_held(
    cluster: Cluster, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(tmp_path)
    room_py = repo / "server" / "room.py"
    room = cluster.create_room()
    spectator = cluster.spectate(room)
    adrian, sam = cluster.start_daemons(
        room,
        [
            {"name": "adrian", "batch_seconds": 0.05},
            {"name": "sam", "agent": "cursor", "batch_seconds": 0.05},
        ],
    )
    _edit(adrian, "s-adrian", repo, room_py, spectator)
    blocked = sam.post(_event("pre_edit", session="s-sam", cwd=repo, file_path=room_py))
    assert (blocked["decision"], blocked["queue_position"]) == ("block", 1)

    monkeypatch.chdir(repo)
    code = cli.main(["wait", str(room_py), "--port", str(sam.daemon.port), "--timeout", "1"])
    captured = capsys.readouterr()
    assert code == 1
    assert "still taken" in captured.err
    assert "waiting for server/room.py (#1 in line)" in captured.out
