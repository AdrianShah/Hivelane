"""Owner resolve actions from the dashboard: release a held edit, let one
member go first on a contested file, ping an agent; and the room expiry the
dashboard shows.

Room-level tests drive Room directly (as test_file_queue.py does); the rest go
through the relay's WebSocket endpoint with a real owner token."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from typing import Any, Optional

import pytest
from fastapi.testclient import TestClient

try:
    from server import main
    from server.file_queue import OWNER_GRACE_SECONDS
    from server.main import app
    from server.models import PROTOCOL_VERSION, ActivityMessage, FileChange, RoomSettings
    from server.room import HOLD_RELEASABLE_SECONDS, MAX_HELD_EDITS, OWNER_RELEASE_SECONDS, Room
    from server.room_manager import RoomManager
    from server.store import ROOM_TTL_SECONDS, MemoryRoomStore
except ImportError:  # Render rootDir=server
    import main  # type: ignore[no-redef]
    from file_queue import OWNER_GRACE_SECONDS  # type: ignore[no-redef]
    from main import app  # type: ignore[no-redef]
    from models import PROTOCOL_VERSION, ActivityMessage, FileChange, RoomSettings  # type: ignore[no-redef]
    from room import HOLD_RELEASABLE_SECONDS, MAX_HELD_EDITS, OWNER_RELEASE_SECONDS, Room  # type: ignore[no-redef]
    from room_manager import RoomManager  # type: ignore[no-redef]
    from store import ROOM_TTL_SECONDS, MemoryRoomStore  # type: ignore[no-redef]

T0 = 1_000_000.0
BLOCK = 20.0
WARN = 60.0
FILE = "src/app.py"


# --- Room: go first ---------------------------------------------------------------


class FakeSocket:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.sent.append(payload)

    async def send_text(self, text: str) -> None:
        import json

        self.sent.append(json.loads(text))

    def of_type(self, kind: str) -> list[dict[str, Any]]:
        return [frame for frame in self.sent if frame.get("type") == kind]


def make_room(*names: str) -> tuple[Room, dict[str, FakeSocket]]:
    room = Room(RoomSettings(block_window_seconds=BLOCK, warn_window_seconds=WARN))
    sockets = {}
    for name in names:
        sockets[name] = FakeSocket()
        room.add_member(name, sockets[name], "cursor")  # type: ignore[arg-type]
    return room, sockets


def touch(room: Room, user: str, at: float, path: str = FILE) -> None:
    room.record_activity(
        ActivityMessage(user_id=user, timestamp=at, changes=[FileChange(file_path=path, diff="@@")])
    )


def check(room: Room, user: str, at: float, path: str = FILE):
    asyncio.run(room.tick_file_queues(at))
    verdict = room.evaluate_edit(user, path, at)
    outcome = room.queue_edit_check(user, "codex", "", path, verdict, at)
    asyncio.run(room.deliver_queue_notices(outcome.notices))
    return outcome


def go_first(room: Room, user: str, at: float, path: str = FILE):
    result = room.go_first("", path, user, at)
    assert result is not None
    asyncio.run(room.deliver_queue_notices(result.notices))
    return result


def test_letting_the_held_member_go_first_releases_the_file_to_it() -> None:
    room, sockets = make_room("ana", "ben")
    touch(room, "ana", T0)
    assert check(room, "ben", T0 + 2).decision == "block"

    result = go_first(room, "ben", T0 + 3)

    # ben is told it is its turn, and why; ana hears ben is waiting.
    [released] = sockets["ben"].of_type("file_released")
    assert released["file_path"] == FILE and released["reason"] == "owner"
    assert [f["by"] for f in sockets["ana"].of_type("file_requested")] == ["ben"]
    assert result.waiting == [("ana", 2)]
    # ana's fresh touch no longer holds ben off: a warn, not a block.
    outcome = check(room, "ben", T0 + 4)
    assert outcome.decision == "warn"
    # ana now waits behind ben (its claim, then its touch).
    outcome = check(room, "ana", T0 + 5)
    assert outcome.decision == "block"
    assert outcome.reason.startswith("ben just took src/app.py")


def test_letting_the_holder_go_first_keeps_the_requester_waiting() -> None:
    room, _sockets = make_room("ana", "ben")
    touch(room, "ana", T0)
    check(room, "ben", T0 + 1)

    result = go_first(room, "ana", T0 + 2)

    assert result.waiting == [("ben", 2)]
    assert room.file_queues.head(("", FILE)).user_id == "ana"  # type: ignore[union-attr]
    outcome = check(room, "ben", T0 + 3)
    assert outcome.decision == "block"
    assert outcome.queue_position == 2
    assert check(room, "ana", T0 + 4).decision != "block"


def test_a_go_first_lapses_on_its_own() -> None:
    room, _sockets = make_room("ana", "ben")
    go_first(room, "ben", T0)
    assert room.has_override("", FILE, "ben", T0 + OWNER_RELEASE_SECONDS - 1)
    assert not room.has_override("", FILE, "ben", T0 + OWNER_RELEASE_SECONDS + 1)
    # An unused release passes on after the owner grace, like any release.
    tick_at = T0 + OWNER_GRACE_SECONDS + 1
    asyncio.run(room.tick_file_queues(tick_at))
    assert room.file_queues.position(("", FILE), "ben") == 0


def test_a_second_go_first_on_the_file_withdraws_the_first() -> None:
    room, _sockets = make_room("ana", "ben")
    touch(room, "ana", T0)
    check(room, "ben", T0 + 1)
    go_first(room, "ben", T0 + 2)
    go_first(room, "ana", T0 + 3)
    assert room.has_override("", FILE, "ana", T0 + 4)
    assert not room.has_override("", FILE, "ben", T0 + 4)


def test_go_first_for_someone_not_in_the_room_changes_nothing() -> None:
    room, _sockets = make_room("ana")
    assert room.go_first("", FILE, "nobody", T0) is None
    assert not room.file_queues


def test_leaving_drops_the_go_first() -> None:
    room, _sockets = make_room("ana", "ben")
    go_first(room, "ben", T0)
    room.remove_member("ben")
    assert not room.has_override("", FILE, "ben", T0 + 1)


def test_held_edits_are_remembered_for_a_while_and_bounded() -> None:
    room, _sockets = make_room("ana")
    room.record_hold("d1", "ana", "", FILE, T0, e2e="blob")
    held = room.held_edit("d1", T0 + 1)
    assert held is not None and (held.user_id, held.file_path, held.e2e) == ("ana", FILE, "blob")
    assert room.held_edit("d1", T0 + HOLD_RELEASABLE_SECONDS + 1) is None
    for index in range(MAX_HELD_EDITS + 5):
        room.record_hold(f"x{index}", "ana", "", FILE, T0)
    assert room.held_edit("x0", T0) is None
    assert room.held_edit(f"x{MAX_HELD_EDITS + 4}", T0) is not None


# --- room expiry ----------------------------------------------------------------


def test_room_settings_carry_the_rooms_lifetime() -> None:
    manager = RoomManager(store=MemoryRoomStore())
    before = time.time()
    code = manager.create_room()
    room = manager.get(code)
    assert room is not None
    update = room.settings_update()
    assert before <= update.created_at <= time.time()
    assert update.expires_at == pytest.approx(update.created_at + ROOM_TTL_SECONDS)


def test_a_revived_room_keeps_its_original_expiry() -> None:
    store = MemoryRoomStore()
    first = RoomManager(store=store)
    code = first.create_room()
    created = first.get(code).created_at  # type: ignore[union-attr]
    # A restart: a new manager over the same store revives the code.
    revived = RoomManager(store=store).get(code)
    assert revived is not None
    assert revived.created_at == pytest.approx(created)
    assert revived.expires_at == pytest.approx(created + ROOM_TTL_SECONDS)


# --- over the WebSocket -----------------------------------------------------------

MAX_FRAMES = 40


@pytest.fixture
def relay(monkeypatch: pytest.MonkeyPatch) -> Iterator[RoomManager]:
    manager = RoomManager(store=MemoryRoomStore())
    monkeypatch.setattr(main, "room_manager", manager)
    yield manager


# Events already read off a socket but not yet asked for: one feed frame
# batches several, and a match must not throw away the ones after it.
_unread: dict[int, list[dict]] = {}


def _next(socket: Any, type_name: str, **match: Any) -> dict:
    pending = _unread.setdefault(id(socket), [])
    for _ in range(MAX_FRAMES):
        while pending:
            event = pending.pop(0)
            if event.get("type") == type_name and all(event.get(k) == v for k, v in match.items()):
                return event
        frame = socket.receive_json()
        pending.extend(frame["events"] if frame.get("type") == "hivelane_feed" else [frame])
    raise AssertionError(f"never received {type_name} {match}")


def _join(socket: Any, name: str) -> None:
    socket.send_json(
        {"type": "join", "name": name, "agent": "claude-code", "protocol_version": PROTOCOL_VERSION}
    )
    _next(socket, "user_joined", user_id=name)


def _spectate(socket: Any, owner_token: Optional[str] = None) -> None:
    frame: dict[str, Any] = {"type": "join", "name": None, "role": "spectator"}
    if owner_token is not None:
        frame["owner_token"] = owner_token
    socket.send_json(frame)
    _next(socket, "room_settings")


def _edit(socket: Any, name: str) -> None:
    socket.send_json(
        {
            "type": "activity",
            "user_id": name,
            "timestamp": time.time(),
            "prompt": None,
            "changes": [{"file_path": FILE, "diff": "@@ -1 +1 @@\n-a\n+b"}],
        }
    )


def _check(socket: Any, request_id: str) -> dict:
    socket.send_json({"type": "edit_check", "request_id": request_id, "file_path": FILE})
    return _next(socket, "edit_check_response", request_id=request_id)


def _held_room(client: TestClient):
    """A room where ana holds FILE and ben's edit of it was just blocked."""
    body = client.post("/rooms").json()
    return body["room_id"], body["owner_token"]


