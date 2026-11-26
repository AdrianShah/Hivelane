"""Invite links, member tokens, rotation and revocation.

POST /rooms hands back an invite secret next to the owner token. A join that
presents it (the share link's `#invite=`), the owner token or a member token
comes straight in; a join with only the room code waits for an owner's
approval. Whoever is let in other than an owner gets a member token, so a
reconnect is not asked again, until an owner revokes it. Rotating the invite
secret makes old links knock. Rooms created without invite links (before them,
or with HIVELANE_INVITE_LINKS off) admit the code alone, as they always did."""

from __future__ import annotations

import json
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
        REASON_MEMBER_REVOKED,
        SPECTATOR_DISPLAY_NAME,
        WS_CLOSE_JOIN_REFUSED,
        app,
    )
    from server.models import PROTOCOL_VERSION
    from server.room import (
        Room,
        hash_owner_token,
        new_invite_secret,
        new_owner_token,
        unwrap_invite_secret,
        wrap_invite_secret,
    )
    from server.room_manager import RoomManager
    from server.store import MemoryRoomStore, SqliteRoomStore
except ImportError:  # Render rootDir=server
    import main  # type: ignore[no-redef]
    import metrics  # type: ignore[no-redef]
    import room as room_module  # type: ignore[no-redef]
    from main import (  # type: ignore[no-redef]
        REASON_MEMBER_REVOKED,
        SPECTATOR_DISPLAY_NAME,
        WS_CLOSE_JOIN_REFUSED,
        app,
    )
    from models import PROTOCOL_VERSION  # type: ignore[no-redef]
    from room import (  # type: ignore[no-redef]
        Room,
        hash_owner_token,
        new_invite_secret,
        new_owner_token,
        unwrap_invite_secret,
        wrap_invite_secret,
    )
    from room_manager import RoomManager  # type: ignore[no-redef]
    from store import MemoryRoomStore, SqliteRoomStore  # type: ignore[no-redef]

pytestmark = pytest.mark.invite_links

MAX_FRAMES = 30


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


def _create(client: TestClient) -> tuple[str, str, str]:
    body = client.post("/rooms").json()
    return body["room_id"], body["owner_token"], body["invite_secret"]


def _join(socket: Any, name: str, **secrets: Optional[str]) -> None:
    frame: dict[str, Any] = {
        "type": "join",
        "name": name,
        "agent": "claude-code",
        "protocol_version": PROTOCOL_VERSION,
    }
    frame.update({k: v for k, v in secrets.items() if v is not None})
    socket.send_json(frame)


def _spectate(socket: Any, **secrets: Optional[str]) -> None:
    frame: dict[str, Any] = {"type": "join", "name": None}
    frame.update({k: v for k, v in secrets.items() if v is not None})
    socket.send_json(frame)


def _frames(socket: Any) -> Iterator[dict]:
    for _ in range(MAX_FRAMES):
        frame = socket.receive_json()
        if frame.get("type") == "hivelane_feed":
            yield from frame["events"]
        else:
            yield frame


def _next(socket: Any, type_name: str) -> dict:
    for event in _frames(socket):
        if event.get("type") == type_name:
            return event
    raise AssertionError(f"never received {type_name}")


def _first(socket: Any) -> dict:
    """The very first frame after a join: join_pending means held at the door."""
    return socket.receive_json()


def _admitted(socket: Any) -> bool:
    socket.send_json({"type": "edit_check", "request_id": "probe", "file_path": "x.py"})
    return _next(socket, "edit_check_response")["request_id"] == "probe"


def _approve_next(owner: Any) -> str:
    request = _next(owner, "join_request")
    owner.send_json(
        {"type": "join_decision", "request_id": request["request_id"], "approve": True}
    )
    return request["request_id"]


# --- creation and storage ------------------------------------------------------


def test_post_rooms_returns_an_invite_secret_and_keeps_only_its_hash(
    relay: RoomManager,
) -> None:
    body = TestClient(app).post("/rooms").json()
    secret, token = body["invite_secret"], body["owner_token"]

    assert isinstance(secret, str) and len(secret) >= 32  # >= 192 bits, URL-safe
    assert all(c.isalnum() or c in "-_" for c in secret)
    assert secret != token
    room = relay.get(body["room_id"])
    assert room is not None and room.invite_required
    record = room.access_record()
    assert record["invite_hash"] == hash_owner_token(secret)
    stored = json.dumps(record)
    assert secret not in stored and token not in stored
    assert room.verify_invite_secret(secret)
    # The wrapped copy opens only with the owner token.
    assert room.invite_secret(token) == secret
    assert room.invite_secret(new_owner_token()) is None


