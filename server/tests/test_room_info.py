"""GET /rooms/{code}: is a room I own still alive, and when does it expire?

The dashboard's "Your rooms" list asks this for each owner token it holds.
Every failure must look the same (404), so the endpoint is no oracle for
which codes exist, and asking must never revive a room."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

try:
    from server import limits, main
    from server.room_manager import RoomManager
    from server.store import ROOM_TTL_SECONDS, MemoryRoomStore, SqliteRoomStore
except ImportError:  # Render rootDir=server
    import limits  # type: ignore[no-redef]
    import main  # type: ignore[no-redef]
    from room_manager import RoomManager  # type: ignore[no-redef]
    from store import ROOM_TTL_SECONDS, MemoryRoomStore, SqliteRoomStore  # type: ignore[no-redef]

HEADER = "X-Hivelane-Owner-Token"


@pytest.fixture
def relay(monkeypatch: pytest.MonkeyPatch) -> Iterator[RoomManager]:
    manager = RoomManager(store=MemoryRoomStore())
    monkeypatch.setattr(main, "room_manager", manager)
    yield manager


def _create(client: TestClient) -> tuple[str, str]:
    body = client.post("/rooms").json()
    return body["room_id"], body["owner_token"]


def test_owner_sees_alive_and_expiry(relay: RoomManager) -> None:
    client = TestClient(main.app)
    room_id, token = _create(client)
    response = client.get(f"/rooms/{room_id}", headers={HEADER: token})
    assert response.status_code == 200
    body = response.json()
    assert body["alive"] is True
    room = relay.live(room_id)
    assert room is not None
    assert body["expires_at"] == pytest.approx(room.created_at + ROOM_TTL_SECONDS)
    # Only these two fields: nothing about who is in the room.
    assert set(body) == {"alive", "expires_at"}


def test_unknown_dead_and_unowned_rooms_look_the_same(relay: RoomManager) -> None:
    client = TestClient(main.app)
    room_id, token = _create(client)
    other_id, _ = _create(client)
    answers = [
        client.get(f"/rooms/{room_id}"),  # no token
        client.get(f"/rooms/{room_id}", headers={HEADER: "wrong"}),
        client.get(f"/rooms/{other_id}", headers={HEADER: token}),  # someone else's
        client.get("/rooms/NOPE9999", headers={HEADER: token}),  # unknown
        client.get("/rooms/" + "x" * 200, headers={HEADER: token}),  # garbage
    ]
    relay.cleanup_if_empty(room_id)  # finished: the code is gone
    answers.append(client.get(f"/rooms/{room_id}", headers={HEADER: token}))
    assert {a.status_code for a in answers} == {404}
    assert len({a.text for a in answers}) == 1
    # Marked, so a client can tell it from the bare 404 of a relay without this route.
    assert answers[0].json()["alive"] is False


def test_asking_does_not_revive_a_room(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    db = str(tmp_path / "relay.db")
    monkeypatch.setattr(main, "room_manager", RoomManager(store=SqliteRoomStore(db)))
    client = TestClient(main.app)
    room_id, token = _create(client)
    restarted = RoomManager(store=SqliteRoomStore(db))
    monkeypatch.setattr(main, "room_manager", restarted)
    response = client.get(f"/rooms/{room_id}", headers={HEADER: token})
    assert response.status_code == 200 and response.json()["expires_at"] > 0
    assert restarted.live(room_id) is None


def test_room_info_is_rate_limited(relay: RoomManager, monkeypatch: pytest.MonkeyPatch) -> None:
    client = TestClient(main.app)
    room_id, token = _create(client)
    monkeypatch.setattr(
        limits,
        "repo_lookup_limiter",
        limits.FixedWindowLimiter(limit=1, window_seconds=60, max_keys=16),
    )
    assert client.get(f"/rooms/{room_id}", headers={HEADER: token}).status_code == 200
    limited = client.get(f"/rooms/{room_id}", headers={HEADER: token})
    assert limited.status_code == 429
    assert limited.headers.get("Retry-After")
