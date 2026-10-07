"""The relay's side of end-to-end encrypted rooms: it copies each sender's
opaque `e2e` blob onto every frame derived from that sender's message, never
looks inside, keeps the room's e2e flag (which a join may set but never
clear), and sends nothing from an e2e room to a third party (Gemini)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, Optional

import pytest
from fastapi.testclient import TestClient

try:
    from server import main
    from server.main import app
    from server.models import PROTOCOL_VERSION, ActivityMessage, FileChange, RoomSettings
    from server.room import Room
    from server.room_manager import RoomManager
    from server.store import MemoryRoomStore
except ImportError:  # Render rootDir=server
    import main  # type: ignore[no-redef]
    from main import app  # type: ignore[no-redef]
    from models import (  # type: ignore[no-redef]
        PROTOCOL_VERSION,
        ActivityMessage,
        FileChange,
        RoomSettings,
    )
    from room import Room  # type: ignore[no-redef]
    from room_manager import RoomManager  # type: ignore[no-redef]
    from store import MemoryRoomStore  # type: ignore[no-redef]

MAX_FRAMES = 20
FILE = "h:0a1b2c3d4e5f60718293a4b5c6d7e8f9"  # what an e2e daemon sends as a path
OTHER = "h:ffeeddccbbaa99887766554433221100"


@pytest.fixture
def relay(monkeypatch: pytest.MonkeyPatch) -> Iterator[RoomManager]:
    manager = RoomManager(store=MemoryRoomStore())
    monkeypatch.setattr(main, "room_manager", manager)
    yield manager


def _join(socket: Any, name: str, settings: Optional[dict] = None) -> None:
    frame: dict[str, Any] = {
        "type": "join",
        "name": name,
        "agent": "claude-code",
        "protocol_version": PROTOCOL_VERSION,
    }
    if settings is not None:
        frame["settings"] = settings
    socket.send_json(frame)


def _next_event(socket: Any, type_name: str) -> dict:
    for _ in range(MAX_FRAMES):
        frame = socket.receive_json()
        events = frame["events"] if frame.get("type") == "synergo_feed" else [frame]
        for event in events:
            if event.get("type") == type_name:
                return event
    raise AssertionError(f"never received {type_name}")


def _activity(file_path: str, blob: str) -> dict:
    return {
        "type": "activity",
        "user_id": "spoofed",
        "changes": [{"file_path": file_path, "diff": ""}],
        "e2e": blob,
    }


# --- pass-through ------------------------------------------------------------------


def test_every_message_type_carries_its_senders_blob(relay: RoomManager) -> None:
    client = TestClient(app)
    room_id = client.post("/rooms", json={"settings": {"e2e": True}}).json()["room_id"]

    with client.websocket_connect(f"/ws/{room_id}") as sam:
        _join(sam, "sam")
        with client.websocket_connect(f"/ws/{room_id}") as priya:
            _join(priya, "priya")

            # activity -> activity_update
            sam.send_json(_activity(FILE, "BLOB-sam-activity"))
            assert _next_event(priya, "activity_update")["e2e"] == "BLOB-sam-activity"

            # edit_check -> edit_check_response.others[] (the peer's activity
            # blob) and the edit_decision broadcast (the asker's own blob)
            priya.send_json(
                {
                    "type": "edit_check",
                    "request_id": "r1",
                    "file_path": FILE,
                    "e2e": "BLOB-priya-check",
                }
            )
            response = _next_event(priya, "edit_check_response")
            assert response["decision"] == "block"
            assert [peer["e2e"] for peer in response["others"]] == ["BLOB-sam-activity"]
            decision = _next_event(sam, "edit_decision")
            assert decision["e2e"] == "BLOB-priya-check"

            # plan -> plan_update; a colliding plan's plan_conflicts carry the
            # other member's plan blob (planned) and activity blob (edited)
            sam.send_json(
                {"type": "plan", "user_id": "sam", "files": [FILE, OTHER], "e2e": "BLOB-sam-plan"}
            )
            assert _next_event(sam, "plan_conflicts")["conflicts"] == []
            assert _next_event(priya, "plan_update")["e2e"] == "BLOB-sam-plan"
            priya.send_json(
                {"type": "plan", "user_id": "priya", "files": [FILE], "e2e": "BLOB-priya-plan"}
            )
            conflicts = _next_event(priya, "plan_conflicts")["conflicts"]
            by_kind = {conflict["kind"]: conflict["e2e"] for conflict in conflicts}
            assert by_kind == {"planned": "BLOB-sam-plan", "edited": "BLOB-sam-activity"}

            # a peer matched only by its plan: PeerTouch carries the plan blob
            priya.send_json({"type": "edit_check", "request_id": "r2", "file_path": OTHER})
            planned = _next_event(priya, "edit_check_response")
            assert planned["scope"] == "plan"
            assert [peer["e2e"] for peer in planned["others"]] == ["BLOB-sam-plan"]

            # intent -> intent_update
            sam.send_json(
                {"type": "intent", "user_id": "sam", "prompt": "", "e2e": "BLOB-sam-intent"}
            )
            assert _next_event(priya, "intent_update")["e2e"] == "BLOB-sam-intent"


def test_a_peer_matched_by_intent_carries_the_intent_blob() -> None:
    """Cleartext rooms only (an e2e intent has no readable prompt to match),
    but the blob still rides along with whatever matched."""
    room = Room()
    room.add_member("sam", object(), "cursor")  # type: ignore[arg-type]
    room.add_member("priya", object(), "claude-code")  # type: ignore[arg-type]
    room.record_intent("sam", "refactor room.py", 1000.0, "cursor", e2e="BLOB-intent")

    verdict = room.evaluate_edit("priya", "server/room.py", 1001.0)

    assert verdict.scope == "intent"
    assert [peer.e2e for peer in verdict.peers] == ["BLOB-intent"]


def test_catch_up_replays_the_blobs(relay: RoomManager) -> None:
    client = TestClient(app)
    room_id = client.post("/rooms").json()["room_id"]

    with client.websocket_connect(f"/ws/{room_id}") as sam:
        _join(sam, "sam")
        sam.send_json(_activity(FILE, "BLOB-a"))
        sam.send_json({"type": "intent", "user_id": "sam", "prompt": "", "e2e": "BLOB-i"})
        sam.send_json({"type": "plan", "user_id": "sam", "files": [FILE], "e2e": "BLOB-p"})
        _next_event(sam, "plan_conflicts")
        _next_event(sam, "activity_update")

        with client.websocket_connect(f"/ws/{room_id}") as dashboard:
            dashboard.send_json({"type": "join", "name": None})
            seen = {}
            for _ in range(8):
                frame = dashboard.receive_json()
                seen[frame["type"]] = frame
                if {"activity_update", "intent_update", "plan_update"} <= set(seen):
                    break

    assert seen["activity_update"]["e2e"] == "BLOB-a"
    assert seen["intent_update"]["e2e"] == "BLOB-i"
    assert seen["plan_update"]["e2e"] == "BLOB-p"


def test_the_blob_is_copied_verbatim() -> None:
    """Never parsed, re-encoded or trimmed: base64 padding, odd characters and
    all come back byte for byte."""
    blob = "AAEC+/==\né" * 100
    room = Room()
    message = ActivityMessage(
        user_id="sam",
        timestamp=1.0,
        changes=[FileChange(file_path=FILE, diff="")],
        e2e=blob,
    )
    room.add_member("sam", object())  # type: ignore[arg-type]
    room.add_member("priya", object())  # type: ignore[arg-type]
    room.record_activity(message)

    peers = room.evaluate_edit("priya", FILE, 2.0).peers

    assert peers[0].e2e == blob


def test_cleartext_rooms_send_empty_blobs(relay: RoomManager) -> None:
    client = TestClient(app)
    room_id = client.post("/rooms").json()["room_id"]

    with client.websocket_connect(f"/ws/{room_id}") as sam:
        _join(sam, "sam")
        sam.send_json(
            {"type": "activity", "user_id": "sam", "changes": [{"file_path": "a.py", "diff": "+x"}]}
        )
        assert _next_event(sam, "activity_update")["e2e"] == ""


# --- the room's e2e flag -------------------------------------------------------------


def test_room_created_e2e_announces_it(relay: RoomManager) -> None:
    client = TestClient(app)
    body = client.post("/rooms", json={"settings": {"e2e": True}}).json()

    assert body["settings"]["e2e"] is True
    with client.websocket_connect(f"/ws/{body['room_id']}") as dashboard:
        dashboard.send_json({"type": "join", "name": None})
        settings = dashboard.receive_json()

    assert settings["type"] == "room_settings"
    assert settings["e2e"] is True


def test_a_member_turns_e2e_on_and_it_is_broadcast(relay: RoomManager) -> None:
    client = TestClient(app)
    room_id = client.post("/rooms").json()["room_id"]

    with client.websocket_connect(f"/ws/{room_id}") as dashboard:
        dashboard.send_json({"type": "join", "name": None})
        assert dashboard.receive_json()["e2e"] is False
        with client.websocket_connect(f"/ws/{room_id}") as sam:
            _join(
                sam,
                "sam",
                settings={"block_window_seconds": 20, "warn_window_seconds": 60, "e2e": True},
            )
            event = _next_event(dashboard, "room_settings")

    assert (event["e2e"], event["set_by"]) == (True, "sam")
    room = relay.get(room_id)
    assert room is not None and room.settings.e2e


@pytest.mark.parametrize(
    "settings",
    [
        # A daemon that predates the flag: windows only.
        {"block_window_seconds": 5, "warn_window_seconds": 30},
        # A stale or hostile client saying "cleartext".
        {"block_window_seconds": 5, "warn_window_seconds": 30, "e2e": False},
    ],
)
def test_a_join_never_clears_e2e(relay: RoomManager, settings: dict) -> None:
    client = TestClient(app)
    room_id = client.post("/rooms", json={"settings": {"e2e": True}}).json()["room_id"]

    with client.websocket_connect(f"/ws/{room_id}") as sam:
        _join(sam, "sam", settings=settings)
        event = _next_event(sam, "room_settings")

    # The windows change; e2e stays on.
    assert (event["block_window_seconds"], event["e2e"]) == (5, True)
    room = relay.get(room_id)
    assert room is not None
    assert room.settings == RoomSettings(
        block_window_seconds=5, warn_window_seconds=30, e2e=True
    )


def test_e2e_flag_survives_a_restart() -> None:
    store = MemoryRoomStore()
    first = RoomManager(store=store)
    room_id = first.create_room(RoomSettings(e2e=True))

    first._rooms.clear()  # the process restarted; the store did not
    revived = first.get(room_id)

    assert revived is not None and revived.settings.e2e


# --- nothing to third parties ---------------------------------------------------------


def test_e2e_rooms_never_call_gemini(
    relay: RoomManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def forbidden(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("an e2e room called Gemini")

    monkeypatch.setattr(main, "generate_caption", forbidden)
    monkeypatch.setattr(main, "classify_conflict", forbidden)
    client = TestClient(app)
    room_id = client.post("/rooms", json={"settings": {"e2e": True}}).json()["room_id"]

    with client.websocket_connect(f"/ws/{room_id}") as sam:
        _join(sam, "sam")
        with client.websocket_connect(f"/ws/{room_id}") as priya:
            _join(priya, "priya")
            sam.send_json(_activity(FILE, "BLOB-1"))
            update = _next_event(priya, "activity_update")
            priya.send_json(_activity(FILE, "BLOB-2"))
            alert = _next_event(sam, "conflict_alert")

    assert update["caption"] == f"sam's agent is editing {FILE}"
    assert alert["verdict"] == "related"
    assert set(alert["users"]) == {"sam", "priya"}
