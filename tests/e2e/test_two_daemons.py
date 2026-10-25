"""The claim the product rests on, end to end: an agent is denied an edit to a
file a teammate just touched, even when the two run from different directories
of the repo, and the answer comes back fast enough to sit on a blocking hook.

Real relay, real daemons, real sockets; only the agents are simulated, by
posting the events their hooks would post. scripts/smoke.sh covers the same
path through the installed CLI.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from .conftest import Cluster

# The hook blocks the agent while it waits, so a check has to be imperceptible.
# Loopback round trips take a few milliseconds. This is a generous guard
# against a catastrophic slowdown (a lost frame waiting out a timeout, a
# blocking call on the loop), not a benchmark: slow Windows/macOS CI runners
# miss a tight wall-clock budget for reasons unrelated to the code.
EDIT_CHECK_BUDGET_SECONDS = 1.0

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="needs git: path keys hang off the repo root"
)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "server").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    for name in ("room.py", "other.py", "utils.py"):
        (repo / "server" / name).write_text(f"# {name}\n", encoding="utf-8")
    return repo


def _event(
    kind: str,
    *,
    session: str,
    cwd: Path,
    file_path: Path | None = None,
    **fields: Any,
) -> dict[str, Any]:
    """A NormalizedEvent as the Claude Code adapter posts it: absolute paths,
    plus the cwd the agent is running in."""
    event = {
        "kind": kind,
        "agent": "claude-code",
        "session_id": session,
        "cwd": str(cwd),
        **fields,
    }
    if file_path is not None:
        event["file_path"] = str(file_path)
    return event


def test_teammates_are_kept_out_of_each_others_files(
    cluster: Cluster, tmp_path: Path
) -> None:
    repo = _repo(tmp_path)
    room_py = repo / "server" / "room.py"
    room = cluster.create_room()
    spectator = cluster.spectate(room)
    adrian, sam = cluster.start_daemons(
        room,
        [
            # Short batch window: the debounce is not what is under test.
            {"name": "adrian", "batch_seconds": 0.05},
            {"name": "sam", "agent": "cursor", "batch_seconds": 0.05},
        ],
    )

    # adrian edits server/room.py, running from the repo root.
    first = adrian.post(
        _event("pre_edit", session="s-adrian", cwd=repo, file_path=room_py,
               old_content="# room.py\n")
    )
    assert first["decision"] == "allow"
    adrian.post(
        _event("post_edit", session="s-adrian", cwd=repo, file_path=room_py,
               new_content="# room.py\ndef broadcast():\n    return 2\n")
    )
    spectator.wait_for(
        lambda e: e.get("type") == "activity_update"
        and e.get("user_id") == "adrian"
        and any(c["file_path"] == "server/room.py" for c in e.get("changes", [])),
        what="adrian's edit to server/room.py",
    )

    # sam, running from repo/server, reaches for the same file.
    started = time.perf_counter()
    blocked = sam.post(
        _event("pre_edit", session="s-sam", cwd=repo / "server", file_path=room_py)
    )
    elapsed = time.perf_counter() - started

    assert blocked["decision"] == "block", blocked
    assert blocked["file_path"] == "server/room.py"
    assert [peer["user_id"] for peer in blocked["others"]] == ["adrian"]
    assert "server/room.py" in blocked["reason"]
    assert elapsed < EDIT_CHECK_BUDGET_SECONDS, (
        f"edit check took {elapsed * 1000:.0f} ms; the hook budget is "
        f"{EDIT_CHECK_BUDGET_SECONDS * 1000:.0f} ms"
    )

    # A file nobody is in stays open.
    other = sam.post(
        _event("pre_edit", session="s-sam", cwd=repo / "server",
               file_path=repo / "server" / "other.py")
    )
    assert other["decision"] == "allow", other

    # A declared intent naming a file warns before anyone has touched it.
    sam.post(
        _event("intent", session="s-sam", cwd=repo / "server",
               prompt="Rework the helpers in utils.py")
    )
    spectator.wait_for(
        lambda e: e.get("type") == "intent_update" and e.get("user_id") == "sam",
        what="sam's intent",
    )
    warned = adrian.post(
        _event("pre_edit", session="s-adrian", cwd=repo,
               file_path=repo / "server" / "utils.py")
    )
    assert warned["decision"] == "warn", warned
    assert [peer["user_id"] for peer in warned["others"]] == ["sam"]