def test_an_owner_releases_a_held_edit(relay: RoomManager) -> None:
    client = TestClient(app)
    code, token = _held_room(client)
    with (
        client.websocket_connect(f"/ws/{code}") as owner,
        client.websocket_connect(f"/ws/{code}") as ana,
        client.websocket_connect(f"/ws/{code}") as ben,
    ):
        _spectate(owner, token)
        _join(ana, "ana")
        _join(ben, "ben")
        _edit(ana, "ana")
        _next(owner, "activity_update", user_id="ana")
        blocked = _check(ben, "r1")
        assert blocked["decision"] == "block"
        decision = _next(owner, "edit_decision", user_id="ben")
        assert decision["decision_id"] == blocked["decision_id"]

        owner.send_json({"type": "hold_release", "decision_id": blocked["decision_id"]})

        released = _next(owner, "hold_released")
        assert (released["user_id"], released["file_path"], released["by"]) == ("ben", FILE, "room owner")
        assert released["decision_id"] == blocked["decision_id"]
        # ana, the holder, now waits behind ben, and the dashboard says so.
        waiting = _next(owner, "edit_decision", user_id="ana")
        assert (waiting["decision"], waiting["queue_position"], waiting["others"]) == ("block", 2, ["ben"])
        assert _next(ben, "file_released")["reason"] == "owner"
        assert _check(ben, "r2")["decision"] == "warn"


