"""Repo identity on the relay: the same relative path in two different repos is
two different files. Without repo_id, two projects that both have src/main.py
would block each other's agents for no reason."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

try:
    from server.main import app
    from server.models import PROTOCOL_VERSION, ActivityMessage, FileChange
    from server.room import Room
except ImportError:  # Render rootDir=server
    from main import app
    from models import PROTOCOL_VERSION, ActivityMessage, FileChange
    from room import Room

FILE = "src/main.py"
REPO_A = "a1b2c3d4e5f60718"
REPO_B = "0f1e2d3c4b5a6978"

MAX_FRAMES = 12


class FakeSocket:
    """Room only stores member sockets; these tests never broadcast."""


def _room(*members: str) -> Room:
    room = Room()
    for name in members:
        room.add_member(name, FakeSocket(), "claude-code")
    return room


def _touch(room: Room, user_id: str, repo_id: str, timestamp: float = 1000.0) -> None:
    room.record_activity(
        ActivityMessage(
            user_id=user_id,
            timestamp=timestamp,
            changes=[FileChange(file_path=FILE, diff="@@ -1 +1 @@")],
            repo_id=repo_id,
        )
    )


# --- Room policy --------------------------------------------------------------


def test_same_path_in_different_repos_is_allowed() -> None:
    room = _room("sam", "priya")
    _touch(room, "sam", REPO_A)

    decision, peers = room.check_edit("priya", FILE, now=1002.0, repo_id=REPO_B)

    assert decision == "allow"
    assert peers == []


def test_same_path_in_the_same_repo_is_blocked() -> None:
    room = _room("sam", "priya")
    _touch(room, "sam", REPO_A)

    decision, peers = room.check_edit("priya", FILE, now=1002.0, repo_id=REPO_A)

    assert decision == "block"
    assert [peer.user_id for peer in peers] == ["sam"]
    assert peers[0].repo_id == REPO_A


def test_legacy_clients_without_repo_id_still_block_each_other() -> None:
    """Two pre-repo_id clients must keep today's behaviour."""
    room = _room("sam", "priya")
    _touch(room, "sam", "")

    decision, _ = room.check_edit("priya", FILE, now=1002.0)

    assert decision == "block"


def test_empty_repo_id_matches_only_empty() -> None:
    room = _room("sam", "priya")
    _touch(room, "sam", "")

    assert room.check_edit("priya", FILE, now=1002.0, repo_id=REPO_A)[0] == "allow"

    _touch(room, "sam", REPO_A, timestamp=1003.0)
    assert room.check_edit("priya", FILE, now=1004.0, repo_id="")[0] == "block"


def test_one_user_in_two_repos_keeps_both_touches() -> None:
    """A multi-repo daemon editing the same path in two repos must not have
    the second touch overwrite the first."""
    room = _room("sam", "priya")
    _touch(room, "sam", REPO_A)
    _touch(room, "sam", REPO_B, timestamp=1001.0)

    assert room.check_edit("priya", FILE, now=1002.0, repo_id=REPO_A)[0] == "block"
    assert room.check_edit("priya", FILE, now=1002.0, repo_id=REPO_B)[0] == "block"


def test_overlaps_are_only_found_within_a_repo() -> None:
    room = _room("sam", "priya")
    _touch(room, "sam", REPO_A)

    def overlaps(repo_id: str) -> int:
        return len(
            room.find_overlaps(
                ActivityMessage(
                    user_id="priya",
                    timestamp=1002.0,
                    changes=[FileChange(file_path=FILE, diff="@@ -2 +2 @@")],
                    repo_id=repo_id,
                )
            )
        )

    assert overlaps(REPO_B) == 0
    assert overlaps(REPO_A) == 1


def test_intent_in_another_repo_does_not_warn() -> None:
    room = _room("sam", "priya")
    room.record_intent("sam", "rewrite main.py", timestamp=1000.0, repo_id=REPO_A)

    assert room.check_edit("priya", FILE, now=1002.0, repo_id=REPO_B)[0] == "allow"


def test_intent_in_the_same_repo_warns() -> None:
    room = _room("sam", "priya")
    room.record_intent("sam", "rewrite main.py", timestamp=1000.0, repo_id=REPO_A)

    decision, peers = room.check_edit("priya", FILE, now=1002.0, repo_id=REPO_A)

    assert decision == "warn"
    assert peers[0].repo_id == REPO_A


def test_legacy_intent_still_warns_legacy_peer() -> None:
    room = _room("sam", "priya")
    room.record_intent("sam", "rewrite main.py", timestamp=1000.0)

    assert room.check_edit("priya", FILE, now=1002.0)[0] == "warn"


def test_record_intent_keeps_repo_id_for_catch_up() -> None:
    room = _room("sam")
    update = room.record_intent("sam", "rewrite main.py", timestamp=1.0, repo_id=REPO_A)

    assert update.repo_id == REPO_A
    assert room.latest_intents() == [update]


# --- over the wire ------------------------------------------------------------


def _join(socket: Any, name: str, repo_id: str = "") -> None:
    socket.send_json(
        {
            "type": "join",
            "name": name,
            "agent": "claude-code",
            "protocol_version": PROTOCOL_VERSION,
            "repo_id": repo_id,
        }
    )


def _next_frame(socket: Any, type_name: str) -> dict:
    for _ in range(MAX_FRAMES):
        frame = socket.receive_json()
        if frame.get("type") == type_name:
            return frame
    raise AssertionError(f"never received a top-level {type_name} frame")


def _next_feed_event(socket: Any, type_name: str) -> dict:
    for _ in range(MAX_FRAMES):
        frame = socket.receive_json()
        if frame.get("type") != "hivelane_feed":
            continue
        for event in frame["events"]:
            if event.get("type") == type_name:
                return event
    raise AssertionError(f"never received a {type_name} feed event")


def _edit_check(socket: Any, request_id: str, repo_id: str) -> dict:
    socket.send_json(
        {
            "type": "edit_check",
            "request_id": request_id,
            "file_path": FILE,
            "repo_id": repo_id,
        }
    )
    return _next_frame(socket, "edit_check_response")


def test_relay_keys_edit_checks_on_repo_id() -> None:
    client = TestClient(app)
    room_id = client.post("/rooms").json()["room_id"]

    with client.websocket_connect(f"/ws/{room_id}") as sam:
        _join(sam, "sam", REPO_A)
        with client.websocket_connect(f"/ws/{room_id}") as priya:
            _join(priya, "priya", REPO_B)

            sam.send_json(
                {
                    "type": "activity",
                    "user_id": "sam",
                    "changes": [{"file_path": FILE, "diff": "@@ -1 +1 @@"}],
                    "agent": "cursor",
                    "repo_id": REPO_A,
                }
            )
            update = _next_feed_event(sam, "activity_update")

            other_repo = _edit_check(priya, "r1", REPO_B)
            same_repo = _edit_check(priya, "r2", REPO_A)
            decision = _next_feed_event(priya, "edit_decision")

    assert update["repo_id"] == REPO_A
    assert other_repo["decision"] == "allow"
    assert same_repo["decision"] == "block"
    assert decision["repo_id"] == REPO_A


def test_intent_broadcast_carries_repo_id() -> None:
    client = TestClient(app)
    room_id = client.post("/rooms").json()["room_id"]

    with client.websocket_connect(f"/ws/{room_id}") as sam:
        _join(sam, "sam", REPO_A)
        sam.send_json(
            {"type": "intent", "user_id": "sam", "prompt": "x", "repo_id": REPO_A}
        )
        intent = _next_feed_event(sam, "intent_update")

    assert intent["repo_id"] == REPO_A
