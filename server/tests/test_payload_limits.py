"""What one client can make the relay hold and fan out: per-field caps
(truncated, not dropped), a per-frame cap (socket closed), the e2e blob cap
(message dropped), and a per-room message quota (excess dropped and counted)."""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

try:
    from server import config, main, metrics
    from server.limits import TokenBucket
    from server.main import WS_CLOSE_FRAME_TOO_LARGE, app
    from server.models import E2E_MAX_CHARS, PROTOCOL_VERSION
    from server.room import Room
    from server.room_manager import RoomManager
    from server.sanitize import (
        MAX_DIFF_BYTES,
        MAX_FILES,
        MAX_FRAME_BYTES,
        MAX_SYMBOLS,
        MAX_TEXT_BYTES,
        TRUNCATION_MARKER,
        truncate_utf8,
    )
    from server.store import MemoryRoomStore
except ImportError:  # Render rootDir=server
    import config  # type: ignore[no-redef]
    import main  # type: ignore[no-redef]
    import metrics  # type: ignore[no-redef]
    from limits import TokenBucket  # type: ignore[no-redef]
    from main import WS_CLOSE_FRAME_TOO_LARGE, app  # type: ignore[no-redef]
    from models import E2E_MAX_CHARS, PROTOCOL_VERSION  # type: ignore[no-redef]
    from room import Room  # type: ignore[no-redef]
    from room_manager import RoomManager  # type: ignore[no-redef]
    from sanitize import (  # type: ignore[no-redef]
        MAX_DIFF_BYTES,
        MAX_FILES,
        MAX_FRAME_BYTES,
        MAX_SYMBOLS,
        MAX_TEXT_BYTES,
        TRUNCATION_MARKER,
        truncate_utf8,
    )
    from store import MemoryRoomStore  # type: ignore[no-redef]

MAX_FRAMES = 20


@pytest.fixture
def relay(monkeypatch: pytest.MonkeyPatch) -> Iterator[RoomManager]:
    manager = RoomManager(store=MemoryRoomStore())
    monkeypatch.setattr(main, "room_manager", manager)
    yield manager


@pytest.fixture(autouse=True)
def _fresh_counters() -> Iterator[None]:
    metrics.registry.reset()
    yield
    metrics.registry.reset()


def _join(socket: Any, name: str) -> None:
    socket.send_json(
        {
            "type": "join",
            "name": name,
            "agent": "claude-code",
            "protocol_version": PROTOCOL_VERSION,
        }
    )


def _next_event(socket: Any, type_name: str) -> dict:
    for _ in range(MAX_FRAMES):
        frame = socket.receive_json()
        events = frame["events"] if frame.get("type") == "synergo_feed" else [frame]
        for event in events:
            if event.get("type") == type_name:
                return event
    raise AssertionError(f"never received {type_name}")