def test_every_room_gets_its_own_invite_secret(relay: RoomManager) -> None:
    client = TestClient(app)

    assert _create(client)[2] != _create(client)[2]


def test_relay_switch_creates_code_only_rooms(
    relay: RoomManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main, "INVITE_LINKS", False)
    body = TestClient(app).post("/rooms").json()

    assert "invite_secret" not in body
    room = relay.get(body["room_id"])
    assert room is not None and not room.invite_required


def test_wrap_needs_the_owner_token() -> None:
    secret, token = new_invite_secret(), new_owner_token()
    wrapped = wrap_invite_secret(secret, token)

    assert unwrap_invite_secret(wrapped, token) == secret
    assert unwrap_invite_secret(wrapped, new_owner_token()) != secret
    assert secret not in json.dumps(wrapped)
    # A fresh nonce each time: the same secret never wraps the same way twice.
    assert wrap_invite_secret(secret, token) != wrapped
    assert unwrap_invite_secret({"nonce": "zz", "ct": "00"}, token) is None
    assert unwrap_invite_secret(None, token) is None


def test_invite_hash_members_and_wrap_survive_a_restart(tmp_path: Path) -> None:
    state_path = str(tmp_path / "hivelane.db")
    first = RoomManager(store=SqliteRoomStore(state_path))
    room_id = first.create_room()
    room = first.get(room_id)
    assert room is not None
    token, secret = new_owner_token(), new_invite_secret()
    room.set_owner_hash(hash_owner_token(token))
    room.set_invite_secret(secret, token)
    member_id, member_token = room.remember_member("sam")
    first.save_access(room_id, room)

    revived = RoomManager(store=SqliteRoomStore(state_path)).get(room_id)

    assert revived is not None
    assert revived.verify_invite_secret(secret)
    assert revived.verify_member_token(member_token) == member_id
    assert revived.invite_secret(token) == secret
    on_disk = Path(state_path).read_bytes()
    for plain in (token, secret, member_token):
        assert plain.encode() not in on_disk


def test_a_stored_invite_without_an_owner_is_not_enforced() -> None:
    """Nobody could approve the code-only joins it would hold."""
    room = Room()
    room.load_access({"owner_hash": None, "invite_hash": "ab" * 32})

    assert not room.invite_required


def test_remembered_members_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(room_module, "MAX_REMEMBERED_MEMBERS", 3)
    room = Room()
    room.set_owner_hash(hash_owner_token(new_owner_token()))
    grants = [room.remember_member(f"m{i}", now=float(i)) for i in range(5)]

    assert [m.name for m in room.remembered_members()] == ["m2", "m3", "m4"]
    assert room.verify_member_token(grants[0][1]) is None
    assert room.verify_member_token(grants[4][1]) == grants[4][0]


@pytest.mark.parametrize("token", [None, "", "x" * 10_000, "not-a-token"])
def test_only_an_issued_member_token_verifies(token: Optional[str]) -> None:
    room = Room()
    room.set_owner_hash(hash_owner_token(new_owner_token()))
    room.remember_member("sam")

    assert room.verify_member_token(token) is None


# --- the admission matrix --------------------------------------------------------


def test_the_invite_secret_admits_a_member_at_once_and_issues_a_member_token(
    relay: RoomManager,
) -> None:
    client = TestClient(app)
    room_id, _, secret = _create(client)

    with client.websocket_connect(f"/ws/{room_id}") as sam:
        _join(sam, "sam", invite_secret=secret)
        issued = _first(sam)
        assert issued["type"] == "member_token"
        assert issued["token"] and issued["member_id"]
        assert _admitted(sam)

    room = relay.get(room_id)
    assert room is not None
    assert room.verify_member_token(issued["token"]) == issued["member_id"]
    assert issued["token"] not in json.dumps(relay._store.get_access(room_id))


