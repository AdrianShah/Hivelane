"""Capacity limits: every one is configurable, 0 means unlimited, and rooms and
members per room are unlimited unless a relay sets them."""

from __future__ import annotations

import contextlib
import time
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

try:
    from server import config, main, room_manager as room_manager_module
    from server import file_queue, limits, room as room_module, sanitize
    from server.models import PROTOCOL_VERSION, ActivityMessage
    from server.room import Room
    from server.room_manager import RoomManager
    from server.store import MemoryRoomStore
except ImportError:  # Render rootDir=server
    import config  # type: ignore[no-redef]
    import file_queue  # type: ignore[no-redef]
    import limits  # type: ignore[no-redef]
    import main  # type: ignore[no-redef]
    import room as room_module  # type: ignore[no-redef]
    import room_manager as room_manager_module  # type: ignore[no-redef]
    import sanitize  # type: ignore[no-redef]
    from models import PROTOCOL_VERSION, ActivityMessage  # type: ignore[no-redef]
    from room import Room  # type: ignore[no-redef]
    from room_manager import RoomManager  # type: ignore[no-redef]
    from store import MemoryRoomStore  # type: ignore[no-redef]

MAX_FRAMES = 30


@pytest.fixture
def relay(monkeypatch: pytest.MonkeyPatch) -> Iterator[RoomManager]:
    manager = RoomManager(store=MemoryRoomStore())
    monkeypatch.setattr(main, "room_manager", manager)
    yield manager


def _join(socket: Any, name: str) -> None:
    socket.send_json(
        {
            "type": "join",
            "name": name,
            "agent": "claude-code",
            "protocol_version": PROTOCOL_VERSION,
        }
    )


