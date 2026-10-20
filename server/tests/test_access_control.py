"""Owner tokens and join approval.

POST /rooms hands back an owner token once; the relay keeps only its SHA-256.
A join presenting it makes that socket an owner, who may switch on "require
approval". From then on every other joiner, member or dashboard, waits in a
bounded queue until an owner approves, declines, or the wait times out."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Optional

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

try:
    from server import main, metrics
    from server import room as room_module
    from server.main import (
        REASON_APPROVAL_DECLINED,
        REASON_APPROVAL_QUEUE_FULL,
        REASON_APPROVAL_TIMED_OUT,
        SPECTATOR_DISPLAY_NAME,
        WS_CLOSE_JOIN_REFUSED,
        app,
    )
    from server.models import PROTOCOL_VERSION
    from server.room import Room, hash_owner_token, new_owner_token
    from server.room_manager import RoomManager
    from server.store import MemoryRoomStore, SqliteRoomStore
except ImportError:  # Render rootDir=server
    import main  # type: ignore[no-redef]
    import metrics  # type: ignore[no-redef]
    import room as room_module  # type: ignore[no-redef]
    from main import (  # type: ignore[no-redef]
        REASON_APPROVAL_DECLINED,
        REASON_APPROVAL_QUEUE_FULL,
        REASON_APPROVAL_TIMED_OUT,
        SPECTATOR_DISPLAY_NAME,
        WS_CLOSE_JOIN_REFUSED,
        app,
    )
    from models import PROTOCOL_VERSION  # type: ignore[no-redef]
    from room import Room, hash_owner_token, new_owner_token  # type: ignore[no-redef]
    from room_manager import RoomManager  # type: ignore[no-redef]
    from store import MemoryRoomStore, SqliteRoomStore  # type: ignore[no-redef]

# Frames to read before giving up on a type: enough to step past catch-up and
# queued feed batches.
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


def _create(client: TestClient) -> tuple[str, str]:
    body = client.post("/rooms").json()
    return body["room_id"], body["owner_token"]


def _join(socket: Any, name: str, owner_token: Optional[str] = None) -> None:
    frame: dict[str, Any] = {
        "type": "join",
        "name": name,
        "agent": "claude-code",
        "protocol_version": PROTOCOL_VERSION,
    }
    if owner_token is not None:
        frame["owner_token"] = owner_token
    socket.send_json(frame)


def _spectate(socket: Any, owner_token: Optional[str] = None) -> None:
    frame: dict[str, Any] = {"type": "join", "name": None}
    if owner_token is not None:
        frame["owner_token"] = owner_token
    socket.send_json(frame)


def _frames(socket: Any) -> Iterator[dict]:
    """Every event the socket receives, feed batches unpacked, in order."""
    for _ in range(MAX_FRAMES):
        frame = socket.receive_json()
        if frame.get("type") == "synergo_feed":
            yield from frame["events"]
        else:
            yield frame


def _next(socket: Any, type_name: str) -> dict:
    for event in _frames(socket):
        if event.get("type") == type_name:
            return event
    raise AssertionError(f"never received {type_name}")


def _admitted(socket: Any) -> bool:
    """True once the relay answers an edit_check, which it does for members only."""
    socket.send_json({"type": "edit_check", "request_id": "probe", "file_path": "x.py"})
    return _next(socket, "edit_check_response")["request_id"] == "probe"


def _let_in(socket: Any, request_id: str) -> None:
    """A waiting joiner learns it is admitted from its own join_resolved."""
    assert _next(socket, "join_resolved") == {
        "type": "join_resolved",
        "request_id": request_id,
        "outcome": "approved",
    }


def _refused(socket: Any) -> tuple[str, int]:
    """(room_rejected reason, close code) for a socket the relay turned away."""
    reason = _next(socket, "room_rejected")["reason"]
    with pytest.raises(WebSocketDisconnect) as closed:
        socket.receive_json()
    return reason, closed.value.code


def _require_approval(owner: Any, value: bool = True) -> None:
    """Set the policy from an owner socket and wait until the relay applied it
    (the catch-up copy of the old policy may still be unread)."""
    owner.send_json({"type": "room_policy", "require_approval": value})
    for event in _frames(owner):
        if event.get("type") == "room_policy_update" and event["require_approval"] is value:
            return
    raise AssertionError("policy change never broadcast")


# --- owner tokens ---------------------------------------------------------------


def test_post_rooms_returns_an_owner_token_and_keeps_only_its_hash(
    relay: RoomManager,
) -> None:
    body = TestClient(app).post("/rooms").json()
    token = body["owner_token"]

    assert isinstance(token, str) and len(token) >= 43  # 32 random bytes
    assert token != body["room_id"]
    room = relay.get(body["room_id"])
    assert room is not None
    record = room.access_record()
    assert record == {
        "owner_hash": hashlib.sha256(token.encode()).hexdigest(),
        "require_approval": False,
    }
    assert token not in json.dumps(record)
    assert room.verify_owner_token(token)


def test_every_room_gets_a_different_owner_token(relay: RoomManager) -> None:
    client = TestClient(app)

    assert _create(client)[1] != _create(client)[1]


@pytest.mark.parametrize("token", [None, "", "not-the-token"])
def test_only_the_exact_token_verifies(token: Optional[str]) -> None:
    room = Room()
    real = new_owner_token()
    room.set_owner_hash(hash_owner_token(real))

    assert room.verify_owner_token(real)
    assert not room.verify_owner_token(token)


def test_a_room_without_an_owner_has_no_owners() -> None:
    room = Room()

    assert not room.has_owner
    assert not room.verify_owner_token(new_owner_token())


def test_owner_hash_and_policy_survive_a_restart(tmp_path: Path) -> None:
    state_path = str(tmp_path / "synergo.db")
    first = RoomManager(store=SqliteRoomStore(state_path))
    room_id = first.create_room()
    room = first.get(room_id)
    assert room is not None
    token = new_owner_token()
    room.set_owner_hash(hash_owner_token(token))
    room.set_require_approval(True)
    first.save_access(room_id, room)

    revived = RoomManager(store=SqliteRoomStore(state_path)).get(room_id)

    assert revived is not None
    assert revived.verify_owner_token(token)
    assert revived.require_approval
    assert token.encode() not in Path(state_path).read_bytes()


def test_store_without_an_access_column_is_migrated(tmp_path: Path) -> None:
    import sqlite3

    state_path = str(tmp_path / "old.db")
    conn = sqlite3.connect(state_path)
    conn.execute(
        "CREATE TABLE rooms (room_id TEXT PRIMARY KEY, created_at REAL NOT NULL, "
        "settings TEXT)"
    )
    conn.execute("INSERT INTO rooms VALUES ('ABCDEFGH', ?, NULL)", (time.time(),))
    conn.commit()
    conn.close()

    store = SqliteRoomStore(state_path)

    assert store.get_access("ABCDEFGH") is None
    revived = RoomManager(store=store).get("ABCDEFGH")
    assert revived is not None and not revived.has_owner and not revived.require_approval
    store.set_access("ABCDEFGH", {"owner_hash": "ab", "require_approval": True})
    assert store.get_access("ABCDEFGH") == {"owner_hash": "ab", "require_approval": True}


def test_stored_approval_without_an_owner_is_not_enforced() -> None:
    """Nobody could ever approve, so every joiner would just time out."""
    room = Room()
    room.load_access({"owner_hash": None, "require_approval": True})

    assert not room.require_approval


# --- policy ---------------------------------------------------------------------


def test_spectator_catch_up_reports_the_join_policy(relay: RoomManager) -> None:
    client = TestClient(app)
    room_id, _ = _create(client)

    with client.websocket_connect(f"/ws/{room_id}") as spectator:
        _spectate(spectator)
        assert spectator.receive_json()["type"] == "room_settings"
        policy = spectator.receive_json()

    assert policy == {"type": "room_policy_update", "require_approval": False}


def test_owner_policy_change_is_broadcast_and_persisted(relay: RoomManager) -> None:
    client = TestClient(app)
    room_id, token = _create(client)

    with client.websocket_connect(f"/ws/{room_id}") as owner:
        _spectate(owner, owner_token=token)
        with client.websocket_connect(f"/ws/{room_id}") as sam:
            _join(sam, "sam")
            assert _admitted(sam)
            _require_approval(owner)
            assert _next(sam, "room_policy_update") == {
                "type": "room_policy_update",
                "require_approval": True,
            }

    room = relay.get(room_id)
    assert room is not None and room.require_approval
    assert relay._store.get_access(room_id)["require_approval"] is True


def test_non_owners_cannot_change_the_policy(relay: RoomManager) -> None:
    client = TestClient(app)
    room_id, _ = _create(client)

    with client.websocket_connect(f"/ws/{room_id}") as sam:
        _join(sam, "sam", owner_token="guessed")
        sam.send_json({"type": "room_policy", "require_approval": True})
        # One socket's frames are handled in order: once this is answered,
        # the room_policy before it has been handled (and ignored).
        assert _admitted(sam)

    room = relay.get(room_id)
    assert room is not None and not room.require_approval


# --- approval -------------------------------------------------------------------


def test_owner_approves_a_waiting_member(relay: RoomManager) -> None:
    client = TestClient(app)
    room_id, token = _create(client)

    with client.websocket_connect(f"/ws/{room_id}") as owner:
        _spectate(owner, owner_token=token)
        _require_approval(owner)
        with client.websocket_connect(f"/ws/{room_id}") as sam:
            _join(sam, "sam")
            pending = _next(sam, "join_pending")
            request = _next(owner, "join_request")

            assert request["request_id"] == pending["request_id"]
            assert (request["name"], request["agent"]) == ("sam", "claude-code")
            assert request["timestamp"] > 0
            assert "approval" in pending["reason"]

            owner.send_json(
                {"type": "join_decision", "request_id": request["request_id"], "approve": True}
            )
            _let_in(sam, request["request_id"])
            assert _admitted(sam)
            resolved = _next(owner, "join_resolved")

    assert resolved == {
        "type": "join_resolved",
        "request_id": request["request_id"],
        "outcome": "approved",
    }


def test_owner_declines_a_waiting_member(relay: RoomManager) -> None:
    client = TestClient(app)
    room_id, token = _create(client)

    with client.websocket_connect(f"/ws/{room_id}") as owner:
        _spectate(owner, owner_token=token)
        _require_approval(owner)
        with client.websocket_connect(f"/ws/{room_id}") as sam:
            _join(sam, "sam")
            request = _next(owner, "join_request")
            owner.send_json(
                {"type": "join_decision", "request_id": request["request_id"], "approve": False}
            )
            reason, code = _refused(sam)
        assert _next(owner, "join_resolved")["outcome"] == "declined"

    assert reason == REASON_APPROVAL_DECLINED
    assert code == WS_CLOSE_JOIN_REFUSED
    room = relay.get(room_id)
    assert room is not None and "sam" not in room.member_ids
    assert metrics.ws_rejections_total.value("approval_declined") == 1


def test_a_waiting_member_times_out(
    relay: RoomManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main, "JOIN_APPROVAL_TIMEOUT_SECONDS", 0.3)
    client = TestClient(app)
    room_id, token = _create(client)

    with client.websocket_connect(f"/ws/{room_id}") as owner:
        _spectate(owner, owner_token=token)
        _require_approval(owner)
        with client.websocket_connect(f"/ws/{room_id}") as sam:
            _join(sam, "sam")
            assert _next(sam, "join_pending")
            reason, code = _refused(sam)
        assert _next(owner, "join_resolved")["outcome"] == "timed_out"

    assert (reason, code) == (REASON_APPROVAL_TIMED_OUT, WS_CLOSE_JOIN_REFUSED)
    assert metrics.ws_rejections_total.value("approval_timeout") == 1
    room = relay.get(room_id)
    assert room is not None and room.pending_count == 0


def test_joiners_wait_without_an_owner_online_until_timeout(
    relay: RoomManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main, "JOIN_APPROVAL_TIMEOUT_SECONDS", 0.3)
    client = TestClient(app)
    room_id, token = _create(client)
    with client.websocket_connect(f"/ws/{room_id}") as owner:
        _spectate(owner, owner_token=token)
        _require_approval(owner)

    with client.websocket_connect(f"/ws/{room_id}") as sam:
        _join(sam, "sam")
        assert "No owner is connected" in _next(sam, "join_pending")["reason"]
        assert _refused(sam)[0] == REASON_APPROVAL_TIMED_OUT


def test_the_pending_queue_is_bounded(
    relay: RoomManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(room_module, "MAX_PENDING_JOINS", 1)
    client = TestClient(app)
    room_id, token = _create(client)

    with client.websocket_connect(f"/ws/{room_id}") as owner:
        _spectate(owner, owner_token=token)
        _require_approval(owner)
        with client.websocket_connect(f"/ws/{room_id}") as sam:
            _join(sam, "sam")
            assert _next(sam, "join_pending")
            with client.websocket_connect(f"/ws/{room_id}") as priya:
                _join(priya, "priya")
                reason, code = _refused(priya)

    assert (reason, code) == (REASON_APPROVAL_QUEUE_FULL, WS_CLOSE_JOIN_REFUSED)
    assert metrics.ws_rejections_total.value("approval_queue_full") == 1


def test_a_joiner_who_leaves_is_withdrawn(relay: RoomManager) -> None:
    client = TestClient(app)
    room_id, token = _create(client)

    with client.websocket_connect(f"/ws/{room_id}") as owner:
        _spectate(owner, owner_token=token)
        _require_approval(owner)
        with client.websocket_connect(f"/ws/{room_id}") as sam:
            _join(sam, "sam")
            request = _next(owner, "join_request")
        resolved = _next(owner, "join_resolved")

    assert resolved["request_id"] == request["request_id"]
    assert resolved["outcome"] == "withdrawn"
    room = relay.get(room_id)
    assert room is not None and room.pending_count == 0


def test_spectators_need_approval_too(relay: RoomManager) -> None:
    client = TestClient(app)
    room_id, token = _create(client)

    with client.websocket_connect(f"/ws/{room_id}") as owner:
        _spectate(owner, owner_token=token)
        _require_approval(owner)
        with client.websocket_connect(f"/ws/{room_id}") as viewer:
            _spectate(viewer)
            assert _next(viewer, "join_pending")
            request = _next(owner, "join_request")
            assert request["name"] == SPECTATOR_DISPLAY_NAME

            owner.send_json(
                {"type": "join_decision", "request_id": request["request_id"], "approve": True}
            )
            # Admitted: told so, then the normal spectator catch-up follows.
            assert viewer.receive_json() == {
                "type": "join_resolved",
                "request_id": request["request_id"],
                "outcome": "approved",
            }
            assert viewer.receive_json()["type"] == "room_settings"
            assert viewer.receive_json()["require_approval"] is True


def test_owners_skip_approval_with_their_token(relay: RoomManager) -> None:
    client = TestClient(app)
    room_id, token = _create(client)

    with client.websocket_connect(f"/ws/{room_id}") as owner:
        _spectate(owner, owner_token=token)
        _require_approval(owner)
        with client.websocket_connect(f"/ws/{room_id}") as second_tab:
            _spectate(second_tab, owner_token=token)
            assert second_tab.receive_json()["type"] == "room_settings"
        with client.websocket_connect(f"/ws/{room_id}") as sam:
            _join(sam, "sam", owner_token=token)
            assert _admitted(sam)


def test_a_wrong_token_waits_like_everyone_else(relay: RoomManager) -> None:
    client = TestClient(app)
    room_id, token = _create(client)

    with client.websocket_connect(f"/ws/{room_id}") as owner:
        _spectate(owner, owner_token=token)
        _require_approval(owner)
        with client.websocket_connect(f"/ws/{room_id}") as sam:
            _join(sam, "sam", owner_token=new_owner_token())
            assert _next(sam, "join_pending")


def test_an_owner_member_can_decide(relay: RoomManager) -> None:
    """A daemon started with the owner token approves from the terminal."""
    client = TestClient(app)
    room_id, token = _create(client)

    with client.websocket_connect(f"/ws/{room_id}") as lead:
        _join(lead, "lead", owner_token=token)
        assert _next(lead, "room_policy_update")["require_approval"] is False
        _require_approval(lead)
        with client.websocket_connect(f"/ws/{room_id}") as sam:
            _join(sam, "sam")
            request = _next(lead, "join_request")
            lead.send_json(
                {"type": "join_decision", "request_id": request["request_id"], "approve": True}
            )
            _let_in(sam, request["request_id"])
            assert _admitted(sam)


def test_non_owner_decisions_are_ignored(relay: RoomManager) -> None:
    client = TestClient(app)
    room_id, token = _create(client)

    with client.websocket_connect(f"/ws/{room_id}") as owner:
        _spectate(owner, owner_token=token)
        with client.websocket_connect(f"/ws/{room_id}") as priya:
            _join(priya, "priya")  # admitted before approval was required
            assert _admitted(priya)
            _require_approval(owner)
            with client.websocket_connect(f"/ws/{room_id}") as sam:
                _join(sam, "sam")
                request = _next(owner, "join_request")
                priya.send_json(
                    {"type": "join_decision", "request_id": request["request_id"], "approve": True}
                )
                assert _admitted(priya)  # priya's decision has been handled
                room = relay.get(room_id)
                assert room is not None and room.pending_count == 1

                owner.send_json(
                    {"type": "join_decision", "request_id": request["request_id"], "approve": False}
                )
                assert _refused(sam)[0] == REASON_APPROVAL_DECLINED


# Reads join_resolved before the policy broadcast: that order comes from the
# debounce, so it runs on the production flush window.
@pytest.mark.real_feed_debounce
def test_switching_approval_off_admits_everyone_waiting(relay: RoomManager) -> None:
    client = TestClient(app)
    room_id, token = _create(client)

    with client.websocket_connect(f"/ws/{room_id}") as owner:
        _spectate(owner, owner_token=token)
        _require_approval(owner)
        with client.websocket_connect(f"/ws/{room_id}") as sam:
            _join(sam, "sam")
            pending = _next(sam, "join_pending")
            assert _next(owner, "join_request")["request_id"] == pending["request_id"]
            owner.send_json({"type": "room_policy", "require_approval": False})
            # join_resolved goes to owners at once; the policy broadcast follows
            # in the next debounced feed, so read them in that order.
            resolved = _next(owner, "join_resolved")
            assert _next(owner, "room_policy_update")["require_approval"] is False
            _let_in(sam, pending["request_id"])
            assert _admitted(sam)

    assert resolved == {
        "type": "join_resolved",
        "request_id": pending["request_id"],
        "outcome": "approved",
    }
    room = relay.get(room_id)
    assert room is not None and not room.require_approval


def test_an_owner_who_connects_later_sees_who_is_waiting(relay: RoomManager) -> None:
    client = TestClient(app)
    room_id, token = _create(client)
    with client.websocket_connect(f"/ws/{room_id}") as owner:
        _spectate(owner, owner_token=token)
        _require_approval(owner)

    with client.websocket_connect(f"/ws/{room_id}") as sam:
        _join(sam, "sam")
        pending = _next(sam, "join_pending")
        with client.websocket_connect(f"/ws/{room_id}") as owner:
            _spectate(owner, owner_token=token)
            request = _next(owner, "join_request")
            assert request["request_id"] == pending["request_id"]
            owner.send_json(
                {"type": "join_decision", "request_id": request["request_id"], "approve": True}
            )
            _let_in(sam, request["request_id"])
            assert _admitted(sam)


def test_approved_joiner_whose_name_was_taken_meanwhile_is_asked_again(
    relay: RoomManager,
) -> None:
    client = TestClient(app)
    room_id, token = _create(client)

    with client.websocket_connect(f"/ws/{room_id}") as owner:
        _spectate(owner, owner_token=token)
        _require_approval(owner)
        with client.websocket_connect(f"/ws/{room_id}") as impostor:
            _join(impostor, "sam")
            request = _next(owner, "join_request")
            with client.websocket_connect(f"/ws/{room_id}") as sam:
                _join(sam, "sam", owner_token=token)  # the owner's own daemon
                assert _admitted(sam)
                owner.send_json(
                    {"type": "join_decision", "request_id": request["request_id"], "approve": True}
                )
                assert _next(impostor, "name_rejected")["reason"] == "Name taken"


def test_rooms_without_approval_admit_as_before(relay: RoomManager) -> None:
    client = TestClient(app)
    room_id, _ = _create(client)

    with client.websocket_connect(f"/ws/{room_id}") as sam:
        _join(sam, "sam")
        assert _admitted(sam)
