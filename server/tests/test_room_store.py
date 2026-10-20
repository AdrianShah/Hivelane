"""Room-code persistence: a deploy must not invalidate rooms in use."""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

import pytest

try:
    from server.models import RoomSettings
    from server.room import DEFAULT_SETTINGS
    from server import room_manager as room_manager_module
    from server.room_manager import (
        CODE_ALPHABET,
        CODE_LENGTH,
        RoomManager,
    )
    from server.store import (
        ROOM_TTL_SECONDS,
        STATE_PATH_ENV,
        MemoryRoomStore,
        SqliteRoomStore,
        build_room_store,
    )
except ImportError:  # Render rootDir=server
    from models import RoomSettings
    from room import DEFAULT_SETTINGS
    import room_manager as room_manager_module  # type: ignore[no-redef]
    from room_manager import (
        CODE_ALPHABET,
        CODE_LENGTH,
        RoomManager,
    )
    from store import (
        ROOM_TTL_SECONDS,
        STATE_PATH_ENV,
        MemoryRoomStore,
        SqliteRoomStore,
        build_room_store,
    )


# A relay that sets HIVELANE_MAX_ROOMS (the default, 0, is unlimited).
MAX_ROOMS = 100


@pytest.fixture
def state_path(tmp_path: Path) -> str:
    return str(tmp_path / "state" / "hivelane.db")


