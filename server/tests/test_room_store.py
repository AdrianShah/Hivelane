"""Room-code persistence: a deploy must not invalidate rooms in use."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

try:
    from server.room_manager import MAX_ROOMS, RoomManager
    from server.store import (
        ROOM_TTL_SECONDS,
        STATE_PATH_ENV,
        MemoryRoomStore,
        RoomStore,
        SqliteRoomStore,
        build_room_store,
    )
except ImportError:  # Render rootDir=server
    from room_manager import MAX_ROOMS, RoomManager
    from store import (
        ROOM_TTL_SECONDS,
        STATE_PATH_ENV,
        MemoryRoomStore,
        RoomStore,
        SqliteRoomStore,
        build_room_store,
    )


@pytest.fixture
def state_path(tmp_path: Path) -> str:
    return str(tmp_path / "state" / "synergo.db")


def _assert_usable(manager: RoomManager, code: str) -> None:
    room = manager.get(code)
    assert room is not None
    assert room.is_empty
    assert room.check_edit("alice", "server/room.py", time.time()) == ("allow", [])


def test_code_survives_restart_over_the_same_store(state_path: str) -> None:
    store = SqliteRoomStore(state_path)
    code = RoomManager(store=store).create_room()

    restarted = RoomManager(store=store)

    _assert_usable(restarted, code)
    assert restarted.get("NOPE99") is None


def test_code_survives_reopening_the_database_file(state_path: str) -> None:
    code = RoomManager(store=SqliteRoomStore(state_path)).create_room()

    restarted = RoomManager(store=SqliteRoomStore(state_path))

    _assert_usable(restarted, code)
    assert restarted.room_count == 1
    assert restarted.durable_room_count == 1


def test_revived_room_is_returned_for_a_lowercase_padded_code(state_path: str) -> None:
    store = SqliteRoomStore(state_path)
    code = RoomManager(store=store).create_room()

    assert RoomManager(store=store).get(f"  {code.lower()} ") is not None


def test_cleanup_if_empty_stops_the_code_resolving_after_a_restart(
    state_path: str,
) -> None:
    store = SqliteRoomStore(state_path)
    manager = RoomManager(store=store)
    code = manager.create_room()

    manager.cleanup_if_empty(code)

    assert manager.get(code) is None
    assert store.exists(code) is False
    assert RoomManager(store=SqliteRoomStore(state_path)).get(code) is None


def test_expired_codes_do_not_resolve_and_are_purged(state_path: str) -> None:
    store = SqliteRoomStore(state_path)
    store.add("OLDCOD", time.time() - ROOM_TTL_SECONDS - 1)
    store.add("NEWCOD", time.time())

    assert store.exists("OLDCOD") is False
    assert RoomManager(store=store).get("OLDCOD") is None
    assert store.count() == 1

    assert store.purge_expired(time.time()) == 1
    assert store.purge_expired(time.time()) == 0
    assert store.exists("NEWCOD") is True


def test_memory_store_keeps_todays_behaviour() -> None:
    manager = RoomManager(store=MemoryRoomStore())
    code = manager.create_room()

    assert manager.get(code) is not None
    assert manager.get("NOPE99") is None
    assert manager.room_count == 1
    # Nothing is shared with a second manager: memory rooms die with the process.
    assert RoomManager(store=MemoryRoomStore()).get(code) is None


def test_memory_store_expires_codes() -> None:
    store = MemoryRoomStore()
    store.add("OLDCOD", time.time() - ROOM_TTL_SECONDS - 1)

    assert store.exists("OLDCOD") is False
    assert store.count() == 0
    assert store.purge_expired(time.time()) == 1


def test_build_room_store_uses_sqlite_when_the_state_path_is_set(
    monkeypatch: pytest.MonkeyPatch, state_path: str
) -> None:
    monkeypatch.setenv(STATE_PATH_ENV, state_path)

    store = build_room_store()

    assert isinstance(store, SqliteRoomStore)
    assert Path(state_path).exists()


def test_build_room_store_falls_back_to_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(STATE_PATH_ENV, raising=False)

    assert isinstance(build_room_store(), MemoryRoomStore)


def test_build_room_store_falls_back_when_the_path_is_unusable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    monkeypatch.setenv(STATE_PATH_ENV, str(blocker / "synergo.db"))

    assert isinstance(build_room_store(), MemoryRoomStore)


def test_max_rooms_is_enforced_against_the_durable_count(state_path: str) -> None:
    store: RoomStore = SqliteRoomStore(state_path)
    now = time.time()
    for index in range(MAX_ROOMS):
        store.add(f"CODE{index:02d}", now)

    # No live rooms at all, yet the cap still applies.
    manager = RoomManager(store=store)
    with pytest.raises(RuntimeError, match="Maximum number of rooms"):
        manager.create_room()
    assert manager.room_count == 0

    store.remove("CODE00")
    assert manager.create_room()


def test_expired_codes_free_up_capacity(state_path: str) -> None:
    store = SqliteRoomStore(state_path)
    for index in range(MAX_ROOMS):
        store.add(f"CODE{index:02d}", time.time() - ROOM_TTL_SECONDS - 1)

    assert RoomManager(store=store).create_room()
    assert store.count() == 1