def test_the_invite_secret_admits_a_dashboard_at_once(relay: RoomManager) -> None:
    client = TestClient(app)
    room_id, _, secret = _create(client)

    with client.websocket_connect(f"/ws/{room_id}") as viewer:
        _spectate(viewer, invite_secret=secret)
        assert _first(viewer)["type"] == "member_token"
        assert viewer.receive_json()["type"] == "room_settings"

    room = relay.get(room_id)
    assert room is not None
    assert [(m.name, m.kind) for m in room.remembered_members()] == [
        (SPECTATOR_DISPLAY_NAME, "dashboard")
    ]


@pytest.mark.parametrize("spectator", [False, True], ids=["member", "dashboard"])
def test_the_code_alone_waits_for_an_owner(relay: RoomManager, spectator: bool) -> None:
    client = TestClient(app)
    room_id, token, _ = _create(client)

    with client.websocket_connect(f"/ws/{room_id}") as owner:
        _spectate(owner, owner_token=token)
        with client.websocket_connect(f"/ws/{room_id}") as guest:
            if spectator:
                _spectate(guest)
            else:
                _join(guest, "guesser")
            pending = _first(guest)
            assert pending["type"] == "join_pending"
            assert "invite link" in pending["reason"]
            request = _next(owner, "join_request")
            assert request["request_id"] == pending["request_id"]


def test_a_wrong_secret_is_indistinguishable_from_none(relay: RoomManager) -> None:
    client = TestClient(app)
    room_id, _, secret = _create(client)
    answers = []
    for presented in (None, "wrong-" + secret, secret[:-1]):
        with client.websocket_connect(f"/ws/{room_id}") as guest:
            _join(guest, f"g{len(answers)}", invite_secret=presented)
            frame = _first(guest)
            answers.append((frame["type"], frame["reason"]))

    assert len(set(answers)) == 1
    assert answers[0][0] == "join_pending"


def test_an_approved_member_is_remembered_and_not_asked_again(
    relay: RoomManager,
) -> None:
    client = TestClient(app)
    room_id, token, _ = _create(client)

    with client.websocket_connect(f"/ws/{room_id}") as owner:
        _spectate(owner, owner_token=token)
        with client.websocket_connect(f"/ws/{room_id}") as sam:
            _join(sam, "sam")
            assert _first(sam)["type"] == "join_pending"
            request_id = _approve_next(owner)
            assert sam.receive_json() == {
                "type": "join_resolved",
                "request_id": request_id,
                "outcome": "approved",
            }
            issued = sam.receive_json()
            assert issued["type"] == "member_token"
            assert _admitted(sam)
            access = _next(owner, "room_access")
            assert [(m["name"], m["online"]) for m in access["members"]] == [("sam", True)]

        # Reconnect (laptop sleep, relay redeploy): straight in, no new token.
        with client.websocket_connect(f"/ws/{room_id}") as sam:
            _join(sam, "sam", member_token=issued["token"])
            assert _admitted(sam)
        assert relay.get(room_id).pending_count == 0  # type: ignore[union-attr]


def test_the_owner_token_admits_without_a_member_token(relay: RoomManager) -> None:
    client = TestClient(app)
    room_id, token, _ = _create(client)

    with client.websocket_connect(f"/ws/{room_id}") as owner:
        _join(owner, "olive", owner_token=token)
        types = []
        for event in _frames(owner):
            types.append(event["type"])
            if event["type"] == "room_access":
                break
        assert "member_token" not in types and "join_pending" not in types

    room = relay.get(room_id)
    assert room is not None and room.remembered_members() == []


def test_owners_see_the_invite_secret_in_their_catch_up_and_nobody_else(
    relay: RoomManager,
) -> None:
    client = TestClient(app)
    room_id, token, secret = _create(client)

    with client.websocket_connect(f"/ws/{room_id}") as owner:
        _spectate(owner, owner_token=token)
        access = _next(owner, "room_access")
        assert access["invite_required"] is True
        assert access["invite_secret"] == secret

        with client.websocket_connect(f"/ws/{room_id}") as viewer:
            _spectate(viewer, invite_secret=secret)
            # Its whole catch-up: the token, the settings, the policy.
            seen = [viewer.receive_json()["type"] for _ in range(3)]
            assert seen == ["member_token", "room_settings", "room_policy_update"]
            # The owner hears about the newcomer; the newcomer hears nothing
            # of access (a member's own reply to a probe comes first).
            assert _next(owner, "room_access")["members"][0]["online"] is True


