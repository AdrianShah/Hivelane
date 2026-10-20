"""Opt-in room history (server/history.py): a bounded in-memory ring buffer of
the feed events spectators were sent, served on history_request as
history_chunk frames, switched by RoomSettings.history_hours."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, Optional

import pytest
from fastapi.testclient import TestClient

try:
    from server import history, main
    from server.history import RequestThrottle, RoomHistory
    from server.main import app
    from server.models import PROTOCOL_VERSION, RoomSettings
    from server.room import Room
    from server.room_manager import RoomManager
    from server.store import MemoryRoomStore
except ImportError:  # Render rootDir=server
    import history  # type: ignore[no-redef]
    import main  # type: ignore[no-redef]
    from history import RequestThrottle, RoomHistory  # type: ignore[no-redef]
    from main import app  # type: ignore[no-redef]
    from models import PROTOCOL_VERSION, RoomSettings  # type: ignore[no-redef]
    from room import Room  # type: ignore[no-redef]
    from room_manager import RoomManager  # type: ignore[no-redef]
    from store import MemoryRoomStore  # type: ignore[no-redef]

MAX_FRAMES = 30
HASHED = "h:0a1b2c3d4e5f60718293a4b5c6d7e8f9"


class Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _event(n: int, ts: Optional[float] = None, pad: int = 0) -> dict[str, Any]:
    event: dict[str, Any] = {"type": "intent_update", "user_id": "sam", "prompt": "x" * pad, "n": n}
    if ts is not None:
        event["timestamp"] = ts
    return event


@pytest.fixture(autouse=True)
def _fresh_throttle() -> Iterator[None]:
    history.reset_throttles()
    yield
    history.reset_throttles()


@pytest.fixture
def relay(monkeypatch: pytest.MonkeyPatch) -> Iterator[RoomManager]:
    manager = RoomManager(store=MemoryRoomStore())
    monkeypatch.setattr(main, "room_manager", manager)
    yield manager


# --- the buffer ---------------------------------------------------------------------


def test_history_off_keeps_nothing() -> None:
    kept = RoomHistory(0)
    kept.record([_event(1)])
    assert not kept.enabled
    assert kept.event_count == 0
    assert kept.since() == []
    assert kept.chunks() == [[]]


def test_hours_are_clamped_to_the_wire_bounds() -> None:
    assert RoomHistory(30).hours == history.MAX_HISTORY_HOURS
    assert RoomHistory(-1).hours == 0
    assert RoomHistory(float("nan")).hours == 0
    assert RoomHistory(True).hours == 0  # a bool is not a number of hours


def test_events_come_back_in_order_and_after_since() -> None:
    clock = Clock()
    kept = RoomHistory(1, clock=clock)
    kept.record([_event(1, ts=1.0)], now=clock.now)
    clock.now += 5
    kept.record([_event(2, ts=2.0), _event(3, ts=3.0)], now=clock.now)

    assert [e["n"] for e in kept.since()] == [1, 2, 3]
    # since is the relay's broadcast clock, not the events' own timestamps.
    assert [e["n"] for e in kept.since(clock.now - 1)] == [2, 3]
    assert kept.since(clock.now) == []


def test_count_cap_drops_the_oldest() -> None:
    kept = RoomHistory(1, max_events=3)
    kept.record([_event(n) for n in range(5)])
    assert kept.event_count == 3
    assert [e["n"] for e in kept.since()] == [2, 3, 4]


def test_byte_cap_drops_the_oldest() -> None:
    one = history.event_size(_event(0, pad=1000))
    kept = RoomHistory(1, max_bytes=one * 2 + one // 2)
    kept.record([_event(n, pad=1000) for n in range(4)])
    assert kept.event_count == 2
    assert kept.byte_count <= kept.max_bytes
    assert [e["n"] for e in kept.since()] == [2, 3]


def test_an_event_bigger_than_the_cap_is_skipped_not_flushing_the_rest() -> None:
    kept = RoomHistory(1, max_bytes=2000)
    kept.record([_event(1)])
    kept.record([_event(2, pad=5000)])
    assert [e["n"] for e in kept.since()] == [1]


def test_age_cap_follows_history_hours() -> None:
    clock = Clock()
    kept = RoomHistory(1, clock=clock)
    kept.record([_event(1)], now=clock.now)
    clock.now += 1800
    kept.record([_event(2)], now=clock.now)
    clock.now += 1801  # event 1 is now just over an hour old
    assert [e["n"] for e in kept.since()] == [2]
    assert kept.event_count == 1


def test_narrowing_the_window_prunes_and_zero_clears() -> None:
    clock = Clock()
    kept = RoomHistory(4, clock=clock)
    kept.record([_event(1)], now=clock.now)
    clock.now += 2 * 3600
    kept.record([_event(2)], now=clock.now)

    kept.configure(1)
    assert [e["n"] for e in kept.since()] == [2]
    kept.configure(0)
    assert kept.event_count == 0 and kept.byte_count == 0
    kept.record([_event(3)], now=clock.now)
    assert kept.event_count == 0  # off means off


def test_process_wide_cap_makes_the_busy_room_give_up_its_own_oldest() -> None:
    size = history.event_size(_event(0, pad=1000))
    quiet = RoomHistory(1)
    quiet.record([_event(100, pad=1000)])
    busy = RoomHistory(1, total_max_bytes=history.total_bytes() + size * 2)
    busy.record([_event(n, pad=1000) for n in range(5)])
    assert quiet.event_count == 1  # untouched
    assert busy.byte_count <= size * 2


def test_chunks_are_at_most_200_events_and_cover_everything() -> None:
    kept = RoomHistory(1)
    kept.record([_event(n) for n in range(450)])
    chunks = kept.chunks()
    assert [len(chunk) for chunk in chunks] == [200, 200, 50]
    assert [e["n"] for chunk in chunks for e in chunk] == list(range(450))


def test_timestampless_events_get_the_broadcast_time_without_changing_whats_kept() -> None:
    clock = Clock(1234.5)
    kept = RoomHistory(1, clock=clock)
    joined = {"type": "user_joined", "user_id": "sam", "agent": "cursor"}
    kept.record([joined, _event(1, ts=5.0)])
    out = kept.since()
    assert out[0] == {**joined, "timestamp": 1234.5}
    assert out[1]["timestamp"] == 5.0  # an event's own timestamp wins
    assert "timestamp" not in joined


def test_totals_feed_the_metrics_gauges() -> None:
    before_events, before_bytes = history.total_events(), history.total_bytes()
    kept = RoomHistory(1)
    kept.record([_event(1), _event(2)])
    assert history.total_events() == before_events + 2
    assert history.total_bytes() == before_bytes + kept.byte_count
    kept.clear()
    assert history.total_events() == before_events


def test_request_throttle_allows_one_per_interval_per_socket() -> None:
    clock = Clock()
    throttle = RequestThrottle(10.0, clock=clock)
    a, b = Room(), Room()  # any weak-referenceable object stands in for a socket
    assert throttle.allow(a)
    assert not throttle.allow(a)
    assert throttle.allow(b)
    clock.now += 10
    assert throttle.allow(a)


# --- the room keeps what the feed carried ----------------------------------------------


def test_room_settings_switch_history_and_announce_it() -> None:
    room = Room(RoomSettings(history_hours=2))
    assert room.history.hours == 2
    assert room.settings_update().history_hours == 2

    room.history.record([_event(1)])
    update = room.apply_settings(RoomSettings(history_hours=0), set_by="sam")
    assert update is not None and update.history_hours == 0
    assert room.history.event_count == 0


def test_eviction_takes_the_history_with_it(relay: RoomManager) -> None:
    room_id = relay.create_room(RoomSettings(history_hours=1))
    room = relay.get(room_id)
    assert room is not None
    room.history.record([_event(1)])
    relay.cleanup_if_empty(room_id)
    assert room.history.event_count == 0
    assert relay.get(room_id) is None


# --- over the wire ------------------------------------------------------------------


def _member(socket: Any, name: str, settings: Optional[dict] = None) -> None:
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
    socket.send_json({"type": "join", "name": None})


def _next(socket: Any, wanted: str) -> dict:
    """The next frame (or feed event) of type `wanted`."""
    for _ in range(MAX_FRAMES):
        frame = socket.receive_json()
        if frame.get("type") == wanted:
            return frame
        if frame.get("type") == "synergo_feed":
            for event in frame["events"]:
                if event.get("type") == wanted:
                    return event
    raise AssertionError(f"never received {wanted}")


def _next_feed_with(socket: Any, wanted: str) -> dict:
    for _ in range(MAX_FRAMES):
        frame = socket.receive_json()
        if frame.get("type") == "synergo_feed" and any(
            event.get("type") == wanted for event in frame["events"]
        ):
            return frame
    raise AssertionError(f"never received a feed with {wanted}")


def _history(socket: Any, since: float = 0) -> list[dict]:
    """Request history and collect every chunk until done."""
    socket.send_json({"type": "history_request", "since": since})
    events: list[dict] = []
    for _ in range(MAX_FRAMES):
        frame = socket.receive_json()
        if frame.get("type") != "history_chunk":
            continue
        assert len(frame["events"]) <= history.HISTORY_CHUNK_EVENTS
        events.extend(frame["events"])
        if frame["done"]:
            return events
    raise AssertionError("history never finished")


def _activity(file_path: str = "src/app.py", blob: str = "") -> dict:
    return {
        "type": "activity",
        "user_id": "spoofed",
        "prompt": "refactor the app",
        "changes": [{"file_path": file_path, "diff": "@@ -1 +1 @@\n-a\n+b"}],
        "e2e": blob,
    }


def test_a_spectator_replays_exactly_what_the_feed_carried(relay: RoomManager) -> None:
    client = TestClient(app)
    room_id = client.post("/rooms", json={"settings": {"history_hours": 1}}).json()["room_id"]

    with client.websocket_connect(f"/ws/{room_id}") as viewer:
        _spectate(viewer)
        assert _next(viewer, "room_settings")["history_hours"] == 1
        with client.websocket_connect(f"/ws/{room_id}") as sam:
            _member(sam, "sam")
            sam.send_json(_activity())
            feed = _next_feed_with(viewer, "activity_update")

            replay = _history(viewer)
            live = [e for e in feed["events"]]
            kept = [e for e in replay if e["type"] == "activity_update"]
            assert kept == [e for e in live if e["type"] == "activity_update"]
            joined = [e for e in replay if e["type"] == "user_joined"]
            assert joined and joined[0]["user_id"] == "sam"
            assert isinstance(joined[0]["timestamp"], float)


def test_history_goes_to_the_requester_only_and_members_may_ask(relay: RoomManager) -> None:
    client = TestClient(app)
    room_id = client.post("/rooms", json={"settings": {"history_hours": 1}}).json()["room_id"]

    with client.websocket_connect(f"/ws/{room_id}") as viewer:
        _spectate(viewer)
        with client.websocket_connect(f"/ws/{room_id}") as sam:
            _member(sam, "sam")
            sam.send_json(_activity())
            _next_feed_with(viewer, "activity_update")
            _next_feed_with(sam, "activity_update")

            events = _history(sam)  # the member loop answers too
            assert any(e["type"] == "activity_update" for e in events)

            # The viewer got no history_chunk: its next frame is the next feed.
            sam.send_json({"type": "intent", "user_id": "sam", "prompt": "next step"})
            frame = viewer.receive_json()
            assert frame["type"] == "synergo_feed"


def test_a_room_without_history_answers_one_empty_done_chunk(relay: RoomManager) -> None:
    client = TestClient(app)
    room_id = client.post("/rooms").json()["room_id"]
    with client.websocket_connect(f"/ws/{room_id}") as viewer:
        _spectate(viewer)
        assert _next(viewer, "room_settings")["history_hours"] == 0
        viewer.send_json({"type": "history_request"})
        chunk = _next(viewer, "history_chunk")
        assert chunk == {"type": "history_chunk", "events": [], "done": True}


def test_history_requests_are_rate_limited_per_socket(relay: RoomManager) -> None:
    client = TestClient(app)
    room_id = client.post("/rooms", json={"settings": {"history_hours": 1}}).json()["room_id"]
    with client.websocket_connect(f"/ws/{room_id}") as viewer:
        _spectate(viewer)
        with client.websocket_connect(f"/ws/{room_id}") as sam:
            _member(sam, "sam")
            _history(viewer)
            # Inside the 10 s budget: ignored, so the next frame is the feed.
            viewer.send_json({"type": "history_request"})
            sam.send_json(_activity())
            for _ in range(MAX_FRAMES):
                frame = viewer.receive_json()
                assert frame["type"] != "history_chunk"
                if frame["type"] == "synergo_feed" and any(
                    e["type"] == "activity_update" for e in frame["events"]
                ):
                    break


def test_long_history_streams_in_chunks_with_done_on_the_last(relay: RoomManager) -> None:
    client = TestClient(app)
    room_id = client.post("/rooms", json={"settings": {"history_hours": 1}}).json()["room_id"]
    room = relay.get(room_id)
    assert room is not None
    room.history.record([_event(n, ts=float(n)) for n in range(450)])
    with client.websocket_connect(f"/ws/{room_id}") as viewer:
        _spectate(viewer)
        viewer.send_json({"type": "history_request", "since": 0})
        chunks = []
        while not chunks or not chunks[-1]["done"]:
            frame = viewer.receive_json()
            if frame["type"] == "history_chunk":
                chunks.append(frame)
        assert [len(c["events"]) for c in chunks] == [200, 200, 50]
        assert [c["done"] for c in chunks] == [False, False, True]


def test_malformed_history_request_is_dropped_without_ending_the_session(
    relay: RoomManager,
) -> None:
    client = TestClient(app)
    room_id = client.post("/rooms").json()["room_id"]
    with client.websocket_connect(f"/ws/{room_id}") as viewer:
        _spectate(viewer)
        viewer.send_json({"type": "history_request", "since": "yesterday"})
        viewer.send_json({"type": "history_request"})
        assert _next(viewer, "history_chunk")["done"] is True


def test_an_e2e_room_keeps_only_hashes_and_ciphertext(relay: RoomManager) -> None:
    client = TestClient(app)
    room_id = client.post(
        "/rooms", json={"settings": {"e2e": True, "history_hours": 1}}
    ).json()["room_id"]
    with client.websocket_connect(f"/ws/{room_id}") as viewer:
        _spectate(viewer)
        with client.websocket_connect(f"/ws/{room_id}") as sam:
            _member(sam, "sam", {"e2e": True, "history_hours": 1})
            sam.send_json(
                {
                    "type": "activity",
                    "user_id": "sam",
                    "prompt": "",
                    "changes": [{"file_path": HASHED, "diff": ""}],
                    "e2e": "BLOB-ciphertext",
                }
            )
            live = _next(viewer, "activity_update")
            replayed = [e for e in _history(viewer) if e["type"] == "activity_update"]
            assert replayed == [live]
            assert replayed[0]["changes"][0]["file_path"] == HASHED
            assert replayed[0]["e2e"] == "BLOB-ciphertext"
            assert replayed[0]["prompt"] in ("", None)


# --- settings round trip ------------------------------------------------------------


def test_history_hours_round_trip_through_join_store_and_revival(relay: RoomManager) -> None:
    client = TestClient(app)
    room_id = client.post("/rooms").json()["room_id"]
    with client.websocket_connect(f"/ws/{room_id}") as viewer:
        _spectate(viewer)
        with client.websocket_connect(f"/ws/{room_id}") as sam:
            _member(sam, "sam", {"block_window_seconds": 20, "warn_window_seconds": 60, "history_hours": 4})
            assert _next(viewer, "room_settings")["history_hours"] == 0  # catch-up
            update = _next(viewer, "room_settings")
            assert update["history_hours"] == 4 and update["set_by"] == "sam"

    stored = relay._store.get_settings(room_id)
    assert stored is not None and stored["history_hours"] == 4
    revived = RoomManager(store=relay._store).get(room_id)
    assert revived is not None
    assert revived.settings.history_hours == 4
    assert revived.history.enabled  # empty after a restart, but on


def test_a_join_that_does_not_mention_history_keeps_the_rooms(relay: RoomManager) -> None:
    client = TestClient(app)
    room_id = client.post("/rooms", json={"settings": {"history_hours": 3}}).json()["room_id"]
    room = relay.get(room_id)
    assert room is not None
    room.history.record([_event(1)])
    with client.websocket_connect(f"/ws/{room_id}") as old:
        # A daemon that predates history_hours sends only the windows.
        _member(old, "old", {"block_window_seconds": 10, "warn_window_seconds": 60})
        with client.websocket_connect(f"/ws/{room_id}") as viewer:
            _spectate(viewer)
            settings = _next(viewer, "room_settings")
            assert settings["block_window_seconds"] == 10
            assert settings["history_hours"] == 3
    assert room.history.event_count >= 1


def test_metrics_report_history_totals_without_room_labels(relay: RoomManager) -> None:
    client = TestClient(app)
    text = client.get("/metrics").text
    assert "# TYPE synergo_history_events gauge" in text
    assert "# TYPE synergo_history_bytes gauge" in text
    assert "synergo_history_events{" not in text
