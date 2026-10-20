"""edit_check over a real WebSocket: the reply must arrive immediately, ahead
of (and outside) the debounced hivelane_feed batches."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

try:
    from server.main import app
    from server.models import PROTOCOL_VERSION
except ImportError:  # Render rootDir=server
    from main import app
    from models import PROTOCOL_VERSION

FILE = "server/room.py"

# Frames to read before giving up: enough to step past queued feed batches.
MAX_FRAMES = 12


def _join(socket: Any, name: str, agent: str) -> None:
    socket.send_json(
        {
            "type": "join",
            "name": name,
            "agent": agent,
            "protocol_version": PROTOCOL_VERSION,
        }
    )


def _next_frame(socket: Any, type_name: str) -> dict:
    """Next top-level frame of this type, skipping debounced feed batches."""
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


def test_edit_check_blocks_on_a_peers_fresh_edit() -> None:
    """Neither join nor activity carries a repo_id (an old client): they
    validate, fall into the empty-id bucket and still coordinate."""
    client = TestClient(app)
    room_id = client.post("/rooms").json()["room_id"]

    with client.websocket_connect(f"/ws/{room_id}") as sam:
        _join(sam, "sam", "cursor")
        with client.websocket_connect(f"/ws/{room_id}") as priya:
            _join(priya, "priya", "claude-code")

            sam.send_json(
                {
                    "type": "activity",
                    "user_id": "sam",
                    "prompt": "rework the broadcast debounce",
                    "changes": [{"file_path": FILE, "diff": "@@ -1 +1 @@"}],
                    "agent": "cursor",
                }
            )
            # The feed batch carrying the activity proves the server recorded
            # the touch, so the edit_check below cannot race it.
            update = _next_feed_event(sam, "activity_update")

            priya.send_json(
                {"type": "edit_check", "request_id": "req-1", "file_path": FILE}
            )
            response = _next_frame(priya, "edit_check_response")

    assert update["repo_id"] == ""
    assert response["request_id"] == "req-1"
    assert response["decision"] == "block"
    assert response["file_path"] == FILE
    assert response["reason"]
    assert [peer["user_id"] for peer in response["others"]] == ["sam"]
    assert response["others"][0]["agent"] == "cursor"


def test_warn_decision_reaches_the_feed() -> None:
    client = TestClient(app)
    room_id = client.post("/rooms").json()["room_id"]

    with client.websocket_connect(f"/ws/{room_id}") as sam:
        _join(sam, "sam", "cursor")
        with client.websocket_connect(f"/ws/{room_id}") as priya:
            _join(priya, "priya", "claude-code")

            sam.send_json(
                {
                    "type": "intent",
                    "user_id": "sam",
                    "prompt": "refactor the Room broadcast",
                }
            )
            _next_feed_event(sam, "intent_update")

            priya.send_json(
                {"type": "edit_check", "request_id": "req-2", "file_path": FILE}
            )
            response = _next_frame(priya, "edit_check_response")
            decision = _next_feed_event(priya, "edit_decision")

    assert response["decision"] == "warn"
    assert decision["user_id"] == "priya"
    assert decision["file_path"] == FILE
    assert decision["others"] == ["sam"]


def test_joining_member_is_told_who_is_already_in_the_room() -> None:
    """A daemon that connects second must still learn its peers; without this
    catch-up its peer list starts empty and `hivelane status` misreports."""
    client = TestClient(app)
    room_id = client.post("/rooms").json()["room_id"]

    with client.websocket_connect(f"/ws/{room_id}") as first:
        _join(first, "adrian", "claude-code")
        with client.websocket_connect(f"/ws/{room_id}") as second:
            _join(second, "sam", "cursor")
            frame = second.receive_json()

    assert frame["type"] == "user_joined"
    assert frame["user_id"] == "adrian"
    assert frame["agent"] == "claude-code"
