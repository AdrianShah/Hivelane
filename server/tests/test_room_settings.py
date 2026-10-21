"""Per-room conflict windows: chosen at room creation or by a member's committed
.synergo.toml, enforced by check_edit, announced as room_settings, and kept
across a relay restart."""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Optional

import pytest
from fastapi.testclient import TestClient

try:
    from server import limits, main
    from server.main import app
    from server.models import (
        PROTOCOL_VERSION,
        ActivityMessage,
        FileChange,
        RoomSettings,
    )
    from server.room import ACTIVITY_PRUNE_INTERVAL_SECONDS, DEFAULT_SETTINGS, Room
    from server.room_manager import RoomManager
    from server.store import MemoryRoomStore, SqliteRoomStore
except ImportError:  # Render rootDir=server
    import limits  # type: ignore[no-redef]
    import main  # type: ignore[no-redef]
    from main import app
    from models import PROTOCOL_VERSION, ActivityMessage, FileChange, RoomSettings
    from room import ACTIVITY_PRUNE_INTERVAL_SECONDS, DEFAULT_SETTINGS, Room
    from room_manager import RoomManager
    from store import MemoryRoomStore, SqliteRoomStore

FILE = "server/room.py"

# Frames to read before giving up: enough to step past queued feed batches.
MAX_FRAMES = 12


class FakeSocket:
    """Stand-in member socket; these Room-level tests never broadcast."""


def _room(block: float, warn: float) -> Room:
    room = Room(RoomSettings(block_window_seconds=block, warn_window_seconds=warn))
    room.add_member("sam", FakeSocket(), "cursor")
    room.add_member("priya", FakeSocket(), "claude-code")
    return room


def _touch(room: Room, user_id: str, timestamp: float, file_path: str = FILE) -> None:
    room.record_activity(_activity(user_id, timestamp, file_path))


def _activity(user_id: str, timestamp: float, file_path: str = FILE) -> ActivityMessage:
    return ActivityMessage(
        user_id=user_id,
        timestamp=timestamp,
        changes=[FileChange(file_path=file_path, diff="@@ -1 +1 @@")],
    )


# --- check_edit and overlaps follow the room's windows --------------------------


def test_default_room_uses_the_documented_windows() -> None:
    assert Room().settings == DEFAULT_SETTINGS
    assert DEFAULT_SETTINGS == RoomSettings(
        block_window_seconds=20.0, warn_window_seconds=60.0
    )


def test_a_narrow_block_window_turns_a_block_into_a_warning() -> None:
    room = _room(block=5.0, warn=60.0)
    _touch(room, "sam", timestamp=1000.0)

    # 10 s old: a block under the default 20 s window, only a warning here.
    decision, peers = room.check_edit("priya", FILE, now=1010.0)

    assert decision == "warn"
    assert [peer.user_id for peer in peers] == ["sam"]
    assert room.check_edit("priya", FILE, now=1004.0)[0] == "block"


def test_a_wide_warn_window_still_warns_past_the_default() -> None:
    room = _room(block=20.0, warn=120.0)
    _touch(room, "sam", timestamp=1000.0)

    assert room.check_edit("priya", FILE, now=1090.0)[0] == "warn"
    assert room.check_edit("priya", FILE, now=1121.0)[0] == "allow"


def test_a_narrow_warn_window_allows_inside_the_default() -> None:
    room = _room(block=10.0, warn=30.0)
    _touch(room, "sam", timestamp=1000.0)

    assert room.check_edit("priya", FILE, now=1045.0) == ("allow", [])


def test_a_zero_block_window_never_blocks() -> None:
    room = _room(block=0.0, warn=60.0)
    _touch(room, "sam", timestamp=1000.0)

    assert room.check_edit("priya", FILE, now=1000.0)[0] == "warn"


def test_overlap_detection_uses_the_warn_window() -> None:
    narrow = _room(block=10.0, warn=30.0)
    default = _room(block=20.0, warn=60.0)
    for room in (narrow, default):
        _touch(room, "sam", timestamp=1000.0)

    incoming = _activity("priya", timestamp=1045.0)

    assert narrow.find_overlaps(incoming) == []
    assert [o.other.user_id for o in default.find_overlaps(incoming)] == ["sam"]


def test_pruning_retains_touches_for_a_wide_warn_window() -> None:
    room = _room(block=20.0, warn=300.0)
    _touch(room, "sam", timestamp=1000.0)

    # Forces a sweep 200 s later: past the default retention, inside this room's.
    sweep_at = 1000.0 + ACTIVITY_PRUNE_INTERVAL_SECONDS + 140.0
    _touch(room, "priya", timestamp=sweep_at, file_path="other.py")

    assert room.check_edit("priya", FILE, now=1200.0)[0] == "warn"


