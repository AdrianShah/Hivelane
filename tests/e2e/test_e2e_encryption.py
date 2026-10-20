"""End-to-end encrypted rooms through the real relay: two daemons holding the
same room key still keep each other out of the same file, the agents read real
names in the answer, and nothing the relay broadcasts (here: what a dashboard
spectator receives) carries a path, a diff or a prompt in cleartext — only
hashes and blobs the key opens."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from hivelane import crypto

if TYPE_CHECKING:
    from .conftest import Cluster

pytestmark = [
    pytest.mark.skipif(shutil.which("git") is None, reason="needs git: path keys hang off the repo root"),
    pytest.mark.skipif(not crypto.available(), reason="needs the e2e extra (cryptography)"),
]

PASSPHRASE = "a shared secret only the team has"
SETTINGS = {"block_window_seconds": 20, "warn_window_seconds": 60, "e2e": True}


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "server").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "server" / "room.py").write_text("def broadcast():\n    return 1\n", encoding="utf-8")
    return repo


def _event(kind: str, *, session: str, cwd: Path, file_path: Path | None = None, **fields: Any) -> dict[str, Any]:
    event = {"kind": kind, "agent": "claude-code", "session_id": session, "cwd": str(cwd), **fields}
    if file_path is not None:
        event["file_path"] = str(file_path)
    return event


def test_two_daemons_with_one_key_still_block_each_other(cluster: Cluster, tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    room_py = repo / "server" / "room.py"
    room = cluster.create_room()
    key = crypto.derive(PASSPHRASE, room)
    spectator = cluster.spectate(room)
    seen: list[dict[str, Any]] = []

    def record(predicate):  # type: ignore[no-untyped-def]
        def check(event: dict[str, Any]) -> bool:
            seen.append(event)
            return predicate(event)

        return check

    adrian, sam = cluster.start_daemons(
        room,
        [
            {"name": "adrian", "batch_seconds": 0.05, "settings": SETTINGS, "room_key": PASSPHRASE},
            {"name": "sam", "agent": "cursor", "batch_seconds": 0.05, "settings": SETTINGS, "room_key": PASSPHRASE},
        ],
    )

    adrian.post(_event("intent", session="s-adrian", cwd=repo, prompt="Make broadcast return the room count"))
    adrian.post(_event("pre_edit", session="s-adrian", cwd=repo, file_path=room_py,
                       old_content="def broadcast():\n    return 1\n"))
    adrian.post(_event("post_edit", session="s-adrian", cwd=repo, file_path=room_py,
                       new_content="def broadcast():\n    return len(rooms)\n"))
    update = spectator.wait_for(
        record(lambda e: e.get("type") == "activity_update" and e.get("user_id") == "adrian"),
        what="adrian's encrypted edit",
    )

    # The relay matched on the hash: sam is blocked from the same file...
    blocked = sam.post(_event("pre_edit", session="s-sam", cwd=repo / "server", file_path=room_py))
    assert blocked["decision"] == "block", blocked
    # ...and sam's agent reads it in real names, with adrian's task.
    assert blocked["file_path"] == "server/room.py"
    assert "server/room.py" in blocked["reason"]
    assert [peer["user_id"] for peer in blocked["others"]] == ["adrian"]
    assert blocked["others"][0]["prompt"] == "Make broadcast return the room count"

    decision = spectator.wait_for(
        record(lambda e: e.get("type") == "edit_decision" and e.get("user_id") == "sam"),
        what="sam's blocked check",
    )

    # What the relay broadcast: hashes and ciphertext only.
    wire = json.dumps(seen)
    for cleartext in ("server/room.py", "room.py", "broadcast", "len(rooms)", "room count"):
        assert cleartext not in wire, cleartext
    assert update["changes"][0]["file_path"] == key.hash("server/room.py")
    assert update["changes"][0]["diff"] == ""
    kind, envelope = key.open(update["e2e"])
    assert kind == "activity"
    assert envelope["changes"][0]["file_path"] == "server/room.py"
    assert "+    return len(rooms)" in envelope["changes"][0]["diff"]
    assert decision["file_path"] == key.hash("server/room.py")
    assert key.open(decision["e2e"])[1]["file_path"] == "server/room.py"
    # And without the key, a blob is just noise.
    with pytest.raises(crypto.DecryptError):
        crypto.derive("a guess", room).open(update["e2e"])