def test_only_an_owner_may_release_reorder_or_ping(relay: RoomManager) -> None:
    client = TestClient(app)
    code, _token = _held_room(client)
    with (
        client.websocket_connect(f"/ws/{code}") as viewer,
        client.websocket_connect(f"/ws/{code}") as ana,
        client.websocket_connect(f"/ws/{code}") as ben,
    ):
        _spectate(viewer)  # no owner token
        _join(ana, "ana")
        _join(ben, "ben")
        _edit(ana, "ana")
        blocked = _check(ben, "r1")
        viewer.send_json({"type": "hold_release", "decision_id": blocked["decision_id"]})
        viewer.send_json({"type": "queue_order", "file_path": FILE, "first_user_id": "ben"})
        viewer.send_json({"type": "nudge", "to_user_id": "ben", "text": "hi"})
        # Still blocked: none of the three did anything.
        assert _check(ben, "r2")["decision"] == "block"
        room = relay.get(code)
        assert room is not None and not room.has_override("", FILE, "ben", time.time())


def test_an_owner_lets_one_member_go_first(relay: RoomManager) -> None:
    client = TestClient(app)
    code, token = _held_room(client)
    with (
        client.websocket_connect(f"/ws/{code}") as owner,
        client.websocket_connect(f"/ws/{code}") as ana,
        client.websocket_connect(f"/ws/{code}") as ben,
    ):
        _spectate(owner, token)
        _join(ana, "ana")
        _join(ben, "ben")
        _edit(ana, "ana")
        _next(owner, "activity_update", user_id="ana")
        assert _check(ben, "r1")["decision"] == "block"

        owner.send_json({"type": "queue_order", "file_path": FILE, "first_user_id": "ben"})

        reordered = _next(owner, "queue_reordered")
        assert (reordered["first_user_id"], reordered["waiting"]) == ("ben", ["ana"])
        assert _next(ben, "file_released")["reason"] == "owner"
        assert _check(ben, "r2")["decision"] == "warn"