def _wait_for(predicate: Any, timeout: float = 3.0) -> None:
    """For effects with no reply (a dropped message): poll briefly."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition never became true")


def _utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))


# --- truncation ------------------------------------------------------------------


def test_short_text_is_untouched() -> None:
    assert truncate_utf8("hello", 10) == "hello"
    assert truncate_utf8("x" * 10, 10) == "x" * 10


def test_long_text_is_cut_to_the_limit_with_a_marker() -> None:
    cut = truncate_utf8("x" * 5000, 4096)

    assert _utf8_len(cut) <= 4096
    assert cut.endswith(TRUNCATION_MARKER)
    assert cut.startswith("x" * 4000)


def test_truncation_never_splits_a_multibyte_character() -> None:
    cut = truncate_utf8("é" * 5000 + "😀" * 10, 4097)

    assert _utf8_len(cut) <= 4097
    cut.encode("utf-8")  # still valid text
    assert cut.endswith(TRUNCATION_MARKER)
    assert set(cut.removesuffix(TRUNCATION_MARKER)) == {"é"}


def test_the_caps_are_the_documented_ones() -> None:
    assert (MAX_DIFF_BYTES, MAX_TEXT_BYTES, MAX_FILES, MAX_SYMBOLS) == (
        64 * 1024,
        4 * 1024,
        50,
        64,
    )
    assert MAX_FRAME_BYTES == 512 * 1024
    assert E2E_MAX_CHARS == 256 * 1024


# --- caps over the wire ------------------------------------------------------------


def test_activity_is_trimmed_not_dropped(relay: RoomManager) -> None:
    client = TestClient(app)
    room_id = client.post("/rooms").json()["room_id"]
    big_diff = "+" + "d" * (100 * 1024)
    changes = [
        {
            "file_path": "big.py",
            "diff": big_diff,
            "symbols": [f"fn_{i}" for i in range(100)],
            "line_ranges": [[i, i] for i in range(400)],
        }
    ] + [{"file_path": f"f{i}.py", "diff": "+x"} for i in range(70)]

    with client.websocket_connect(f"/ws/{room_id}") as sam:
        _join(sam, "sam")
        sam.send_json(
            {
                "type": "activity",
                "user_id": "sam",
                "prompt": "p" * (10 * 1024),
                "changes": changes,
            }
        )
        update = _next_event(sam, "activity_update")

    assert _utf8_len(update["prompt"]) <= MAX_TEXT_BYTES
    assert update["prompt"].endswith(TRUNCATION_MARKER)
    assert len(update["changes"]) == MAX_FILES
    first = update["changes"][0]
    assert first["file_path"] == "big.py"
    assert _utf8_len(first["diff"]) <= MAX_DIFF_BYTES
    assert first["diff"].endswith(TRUNCATION_MARKER)
    assert len(first["symbols"]) == MAX_SYMBOLS
    assert len(first["line_ranges"]) == 256
    assert update["changes"][1] == {
        "file_path": "f0.py",
        "diff": "+x",
        "symbols": [],
        "line_ranges": [],
    }


def test_intent_prompt_is_capped(relay: RoomManager) -> None:
    client = TestClient(app)
    room_id = client.post("/rooms").json()["room_id"]

    with client.websocket_connect(f"/ws/{room_id}") as sam:
        _join(sam, "sam")
        sam.send_json({"type": "intent", "user_id": "sam", "prompt": "i" * 9000})
        update = _next_event(sam, "intent_update")

    assert _utf8_len(update["prompt"]) <= MAX_TEXT_BYTES
    assert update["prompt"].endswith(TRUNCATION_MARKER)


def test_plan_is_capped(relay: RoomManager) -> None:
    client = TestClient(app)
    room_id = client.post("/rooms").json()["room_id"]

    with client.websocket_connect(f"/ws/{room_id}") as sam:
        _join(sam, "sam")
        sam.send_json(
            {
                "type": "plan",
                "user_id": "sam",
                "summary": "s" * 9000,
                "files": [f"f{i}.py" for i in range(80)],
                "symbols": [f"fn_{i}" for i in range(100)],
            }
        )
        assert _next_event(sam, "plan_conflicts")["conflicts"] == []
        update = _next_event(sam, "plan_update")

    assert _utf8_len(update["summary"]) <= MAX_TEXT_BYTES
    assert len(update["files"]) == MAX_FILES
    assert len(update["symbols"]) == MAX_SYMBOLS


def test_an_edit_check_with_too_many_symbols_is_still_answered(relay: RoomManager) -> None:
    client = TestClient(app)
    room_id = client.post("/rooms").json()["room_id"]

    with client.websocket_connect(f"/ws/{room_id}") as sam:
        _join(sam, "sam")
        sam.send_json(
            {
                "type": "edit_check",
                "request_id": "r1",
                "file_path": "a.py",
                "symbols": [f"fn_{i}" for i in range(500)],
                "line_ranges": [[i, i] for i in range(2000)],
            }
        )
        assert _next_event(sam, "edit_check_response")["decision"] == "allow"


# --- frames ------------------------------------------------------------------------


def test_an_oversized_frame_closes_the_socket(relay: RoomManager) -> None:
    client = TestClient(app)
    room_id = client.post("/rooms").json()["room_id"]

    with client.websocket_connect(f"/ws/{room_id}") as sam:
        _join(sam, "sam")
        sam.send_text('{"type": "intent", "prompt": "' + "x" * MAX_FRAME_BYTES + '"}')
        with pytest.raises(WebSocketDisconnect) as closed:
            for _ in range(MAX_FRAMES):
                sam.receive_json()

    assert closed.value.code == WS_CLOSE_FRAME_TOO_LARGE
    assert "512 KB" in (closed.value.reason or "")
    assert metrics.messages_dropped_total.value("frame_too_large") == 1
    room = relay.get(room_id)
    assert room is not None and room.member_ids == []


def test_an_oversized_join_frame_is_refused(relay: RoomManager) -> None:
    client = TestClient(app)
    room_id = client.post("/rooms").json()["room_id"]

    with client.websocket_connect(f"/ws/{room_id}") as socket:
        socket.send_bytes(b"x" * (MAX_FRAME_BYTES + 1))
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_json()

    assert closed.value.code == WS_CLOSE_FRAME_TOO_LARGE
    assert metrics.ws_rejections_total.value("frame_too_large") == 1


def test_multibyte_frames_are_measured_in_bytes(relay: RoomManager) -> None:
    """Under the cap in characters, over it in UTF-8 bytes."""
    client = TestClient(app)
    room_id = client.post("/rooms").json()["room_id"]

    with client.websocket_connect(f"/ws/{room_id}") as sam:
        _join(sam, "sam")
        sam.send_text('{"type": "x", "p": "' + "é" * (MAX_FRAME_BYTES // 2 + 10) + '"}')
        with pytest.raises(WebSocketDisconnect) as closed:
            for _ in range(MAX_FRAMES):
                sam.receive_json()

    assert closed.value.code == WS_CLOSE_FRAME_TOO_LARGE


def test_a_frame_that_is_not_json_is_dropped_not_fatal(relay: RoomManager) -> None:
    client = TestClient(app)
    room_id = client.post("/rooms").json()["room_id"]

    with client.websocket_connect(f"/ws/{room_id}") as sam:
        _join(sam, "sam")
        sam.send_text("{not json")
        sam.send_json(["a", "list"])
        sam.send_json({"type": "edit_check", "request_id": "r1", "file_path": "a.py"})
        assert _next_event(sam, "edit_check_response")["request_id"] == "r1"

    assert metrics.messages_dropped_total.value("invalid") == 2


def test_an_oversized_e2e_blob_drops_the_message(relay: RoomManager) -> None:
    client = TestClient(app)
    room_id = client.post("/rooms").json()["room_id"]

    with client.websocket_connect(f"/ws/{room_id}") as sam:
        _join(sam, "sam")
        sam.send_json(
            {
                "type": "activity",
                "user_id": "sam",
                "changes": [],
                "e2e": "A" * (E2E_MAX_CHARS + 1),
            }
        )
        _wait_for(lambda: metrics.messages_dropped_total.value("invalid") == 1)
        sam.send_json({"type": "activity", "user_id": "sam", "changes": [], "e2e": "ok"})
        update = _next_event(sam, "activity_update")

    assert update["e2e"] == "ok"


# --- room quota --------------------------------------------------------------------


def test_token_bucket_allows_a_burst_then_refills_steadily() -> None:
    now = [100.0]
    bucket = TokenBucket(rate_per_minute=3, clock=lambda: now[0])

    assert [bucket.take() for _ in range(4)] == [True, True, True, False]
    now[0] += 20.0  # 3/min = one token per 20 s
    assert bucket.take()
    assert not bucket.take()
    now[0] += 3600.0  # never more than one minute's worth banked
    assert [bucket.take() for _ in range(4)] == [True, True, True, False]


def test_rooms_get_the_configured_quota(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "ROOM_MESSAGE_LIMIT", 7)

    assert Room().message_quota.capacity == 7


def test_default_room_quota_is_600_a_minute() -> None:
    assert config.load_settings({}).room_message_limit == 600
    assert config.load_settings({"ROOM_MESSAGE_LIMIT": "50"}).room_message_limit == 50


def test_messages_over_the_room_quota_are_dropped_and_counted(relay: RoomManager) -> None:
    client = TestClient(app)
    room_id = client.post("/rooms").json()["room_id"]
    room = relay.get(room_id)
    assert room is not None
    room.message_quota = TokenBucket(rate_per_minute=2)

    with client.websocket_connect(f"/ws/{room_id}") as sam:
        _join(sam, "sam")  # the join itself is not counted
        for decision_id in ("d1", "d2", "d3", "d4"):
            sam.send_json({"type": "decision_feedback", "decision_id": decision_id, "useful": True})
        _wait_for(lambda: metrics.messages_dropped_total.value("room_quota") == 2)
        # Edit checks never draw on the room quota: still answered.
        sam.send_json({"type": "edit_check", "request_id": "r1", "file_path": "a.py"})
        assert _next_event(sam, "edit_check_response")["request_id"] == "r1"

    scrape = client.get("/metrics").text
    assert 'synergo_messages_dropped_total{reason="room_quota"} 2' in scrape


def test_the_quota_is_shared_by_everyone_in_the_room(relay: RoomManager) -> None:
    client = TestClient(app)
    room_id = client.post("/rooms").json()["room_id"]
    room = relay.get(room_id)
    assert room is not None
    room.message_quota = TokenBucket(rate_per_minute=1)

    with client.websocket_connect(f"/ws/{room_id}") as sam:
        _join(sam, "sam")
        with client.websocket_connect(f"/ws/{room_id}") as priya:
            _join(priya, "priya")
            sam.send_json({"type": "decision_feedback", "decision_id": "x", "useful": True})
            priya.send_json({"type": "decision_feedback", "decision_id": "y", "useful": True})
            _wait_for(lambda: metrics.messages_dropped_total.value("room_quota") == 1)
            # priya's edit check is unaffected by the exhausted room quota.
            priya.send_json({"type": "edit_check", "request_id": "p1", "file_path": "a.py"})
            assert _next_event(priya, "edit_check_response")["request_id"] == "p1"


def test_dropped_message_reasons_start_at_zero() -> None:
    scrape = TestClient(app).get("/metrics").text

    for reason in ("room_quota", "invalid", "frame_too_large"):
        assert f'synergo_messages_dropped_total{{reason="{reason}"}} 0' in scrape