def test_pruning_never_retains_less_than_the_default() -> None:
    """Narrowing and re-widening the window must not lose touches in between."""
    room = _room(block=5.0, warn=10.0)
    _touch(room, "priya", timestamp=950.0, file_path="other.py")  # first sweep
    _touch(room, "sam", timestamp=1000.0)
    # The next sweep runs when sam's touch is 15 s old: past this room's 10 s
    # warn window, inside the default retention.
    sweep_at = 950.0 + ACTIVITY_PRUNE_INTERVAL_SECONDS + 5.0
    _touch(room, "priya", timestamp=sweep_at, file_path="other.py")
    room.apply_settings(DEFAULT_SETTINGS)

    assert room.check_edit("priya", FILE, now=sweep_at)[0] == "block"


# --- apply_settings -----------------------------------------------------------


def test_apply_settings_returns_the_frame_to_broadcast() -> None:
    room = Room()
    custom = RoomSettings(block_window_seconds=5.0, warn_window_seconds=30.0)

    update = room.apply_settings(custom, set_by="sam")

    assert update is not None
    assert update.type == "room_settings"
    assert (update.block_window_seconds, update.warn_window_seconds) == (5.0, 30.0)
    assert update.set_by == "sam"
    assert room.settings == custom
    assert room.settings_update().model_dump() == update.model_dump()


def test_unchanged_settings_are_not_rebroadcast() -> None:
    room = Room()

    assert room.apply_settings(DEFAULT_SETTINGS.model_copy(), set_by="sam") is None
    assert room.settings_update().set_by is None


def test_warn_window_shorter_than_block_window_is_ignored(
    caplog: pytest.LogCaptureFixture,
) -> None:
    room = Room()

    update = room.apply_settings(
        RoomSettings(block_window_seconds=50.0, warn_window_seconds=10.0), set_by="sam"
    )

    assert update is None
    assert room.settings == DEFAULT_SETTINGS
    assert "Ignoring room settings from sam" in caplog.text


# --- over the wire ------------------------------------------------------------


@pytest.fixture
def relay(monkeypatch: pytest.MonkeyPatch) -> Iterator[RoomManager]:
    """A fresh room registry for the app, so these tests neither see nor count
    against rooms other tests created."""
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


def _spectate(socket: Any) -> None:
    socket.send_json({"type": "join", "name": None, "role": "spectator"})


def _next_frame(socket: Any, type_name: str) -> dict:
    for _ in range(MAX_FRAMES):
        frame = socket.receive_json()
        if frame.get("type") == type_name:
            return frame
    raise AssertionError(f"never received a top-level {type_name} frame")


def _next_feed_event(socket: Any, type_name: str) -> dict:
    for _ in range(MAX_FRAMES):
        frame = socket.receive_json()
        if frame.get("type") != "synergo_feed":
            continue
        for event in frame["events"]:
            if event.get("type") == type_name:
                return event
    raise AssertionError(f"never received a {type_name} feed event")


def _admitted(socket: Any) -> bool:
    """True once the relay answers an edit_check, which it only does for members."""
    socket.send_json({"type": "edit_check", "request_id": "probe", "file_path": "x.py"})
    return _next_frame(socket, "edit_check_response")["request_id"] == "probe"


def test_post_rooms_without_a_body_uses_the_defaults(relay: RoomManager) -> None:
    client = TestClient(app)

    response = client.post("/rooms")

    assert response.status_code == 200
    body = response.json()
    assert len(body["room_id"]) == 8
    assert body["settings"] == DEFAULT_SETTINGS.model_dump()
    room = relay.get(body["room_id"])
    assert room is not None
    assert room.settings == DEFAULT_SETTINGS


def test_post_rooms_with_an_empty_object_uses_the_defaults(relay: RoomManager) -> None:
    response = TestClient(app).post("/rooms", json={})

    assert response.status_code == 200
    assert response.json()["settings"] == DEFAULT_SETTINGS.model_dump()


def test_post_rooms_with_settings_starts_the_room_on_them(relay: RoomManager) -> None:
    client = TestClient(app)
    custom = {"block_window_seconds": 0.0, "warn_window_seconds": 90.0}

    response = client.post("/rooms", json={"settings": custom})

    assert response.status_code == 200
    room_id = response.json()["room_id"]
    # Echoed in full: the windows asked for, plus e2e at its default.
    assert response.json()["settings"] == {**custom, "e2e": False}
    with client.websocket_connect(f"/ws/{room_id}") as spectator:
        _spectate(spectator)
        caught_up = spectator.receive_json()

    assert caught_up["type"] == "room_settings"
    assert caught_up["block_window_seconds"] == 0.0
    assert caught_up["warn_window_seconds"] == 90.0
    assert caught_up["set_by"] is None


@pytest.mark.parametrize(
    "settings",
    [
        {"block_window_seconds": 50.0, "warn_window_seconds": 10.0},
        {"block_window_seconds": -1.0, "warn_window_seconds": 60.0},
        {"block_window_seconds": 20.0, "warn_window_seconds": 99999.0},
    ],
)
def test_post_rooms_refuses_unenforceable_settings(
    relay: RoomManager, settings: dict
) -> None:
    response = TestClient(app).post("/rooms", json={"settings": settings})

    assert response.status_code == 422
    assert relay.durable_room_count == 0