def test_an_owner_pings_an_agent(relay: RoomManager) -> None:
    client = TestClient(app)
    code, token = _held_room(client)
    with (
        client.websocket_connect(f"/ws/{code}") as owner,
        client.websocket_connect(f"/ws/{code}") as ben,
    ):
        _spectate(owner, token)
        _join(ben, "ben")

        owner.send_json({"type": "nudge", "to_user_id": "ben", "text": "wrap up\x1b[31m auth‮.py\n"})

        got = _next(ben, "nudged")
        assert (got["user_id"], got["by"], got["text"]) == ("ben", "room owner", "wrap up [31m auth .py")
        seen = _next(owner, "nudged")
        assert seen["delivered"] is True and seen["text"] == got["text"]


def test_a_ping_in_an_e2e_room_carries_no_text(relay: RoomManager) -> None:
    client = TestClient(app)
    body = client.post("/rooms", json={"settings": {"e2e": True}}).json()
    code, token = body["room_id"], body["owner_token"]
    with client.websocket_connect(f"/ws/{code}") as owner, client.websocket_connect(f"/ws/{code}") as ben:
        _spectate(owner, token)
        _join(ben, "ben")
        owner.send_json({"type": "nudge", "to_user_id": "ben", "text": "src/secret_plan.py is contested"})
        assert _next(ben, "nudged")["text"] == ""
        assert _next(owner, "nudged")["text"] == ""


def test_a_ping_for_someone_not_in_the_room_is_dropped(relay: RoomManager) -> None:
    client = TestClient(app)
    code, token = _held_room(client)
    with client.websocket_connect(f"/ws/{code}") as owner, client.websocket_connect(f"/ws/{code}") as ben:
        _spectate(owner, token)
        _join(ben, "ben")
        owner.send_json({"type": "nudge", "to_user_id": "ghost", "text": "hello"})
        owner.send_json({"type": "nudge", "to_user_id": "ben", "text": "second"})
        # The first ping produced nothing; the next frame is the second one.
        assert _next(owner, "nudged")["user_id"] == "ben"


def test_spectators_see_the_rooms_expiry(relay: RoomManager) -> None:
    client = TestClient(app)
    code, _token = _held_room(client)
    with client.websocket_connect(f"/ws/{code}") as viewer:
        viewer.send_json({"type": "join", "name": None, "role": "spectator"})
        settings = _next(viewer, "room_settings")
        assert settings["expires_at"] == pytest.approx(settings["created_at"] + ROOM_TTL_SECONDS)
