"""Lane metadata: a member's parent chat, subagent type and worktree reach
every dashboard, and a member_update relabels a member already in the room
(a daemon joins before any agent has told it which agent it is)."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

try:
    from server.main import app
    from server.models import PROTOCOL_VERSION
except ImportError:  # Render rootDir=server
    from main import app
    from models import PROTOCOL_VERSION

MAX_FRAMES = 12


def _join(socket: Any, name: str, agent: str, **meta: str) -> None:
    socket.send_json(
        {"type": "join", "name": name, "agent": agent, "protocol_version": PROTOCOL_VERSION, **meta}
    )


def _user_joined(socket: Any, user_id: str) -> dict:
    """The next user_joined for user_id, top-level or inside a feed batch."""
    for _ in range(MAX_FRAMES):
        frame = socket.receive_json()
        events = frame["events"] if frame.get("type") == "hivelane_feed" else [frame]
        for item in events:
            if item.get("type") == "user_joined" and item.get("user_id") == user_id:
                return item
    raise AssertionError(f"never received user_joined for {user_id}")


def test_subagent_metadata_reaches_a_dashboard() -> None:
    client = TestClient(app)
    room_id = client.post("/rooms").json()["room_id"]

    with client.websocket_connect(f"/ws/{room_id}") as main:
        _join(main, "ada", "claude-code")
        with client.websocket_connect(f"/ws/{room_id}") as sub:
            _join(sub, "ada-explore-9f31", "claude-code", parent="ada", subagent_type="Explore", worktree="ghost-merge")
            # A member that joins later is told about both, metadata included.
            with client.websocket_connect(f"/ws/{room_id}") as late:
                _join(late, "sam", "codex")
                seen = _user_joined(late, "ada-explore-9f31")
                assert (seen["parent"], seen["subagent_type"], seen["worktree"]) == ("ada", "Explore", "ghost-merge")
                plain = _user_joined(main, "sam")
                assert (plain["parent"], plain["subagent_type"], plain["worktree"]) == ("", "", "")


def test_member_update_relabels_a_member() -> None:
    client = TestClient(app)
    room_id = client.post("/rooms").json()["room_id"]

    with client.websocket_connect(f"/ws/{room_id}") as watcher:
        _join(watcher, "sam", "codex")
        with client.websocket_connect(f"/ws/{room_id}") as daemon:
            _join(daemon, "ada", "unknown")
            assert _user_joined(watcher, "ada")["agent"] == "unknown"
            daemon.send_json({"type": "member_update", "agent": "claude-code", "worktree": "ghost-merge"})
            relabelled = _user_joined(watcher, "ada")
            assert (relabelled["agent"], relabelled["worktree"]) == ("claude-code", "ghost-merge")
            # Unknown never overwrites a known agent; an unchanged update is not re-announced.
            daemon.send_json({"type": "member_update", "agent": "unknown", "worktree": "ghost-merge"})
            daemon.send_json({"type": "member_update", "agent": "claude-code", "worktree": ""})
            last = _user_joined(watcher, "ada")
            assert (last["agent"], last["worktree"]) == ("claude-code", "")


def test_overlong_metadata_is_refused() -> None:
    client = TestClient(app)
    room_id = client.post("/rooms").json()["room_id"]

    with client.websocket_connect(f"/ws/{room_id}") as watcher:
        _join(watcher, "sam", "codex")
        with client.websocket_connect(f"/ws/{room_id}") as daemon:
            _join(daemon, "ada", "claude-code")
            _user_joined(watcher, "ada")
            daemon.send_json({"type": "member_update", "agent": "claude-code", "worktree": "x" * 500})
            daemon.send_json({"type": "member_update", "agent": "codex", "worktree": ""})
            # The oversized update was dropped; the next valid one is what arrives.
            assert _user_joined(watcher, "ada")["agent"] == "codex"


def test_a_chat_and_its_own_subagent_never_conflict() -> None:
    try:
        from server.models import ActivityMessage, FileChange
        from server.room import Room
    except ImportError:  # Render rootDir=server
        from models import ActivityMessage, FileChange  # type: ignore[no-redef]
        from room import Room  # type: ignore[no-redef]

    room = Room()
    for name, parent in (("ada", ""), ("ada-explore-1", "ada"), ("ada-plan-2", "ada"), ("sam", "")):
        room.add_member(name, object(), "claude-code", {"parent": parent})  # type: ignore[arg-type]

    def touch(user: str) -> ActivityMessage:
        return ActivityMessage(user_id=user, changes=[FileChange(file_path="app.py", change_type="modified", diff="@@ -1 +1 @@\n-a\n+b\n")])

    room.record_activity(touch("ada"))
    assert room.find_overlaps(touch("ada-explore-1")) == []  # its own chat: a hand-off
    room.record_activity(touch("ada-explore-1"))
    assert [o.other.user_id for o in room.find_overlaps(touch("ada-plan-2"))] == ["ada-explore-1"]  # siblings
    assert room.find_overlaps(touch("sam"))  # anyone else still overlaps