@pytest.fixture
def room_cap(monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setattr(room_manager_module, "MAX_ROOMS", MAX_ROOMS)
    return MAX_ROOMS


def _assert_usable(manager: RoomManager, code: str) -> None:
    room = manager.get(code)
    assert room is not None
    assert room.is_empty
    assert room.check_edit("alice", "server/room.py", time.time()) == ("allow", [])


def test_code_survives_reopening_the_database_file(state_path: str) -> None:
    code = RoomManager(store=SqliteRoomStore(state_path)).create_room()

    restarted = RoomManager(store=SqliteRoomStore(state_path))

    _assert_usable(restarted, code)
    assert restarted.get("NOPE99") is None
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


def test_memory_store_keeps_todays_behaviour() -> None:
    manager = RoomManager(store=MemoryRoomStore())
    code = manager.create_room()

    assert manager.get(code) is not None
    assert manager.get("NOPE99") is None
    assert manager.room_count == 1
    # Nothing is shared with a second manager: memory rooms die with the process.
    assert RoomManager(store=MemoryRoomStore()).get(code) is None


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
    monkeypatch.setenv(STATE_PATH_ENV, str(blocker / "hivelane.db"))

    assert isinstance(build_room_store(), MemoryRoomStore)


def test_rooms_are_unlimited_by_default(state_path: str) -> None:
    assert room_manager_module.MAX_ROOMS == 0
    store = SqliteRoomStore(state_path)
    now = time.time()
    for index in range(MAX_ROOMS):
        store.add(f"CODE{index:02d}", now)

    manager = RoomManager(store=store)
    assert manager.create_room()
    assert store.count() == MAX_ROOMS + 1


def test_expired_codes_free_up_capacity(state_path: str, room_cap: int) -> None:
    store = SqliteRoomStore(state_path)
    for index in range(MAX_ROOMS):
        store.add(f"CODE{index:02d}", time.time() - ROOM_TTL_SECONDS - 1)

    assert RoomManager(store=store).create_room()
    assert store.count() == 1


# --- code length ----------------------------------------------------------------


def test_new_codes_are_eight_characters_from_the_readable_alphabet() -> None:
    manager = RoomManager(store=MemoryRoomStore())

    codes = [manager.create_room() for _ in range(20)]

    assert CODE_LENGTH == 8
    for code in codes:
        assert len(code) == 8
        assert set(code) <= set(CODE_ALPHABET)


def test_legacy_six_character_codes_still_resolve(state_path: str) -> None:
    """Codes issued before the switch stay in the store for up to a week."""
    store = SqliteRoomStore(state_path)
    store.add("K7M2PQ", time.time())

    manager = RoomManager(store=store)

    assert manager.get("K7M2PQ") is not None
    assert manager.get(" k7m2pq ") is manager.get("K7M2PQ")


@pytest.mark.parametrize("typed", ["abcd-efgh", " ABCD EFGH ", "abcdefgh", "AB-CD-EF-GH"])
def test_codes_are_normalized_before_lookup(typed: str) -> None:
    store = MemoryRoomStore()
    store.add("ABCDEFGH", time.time())

    assert RoomManager(store=store).get(typed) is not None


class _NoLookups(MemoryRoomStore):
    def exists(self, room_id: str) -> bool:
        raise AssertionError(f"looked up implausible code {room_id!r}")


@pytest.mark.parametrize("code", ["", "ABCDE", "ABCDEFGHJ", "ABC/DEFG", "../../etc"])
def test_implausible_codes_are_refused_without_a_store_lookup(code: str) -> None:
    assert RoomManager(store=_NoLookups()).get(code) is None


# --- settings persistence --------------------------------------------------------

CUSTOM = RoomSettings(block_window_seconds=5.0, warn_window_seconds=30.0)


def test_room_created_with_settings_keeps_them_after_a_restart(state_path: str) -> None:
    code = RoomManager(store=SqliteRoomStore(state_path)).create_room(CUSTOM)

    revived = RoomManager(store=SqliteRoomStore(state_path)).get(code)

    assert revived is not None
    assert revived.settings == CUSTOM


def test_saved_settings_survive_a_restart(state_path: str) -> None:
    manager = RoomManager(store=SqliteRoomStore(state_path))
    code = manager.create_room()
    room = manager.get(code)
    assert room is not None
    assert room.settings == DEFAULT_SETTINGS

    assert room.apply_settings(CUSTOM, set_by="sam") is not None
    manager.save_settings(code.lower(), room.settings)

    revived = RoomManager(store=SqliteRoomStore(state_path)).get(code)
    assert revived is not None
    assert revived.settings == CUSTOM


def test_create_room_refuses_unenforceable_settings() -> None:
    manager = RoomManager(store=MemoryRoomStore())

    with pytest.raises(ValueError, match="shorter than"):
        manager.create_room(
            RoomSettings(block_window_seconds=50.0, warn_window_seconds=10.0)
        )
    assert manager.durable_room_count == 0


@pytest.mark.parametrize(
    "stored",
    [
        {"block_window_seconds": 50.0, "warn_window_seconds": 10.0},
        {"block_window_seconds": "soon", "warn_window_seconds": 10.0},
        {"block_window_seconds": -1.0, "warn_window_seconds": 10.0},
    ],
)
def test_invalid_stored_settings_revive_on_the_defaults(
    state_path: str, stored: dict
) -> None:
    store = SqliteRoomStore(state_path)
    store.add("ABCDEFGH", time.time(), stored)

    room = RoomManager(store=store).get("ABCDEFGH")

    assert room is not None
    assert room.settings == DEFAULT_SETTINGS


def test_an_old_registry_file_is_migrated_in_place(state_path: str) -> None:
    """A file written before settings existed has no settings column."""
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    old = sqlite3.connect(state_path)
    old.execute(
        "CREATE TABLE rooms (room_id TEXT PRIMARY KEY, created_at REAL NOT NULL)"
    )
    old.execute("INSERT INTO rooms VALUES (?, ?)", ("K7M2PQ", time.time()))
    old.commit()
    old.close()

    store = SqliteRoomStore(state_path)

    probe = sqlite3.connect(state_path)
    columns = {row[1] for row in probe.execute("PRAGMA table_info(rooms)")}
    probe.close()
    assert "settings" in columns
    assert store.exists("K7M2PQ")
    assert store.get_settings("K7M2PQ") is None
    room = RoomManager(store=store).get("K7M2PQ")
    assert room is not None
    assert room.settings == DEFAULT_SETTINGS

    store.set_settings("K7M2PQ", CUSTOM.model_dump())
    # Reopening runs the migration again; it must be a no-op now.
    reopened = SqliteRoomStore(state_path)
    revived = RoomManager(store=reopened).get("K7M2PQ")
    assert revived is not None
    assert revived.settings == CUSTOM
