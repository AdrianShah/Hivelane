"""One daemon per machine, many repos, against the real relay: a developer's
single daemon adopts two repos from their .hivelane.toml, joins each repo's own
room, and a teammate in one room is blocked only by edits made in that room.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from hivelane.daemon import Daemon

from .conftest import free_port, wait_until

if TYPE_CHECKING:
    from .conftest import Cluster, DaemonClient

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="needs git: path keys hang off the repo root"
)


def _repo(path: Path, room: str, relay: str) -> Path:
    (path / "server").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / ".hivelane.toml").write_text(
        f'room = "{room}"\nrelay = "{relay}"\nblock_window_seconds = 20\n', encoding="utf-8"
    )
    for name in ("room.py", "other.py"):
        (path / "server" / name).write_text(f"# {name}\n", encoding="utf-8")
    return path.resolve()


def _event(kind: str, cwd: Path, file_path: Path | None = None, **fields: Any) -> dict[str, Any]:
    event = {"kind": kind, "agent": "claude-code", "session_id": f"s-{cwd.name}", "cwd": str(cwd), **fields}
    if file_path is not None:
        event["file_path"] = str(file_path)
    return event


def _entry(client: DaemonClient, root: Path) -> dict[str, Any] | None:
    status = client.status() or {}
    return next((r for r in status.get("repos", []) if r["root"] == str(root)), None)


def test_one_daemon_coordinates_two_repos_in_their_own_rooms(
    cluster: Cluster, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The adopted repos take their display name from here, not from whoever
    # runs the tests.
    monkeypatch.setenv("HIVELANE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HIVELANE_NAME", "adrian")
    monkeypatch.delenv("HIVELANE_RELAY", raising=False)

    room_one, room_two = cluster.create_room(), cluster.create_room()
    one = _repo(tmp_path / "one", room_one, cluster.relay.ws_url)
    two = _repo(tmp_path / "two", room_two, cluster.relay.ws_url)
    spectator_one = cluster.spectate(room_one)
    spectator_two = cluster.spectate(room_two)

    adrian, sam = cluster.run_daemons(
        [
            Daemon(port=free_port(), batch_seconds=0.05),
            Daemon(room_one, "sam", agent="cursor", server=cluster.relay.ws_url, port=free_port()),
        ]
    )

    # Nothing registered by hand: the first hook from each repo adopts it.
    adrian.post(_event("intent", one / "server", prompt="Rework the room broadcast"))
    adrian.post(_event("intent", two, prompt="Tidy the other service"))

    def adopted() -> bool:
        first, second = _entry(adrian, one), _entry(adrian, two)
        return bool(
            first and second and first["connected"] and second["connected"]
            and "sam" in first["peers"]
        )

    wait_until(adopted, what="adrian's daemon to join both rooms")
    assert (_entry(adrian, one)["room"], _entry(adrian, two)["room"]) == (room_one, room_two)

    # adrian edits server/room.py in repo one: sam, in the same room, is blocked.
    adrian.post(_event("pre_edit", one, one / "server" / "room.py", old_content="# room.py\n"))
    adrian.post(_event("post_edit", one, one / "server" / "room.py", new_content="# room.py\nx = 1\n"))
    spectator_one.wait_for(
        lambda e: e.get("type") == "activity_update" and e.get("user_id") == "adrian",
        what="adrian's edit in room one",
    )
    blocked = sam.post(_event("pre_edit", one / "server", one / "server" / "room.py"))
    assert blocked["decision"] == "block", blocked
    assert blocked["file_path"] == "server/room.py"

    # adrian edits server/other.py in repo two: that is room two's business.
    adrian.post(_event("pre_edit", two, two / "server" / "other.py", old_content="# other.py\n"))
    adrian.post(_event("post_edit", two, two / "server" / "other.py", new_content="# other.py\ny = 2\n"))
    spectator_two.wait_for(
        lambda e: e.get("type") == "activity_update" and e.get("user_id") == "adrian",
        what="adrian's edit in room two",
    )
    allowed = sam.post(_event("pre_edit", one / "server", one / "server" / "other.py"))
    assert allowed["decision"] == "allow", allowed