def test_rotation_makes_old_links_knock(relay: RoomManager) -> None:
    client = TestClient(app)
    room_id, token, old = _create(client)

    with client.websocket_connect(f"/ws/{room_id}") as owner:
        _spectate(owner, owner_token=token)
        assert _next(owner, "room_access")["invite_secret"] == old
        owner.send_json({"type": "invite_rotate"})
        new = _next(owner, "room_access")["invite_secret"]
        assert new and new != old

        with client.websocket_connect(f"/ws/{room_id}") as stale:
            _join(stale, "stale", invite_secret=old)
            assert _first(stale)["type"] == "join_pending"
        with client.websocket_connect(f"/ws/{room_id}") as fresh:
            _join(fresh, "fresh", invite_secret=new)
            assert _first(fresh)["type"] == "member_token"

    assert relay._store.get_access(room_id)["invite_hash"] == hash_owner_token(new)


def test_rotation_keeps_remembered_members(relay: RoomManager) -> None:
    client = TestClient(app)
    room_id, token, secret = _create(client)
    with client.websocket_connect(f"/ws/{room_id}") as sam:
        _join(sam, "sam", invite_secret=secret)
        issued = _first(sam)

    with client.websocket_connect(f"/ws/{room_id}") as owner:
        _spectate(owner, owner_token=token)
        owner.send_json({"type": "invite_rotate"})
        _next(owner, "room_access")
        _next(owner, "room_access")

    with client.websocket_connect(f"/ws/{room_id}") as sam:
        _join(sam, "sam", member_token=issued["token"], invite_secret=secret)
        assert _admitted(sam)


def test_revoking_a_member_kicks_it_and_its_token_stops_working(
    relay: RoomManager,
) -> None:
    client = TestClient(app)
    room_id, token, secret = _create(client)

    with client.websocket_connect(f"/ws/{room_id}") as owner:
        _spectate(owner, owner_token=token)
        with client.websocket_connect(f"/ws/{room_id}") as sam:
            _join(sam, "sam", invite_secret=secret)
            issued = _first(sam)
            assert _admitted(sam)
            owner.send_json({"type": "member_revoke", "member_id": issued["member_id"]})
            assert _next(sam, "room_rejected")["reason"] == REASON_MEMBER_REVOKED
            with pytest.raises(WebSocketDisconnect) as closed:
                for _ in range(MAX_FRAMES):
                    sam.receive_json()
            assert closed.value.code == WS_CLOSE_JOIN_REFUSED
        for event in _frames(owner):
            if event["type"] == "room_access" and not event["members"]:
                break

        # The token is gone: with it alone, sam only knocks now.
        with client.websocket_connect(f"/ws/{room_id}") as sam:
            _join(sam, "sam", member_token=issued["token"])
            assert _first(sam)["type"] == "join_pending"

    assert "members" not in relay._store.get_access(room_id)


def test_non_owners_cannot_rotate_or_revoke(relay: RoomManager) -> None:
    client = TestClient(app)
    room_id, _, secret = _create(client)

    with client.websocket_connect(f"/ws/{room_id}") as sam:
        _join(sam, "sam", invite_secret=secret)
        issued = _first(sam)
        sam.send_json({"type": "invite_rotate"})
        sam.send_json({"type": "member_revoke", "member_id": issued["member_id"]})
        assert _admitted(sam)

    room = relay.get(room_id)
    assert room is not None
    assert room.verify_invite_secret(secret)
    assert room.verify_member_token(issued["token"]) == issued["member_id"]


def test_require_approval_holds_link_holders_but_not_remembered_members(
    relay: RoomManager,
) -> None:
    client = TestClient(app)
    room_id, token, secret = _create(client)
    with client.websocket_connect(f"/ws/{room_id}") as sam:
        _join(sam, "sam", invite_secret=secret)
        issued = _first(sam)

    with client.websocket_connect(f"/ws/{room_id}") as owner:
        _spectate(owner, owner_token=token)
        owner.send_json({"type": "room_policy", "require_approval": True})
        _next(owner, "room_policy_update")
        _next(owner, "room_policy_update")
        with client.websocket_connect(f"/ws/{room_id}") as kim:
            _join(kim, "kim", invite_secret=secret)
            assert _first(kim)["type"] == "join_pending"
        with client.websocket_connect(f"/ws/{room_id}") as sam:
            _join(sam, "sam", member_token=issued["token"])
            assert _admitted(sam)


