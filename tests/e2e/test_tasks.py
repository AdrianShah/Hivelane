"""Room tasks end to end: a real relay, two daemons and a dashboard. A task's
files are reserved while it is claimed: the claimer's own agents (any lane)
edit them, a teammate's agent is blocked until the task is done, and the
dashboard sees every change."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from .conftest import Cluster

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="needs git: path keys hang off the repo root"
)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "server").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "server" / "api.py").write_text("def get_user(uid):\n    return uid\n", encoding="utf-8")
    return repo


def _pre_edit(client: Any, session: str, repo: Path, path: Path) -> dict[str, Any]:
    return client.post(
        {
            "kind": "pre_edit",
            "agent": "claude-code",
            "session_id": session,
            "cwd": str(repo),
            "file_path": str(path),
            "old_content": path.read_text(encoding="utf-8"),
        }
    )


def _task(client: Any, repo: Path, op: str, **fields: Any) -> dict[str, Any]:
    return client.call("/tasks", {"cwd": str(repo), "op": op, **fields})


def test_a_claimed_task_reserves_its_files_for_the_claimer(cluster: Cluster, tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    api = repo / "server" / "api.py"
    room = cluster.create_room()
    spectator = cluster.spectate(room)
    adrian, sam = cluster.start_daemons(
        room,
        [
            {"name": "adrian", "batch_seconds": 0.05},
            {"name": "sam", "agent": "cursor", "batch_seconds": 0.05},
        ],
    )

    limits = repo / "server" / "limits.py"
    limits.write_text("WINDOW = 60\n", encoding="utf-8")
    users = repo / "server" / "users.py"
    users.write_text("USERS = []\n", encoding="utf-8")
    files = ["server/api.py", "server/limits.py", "server/users.py"]
    created = _task(sam, repo, "create", title="Add rate limiting", files=files)
    assert created["ok"], created
    task_id = created["task"]["task_id"]
    assert created["task"]["files"] == files

    claimed = _task(adrian, repo, "claim", next=True)
    assert claimed["ok"] and claimed["task"]["task_id"] == task_id, claimed
    assert claimed["task"]["claimer"] == "adrian"
    spectator.wait_for(
        lambda e: e.get("type") == "task_update" and e["task"]["status"] == "claimed",
        what="the dashboard seeing the claim",
    )

    # adrian's first chat, then a second chat on its own lane: both may edit
    # the reserved files (each its own file: two chats asking about one file
    # at once are judged against each other like any two agents).
    assert _pre_edit(adrian, "chat-1", repo, api)["decision"] == "allow"
    assert _pre_edit(adrian, "chat-2", repo, limits)["decision"] == "allow"

    # A reserved file nobody is editing right now: the reservation decides.
    blocked = _pre_edit(sam, "sam-1", repo, users)
    assert blocked["decision"] == "block", blocked
    assert blocked["scope"] == "task"
    assert f"reserved by adrian's task {task_id}" in blocked["reason"]
    assert "hivelane task list" in blocked["suggestion"]

    # sam may not complete adrian's claim.
    refused = _task(sam, repo, "done", task_id=task_id)
    assert (refused["ok"], refused["reason"]) == (False, "not_claimer")

    done = _task(adrian, repo, "done", task_id=task_id, text="PR #12")
    assert done["ok"] and done["task"]["status"] == "done", done
    spectator.wait_for(
        lambda e: e.get("type") == "task_update" and e["task"]["status"] == "done",
        what="the dashboard seeing the task done",
    )
    assert _pre_edit(sam, "sam-1", repo, users)["decision"] != "block"

    listed = _task(sam, repo, "list", include_closed=True)
    assert [t["task_id"] for t in listed["tasks"]] == [task_id]
    assert listed["tasks"][0]["notes"][-1]["text"] == "PR #12"


def test_a_burst_of_task_commands_is_paced_not_refused(cluster: Cluster, tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    room = cluster.create_room()
    (sam,) = cluster.start_daemons(room, [{"name": "sam", "agent": "cursor", "batch_seconds": 0.05}])
    # Faster than the relay's one task change per 0.2 s: the daemon paces them.
    created = [_task(sam, repo, "create", title=f"task {i}") for i in range(4)]
    assert [c["ok"] for c in created] == [True] * 4, created
    assert [c["task"]["task_id"] for c in created] == ["T1", "T2", "T3", "T4"]