def test_spectator_catch_up_starts_with_the_current_settings(relay: RoomManager) -> None:
    client = TestClient(app)
    room_id = client.post("/rooms").json()["room_id"]

    with client.websocket_connect(f"/ws/{room_id}") as spectator:
        _spectate(spectator)
        frame = spectator.receive_json()

    assert frame["type"] == "room_settings"
    assert frame["block_window_seconds"] == 20.0
    assert frame["warn_window_seconds"] == 60.0
    assert frame["set_by"] is None
    assert frame["timestamp"] > 0


def test_join_with_settings_updates_the_room_and_broadcasts_them(
    relay: RoomManager,
) -> None:
    client = TestClient(app)
    room_id = client.post("/rooms").json()["room_id"]
    custom = {"block_window_seconds": 5.0, "warn_window_seconds": 30.0}

    with client.websocket_connect(f"/ws/{room_id}") as spectator:
        _spectate(spectator)
        assert _next_frame(spectator, "room_settings")["set_by"] is None
        with client.websocket_connect(f"/ws/{room_id}") as sam:
            _join(sam, "sam", settings=custom)
            seen_by_spectator = _next_feed_event(spectator, "room_settings")
            seen_by_member = _next_feed_event(sam, "room_settings")

    for event in (seen_by_spectator, seen_by_member):
        assert event["block_window_seconds"] == 5.0
        assert event["warn_window_seconds"] == 30.0
        assert event["set_by"] == "sam"
    room = relay.get(room_id)
    assert room is not None
    assert room.settings == RoomSettings(**custom)


def test_member_settings_are_persisted_for_a_revived_room(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_path = str(tmp_path / "synergo.db")
    monkeypatch.setattr(
        main, "room_manager", RoomManager(store=SqliteRoomStore(state_path))
    )
    client = TestClient(app)
    room_id = client.post("/rooms").json()["room_id"]
    custom = {"block_window_seconds": 5.0, "warn_window_seconds": 30.0}

    with client.websocket_connect(f"/ws/{room_id}") as sam:
        _join(sam, "sam", settings=custom)
        _next_feed_event(sam, "room_settings")

    revived = RoomManager(store=SqliteRoomStore(state_path)).get(room_id)
    assert revived is not None
    assert revived.settings == RoomSettings(**custom)


@pytest.mark.parametrize(
    "settings",
    [
        # Cross-field: the model accepts it, the room refuses it.
        {"block_window_seconds": 50.0, "warn_window_seconds": 10.0},
        # Out of range: the model itself refuses it.
        {"block_window_seconds": -1.0, "warn_window_seconds": 99999.0},
        {"block_window_seconds": "soon"},
    ],
)
def test_invalid_join_settings_are_ignored_and_the_member_still_joins(
    relay: RoomManager, settings: dict, caplog: pytest.LogCaptureFixture
) -> None:
    client = TestClient(app)
    room_id = client.post("/rooms").json()["room_id"]

    with client.websocket_connect(f"/ws/{room_id}") as sam:
        _join(sam, "sam", settings=settings)
        assert _admitted(sam)

    room = relay.get(room_id)
    assert room is not None
    assert room.settings == DEFAULT_SETTINGS
    assert "Ignoring" in caplog.text


def test_edit_check_over_the_wire_honours_the_room_windows(relay: RoomManager) -> None:
    client = TestClient(app)
    room_id = client.post(
        "/rooms",
        json={"settings": {"block_window_seconds": 0.0, "warn_window_seconds": 60.0}},
    ).json()["room_id"]

    with client.websocket_connect(f"/ws/{room_id}") as sam:
        _join(sam, "sam")
        with client.websocket_connect(f"/ws/{room_id}") as priya:
            _join(priya, "priya")
            sam.send_json(
                {
                    "type": "activity",
                    "user_id": "sam",
                    "changes": [{"file_path": FILE, "diff": "@@ -1 +1 @@"}],
                    "agent": "cursor",
                }
            )
            _next_feed_event(sam, "activity_update")
            priya.send_json(
                {"type": "edit_check", "request_id": "req-1", "file_path": FILE}
            )
            response = _next_frame(priya, "edit_check_response")

    # A fresh peer edit blocks under the defaults; this room never blocks.
    assert response["decision"] == "warn"
    assert [peer["user_id"] for peer in response["others"]] == ["sam"]


def test_legacy_six_character_code_joins_over_the_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryRoomStore()
    store.add("K7M2PQ", time.time())
    monkeypatch.setattr(main, "room_manager", RoomManager(store=store))
    client = TestClient(app)

    with client.websocket_connect("/ws/k7m2pq") as sam:
        _join(sam, "sam")
        assert _admitted(sam)


def test_readyz_reports_the_rate_limit_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    client = TestClient(app)

    assert client.get("/readyz").json()["rate_limit_backend"] == limits.BACKEND
    assert limits.BACKEND in ("memory", "redis")

    monkeypatch.setattr(limits, "BACKEND", "redis")
    assert client.get("/readyz").json()["rate_limit_backend"] == "redis"