def _wait_for(predicate: Any, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition never became true")


def _edit_check_reply(socket: Any) -> dict:
    socket.send_json({"type": "edit_check", "request_id": "r1", "file_path": "a.py"})
    for _ in range(MAX_FRAMES):
        frame = socket.receive_json()
        if frame.get("type") == "edit_check_response":
            return frame
    raise AssertionError("never received edit_check_response")


# --- configuration -----------------------------------------------------------------


def test_rooms_and_members_are_unlimited_by_default() -> None:
    settings = config.load_settings({})

    assert settings.max_rooms == 0
    assert settings.max_members == 0
    assert room_manager_module.MAX_ROOMS == config.MAX_ROOMS
    assert room_manager_module.MAX_MEMBERS == config.MAX_MEMBERS


def test_per_message_caps_keep_their_defaults() -> None:
    settings = config.load_settings({})

    assert settings.max_frame_bytes == 512 * 1024
    assert settings.max_diff_bytes == 64 * 1024
    assert settings.max_text_bytes == 4 * 1024
    assert settings.max_files_per_message == 50
    assert settings.max_symbols_per_message == 64
    assert settings.max_line_ranges_per_message == 256
    assert settings.max_path_chars == 1024
    assert settings.max_pending_joins == 10
    assert settings.max_tracked_decisions == 1000
    assert settings.history_max_events == 5000
    assert settings.max_webhooks_per_room == 3


@pytest.mark.parametrize(("field", "name", "_default"), config.LIMITS)
def test_every_limit_reads_its_environment_variable(
    field: str, name: str, _default: int
) -> None:
    assert getattr(config.load_settings({name: "7"}), field) == 7
    assert getattr(config.load_settings({name: "0"}), field) == 0


@pytest.mark.parametrize("raw", ["-1", "lots", "1.5"])
def test_a_bad_limit_refuses_to_boot(raw: str) -> None:
    with pytest.raises(config.ConfigError, match="SYNERGO_MAX_MEMBERS"):
        config.load_settings({"SYNERGO_MAX_MEMBERS": raw})


@pytest.mark.parametrize(
    "name", ["ROOM_CREATE_LIMIT", "WS_ATTEMPT_LIMIT", "ROOM_MESSAGE_LIMIT", "RATE_LIMIT_MAX_TRACKED_IPS"]
)
def test_rate_limits_accept_zero_as_unlimited(name: str) -> None:
    settings = config.load_settings({name: "0"})

    assert config.limits_summary(settings)[name.lower()] == 0


def test_webhooks_per_room_cannot_be_unlimited() -> None:
    with pytest.raises(config.ConfigError, match="SYNERGO_MAX_WEBHOOKS_PER_ROOM"):
        config.load_settings({"SYNERGO_MAX_WEBHOOKS_PER_ROOM": "0"})
    assert config.load_settings({"SYNERGO_MAX_WEBHOOKS_PER_ROOM": "10"}).max_webhooks_per_room == 10


def test_readyz_reports_the_limits_in_force(relay: RoomManager) -> None:
    body = TestClient(main.app).get("/readyz").json()

    assert body["limits"]["max_rooms"] == config.MAX_ROOMS
    assert body["limits"]["max_members"] == config.MAX_MEMBERS
    assert body["limits"]["max_frame_bytes"] == config.MAX_FRAME_BYTES


# --- members per room ----------------------------------------------------------------


def test_a_seventh_member_joins_when_members_are_unlimited(
    relay: RoomManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(room_manager_module, "MAX_MEMBERS", 0)
    room_id = relay.create_room()
    client = TestClient(main.app)
    with contextlib.ExitStack() as stack:
        sockets = []
        for index in range(7):
            socket = stack.enter_context(client.websocket_connect(f"/ws/{room_id}"))
            _join(socket, f"member{index}")
            sockets.append(socket)
            _wait_for(lambda: relay.get(room_id).member_count == index + 1)

        assert _edit_check_reply(sockets[-1])["decision"] == "allow"


def test_a_configured_member_cap_turns_the_next_joiner_away(
    relay: RoomManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(room_manager_module, "MAX_MEMBERS", 2)
    room_id = relay.create_room()
    client = TestClient(main.app)
    with contextlib.ExitStack() as stack:
        for index in range(2):
            socket = stack.enter_context(client.websocket_connect(f"/ws/{room_id}"))
            _join(socket, f"member{index}")
            _wait_for(lambda: relay.get(room_id).member_count == index + 1)
        third = stack.enter_context(client.websocket_connect(f"/ws/{room_id}"))
        _join(third, "member2")
        for _ in range(MAX_FRAMES):
            frame = third.receive_json()
            if frame.get("type") == "name_rejected":
                assert frame["reason"] == "Room full"
                break
        else:
            raise AssertionError("never received name_rejected")
        assert relay.get(room_id).member_count == 2


# --- rooms -----------------------------------------------------------------------------


def test_more_than_a_hundred_rooms_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(room_manager_module, "MAX_ROOMS", 0)
    manager = RoomManager(store=MemoryRoomStore())

    codes = {manager.create_room() for _ in range(150)}

    assert len(codes) == 150 and manager.room_count == 150


def test_a_configured_room_cap_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(room_manager_module, "MAX_ROOMS", 3)
    manager = RoomManager(store=MemoryRoomStore())
    for _ in range(3):
        manager.create_room()

    with pytest.raises(RuntimeError, match=r"Maximum number of rooms reached \(3\)"):
        manager.create_room()


# --- zero means unlimited everywhere else ---------------------------------------------


def test_a_zero_rate_limit_allows_everything() -> None:
    limiter = limits.FixedWindowLimiter(limit=0, window_seconds=60, max_keys=1)

    assert all(limiter.check("1.2.3.4").allowed for _ in range(1000))
    assert limiter.tracked_keys == 0


def test_zero_tracked_ips_does_not_evict() -> None:
    limiter = limits.FixedWindowLimiter(limit=5, window_seconds=60, max_keys=0)
    for index in range(100):
        limiter.check(f"10.0.0.{index}")

    assert limiter.tracked_keys == 100


def test_a_zero_room_message_limit_has_no_quota(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "ROOM_MESSAGE_LIMIT", 0)
    room = Room()

    assert room.message_quota is None
    assert all(room.allow_message() for _ in range(10_000))


def test_zero_payload_caps_keep_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("MAX_TEXT_BYTES", "MAX_DIFF_BYTES", "MAX_FILES", "MAX_SYMBOLS", "MAX_LINE_RANGES"):
        monkeypatch.setattr(sanitize, name, 0)
    message = ActivityMessage(
        user_id="sam",
        prompt="p" * 10_000,
        changes=[
            {"file_path": f"f{index}.py", "diff": "d" * 100_000, "symbols": ["s"] * 100}
            for index in range(60)
        ],
    )

    sanitize.cap_activity(message)

    assert len(message.prompt or "") == 10_000
    assert len(message.changes) == 60
    assert len(message.changes[0].diff) == 100_000
    assert len(message.changes[0].symbols) == 100
    assert sanitize.truncate_utf8("x" * 10, 0) == "x" * 10


def test_a_zero_queue_length_holds_every_member() -> None:
    queues = file_queue.FileQueues(max_length=0, max_files=0)
    key = ("", "a.py")

    positions = [queues.enqueue(key, f"m{index}", "claude-code", 0.0) for index in range(20)]

    assert positions == list(range(1, 21))


def test_zero_lru_bounds_keep_every_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(room_module, "MAX_TRACKED_DECISIONS", 0)
    monkeypatch.setattr(room_module, "MAX_KNOWN_PATHS", 0)
    room = Room()
    for index in range(1500):
        room.register_decision(f"d{index}", "file")
        room.remember_path("", f"src/f{index}.py")

    assert room.decision_scope("d0") == "file"
    assert len(room._known_paths) == 1500