def test_a_repo_bound_room_admits_the_invite_link(relay: RoomManager) -> None:
    client = TestClient(app)
    body = client.post("/rooms", json={"repo_key": "c" * 64}).json()
    room_id, secret = body["room_id"], body["invite_secret"]

    with client.websocket_connect(f"/ws/{room_id}") as sam:
        _join(sam, "sam", invite_secret=secret, via_repo_lookup=None)
        assert _first(sam)["type"] == "member_token"
    with client.websocket_connect(f"/ws/{room_id}") as guess:
        _join(guess, "guess")
        assert _first(guess)["type"] == "join_pending"


def test_a_legacy_room_admits_the_code_alone_and_issues_nothing(
    relay: RoomManager,
) -> None:
    """A room from before invite links (owner token, no invite secret)."""
    client = TestClient(app)
    room_id = relay.create_room()
    room = relay.get(room_id)
    assert room is not None
    room.set_owner_hash(hash_owner_token(new_owner_token()))

    with client.websocket_connect(f"/ws/{room_id}") as sam:
        _join(sam, "sam")
        types = []
        sam.send_json({"type": "edit_check", "request_id": "probe", "file_path": "x.py"})
        for event in _frames(sam):
            types.append(event["type"])
            if event["type"] == "edit_check_response":
                break
        assert "join_pending" not in types and "member_token" not in types
    assert room.remembered_members() == []


def test_rotating_a_legacy_room_turns_invite_links_on(relay: RoomManager) -> None:
    client = TestClient(app)
    room_id = relay.create_room()
    room = relay.get(room_id)
    assert room is not None
    token = new_owner_token()
    room.set_owner_hash(hash_owner_token(token))

    with client.websocket_connect(f"/ws/{room_id}") as owner:
        _spectate(owner, owner_token=token)
        first = _next(owner, "room_access")
        assert first["invite_required"] is False and first["invite_secret"] is None
        owner.send_json({"type": "invite_rotate"})
        after = _next(owner, "room_access")
        assert after["invite_required"] is True and after["invite_secret"]
        with client.websocket_connect(f"/ws/{room_id}") as guess:
            _join(guess, "guess")
            assert _first(guess)["type"] == "join_pending"


# --- HTTP -------------------------------------------------------------------------


def test_the_owner_reads_and_rotates_the_invite_over_http(relay: RoomManager) -> None:
    client = TestClient(app)
    room_id, token, secret = _create(client)
    headers = {"X-Hivelane-Owner-Token": token}

    read = client.get(f"/rooms/{room_id}/invite", headers=headers)
    assert read.status_code == 200
    assert read.json() == {"room_id": room_id, "invite_required": True, "invite_secret": secret}

    rotated = client.post(f"/rooms/{room_id}/invite/rotate", headers=headers)
    assert rotated.status_code == 200
    new = rotated.json()["invite_secret"]
    assert new and new != secret
    assert client.get(f"/rooms/{room_id}/invite", headers=headers).json()["invite_secret"] == new
    room = relay.get(room_id)
    assert room is not None and not room.verify_invite_secret(secret)


def test_a_wrong_owner_token_looks_exactly_like_an_unknown_room(
    relay: RoomManager,
) -> None:
    client = TestClient(app)
    room_id, token, _ = _create(client)

    answers = [
        client.get(f"/rooms/{room_id}/invite", headers={"X-Hivelane-Owner-Token": "nope"}),
        client.get(f"/rooms/{room_id}/invite"),
        client.get("/rooms/ZZZZZZZZ/invite", headers={"X-Hivelane-Owner-Token": token}),
        client.post(f"/rooms/{room_id}/invite/rotate", headers={"X-Hivelane-Owner-Token": "nope"}),
        client.post("/rooms/ZZZZZZZZ/invite/rotate", headers={"X-Hivelane-Owner-Token": token}),
    ]

    assert {(a.status_code, a.text) for a in answers} == {
        (404, '{"detail":"Room not found"}')
    }
