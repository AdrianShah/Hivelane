"""The Redis room store: the same contract as the SQLite and memory stores (run
against all three here), codes shared between instances, and degrading to
this instance's own copy while Redis is down."""

from __future__ import annotations

import logging
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

try:
    from server.models import RoomSettings
    from server import room_manager as room_manager_module
    from server.room_manager import RoomManager
    from server.store import (
        REDIS_KEY_PREFIX,
        ROOM_STORE_ENV,
        ROOM_TTL_SECONDS,
        STATE_PATH_ENV,
        MemoryRoomStore,
        RedisRoomStore,
        RoomStore,
        SqliteRoomStore,
        build_room_store,
    )
    from server.tests.fake_redis import Clock, FakeRedis
except ImportError:  # Render rootDir=server
    from models import RoomSettings
    import room_manager as room_manager_module  # type: ignore[no-redef]
    from room_manager import RoomManager
    from store import (
        REDIS_KEY_PREFIX,
        ROOM_STORE_ENV,
        ROOM_TTL_SECONDS,
        STATE_PATH_ENV,
        MemoryRoomStore,
        RedisRoomStore,
        RoomStore,
        SqliteRoomStore,
        build_room_store,
    )
    from tests.fake_redis import Clock, FakeRedis

CUSTOM = RoomSettings(block_window_seconds=5.0, warn_window_seconds=30.0)
# A relay that sets SYNERGO_MAX_ROOMS (the default, 0, is unlimited).
MAX_ROOMS = 100
ACCESS = {"owner_hash": "ab" * 32, "require_approval": True}

StoreFactory = Callable[[], RoomStore]


@pytest.fixture(params=["memory", "sqlite", "redis"])
def make_store(request: pytest.FixtureRequest, tmp_path: Path) -> StoreFactory:
    """Builds stores of one kind that all see the same state (for sqlite and
    redis), the way a restarted or second instance would."""
    if request.param == "memory":
        shared = MemoryRoomStore()
        return lambda: shared
    if request.param == "sqlite":
        path = str(tmp_path / "state" / "synergo.db")
        return lambda: SqliteRoomStore(path)
    redis = FakeRedis()
    return lambda: RedisRoomStore(redis)


# --- the shared contract --------------------------------------------------------------


def test_codes_exist_until_removed(make_store: StoreFactory) -> None:
    store = make_store()
    store.add("ABCDEFGH", time.time())

    assert store.exists("ABCDEFGH")
    assert not store.exists("ZZZZZZZZ")
    assert store.count() == 1

    store.remove("ABCDEFGH")
    assert not store.exists("ABCDEFGH")
    assert store.count() == 0


def test_expired_codes_do_not_resolve_and_are_purged(make_store: StoreFactory) -> None:
    store = make_store()
    store.add("OLDCOD", time.time() - ROOM_TTL_SECONDS - 1)
    store.add("NEWCOD", time.time())

    assert store.exists("OLDCOD") is False
    assert store.count() == 1

    assert store.purge_expired(time.time()) == 1
    assert store.purge_expired(time.time()) == 0
    assert store.exists("NEWCOD") is True


def test_settings_go_with_the_code(make_store: StoreFactory) -> None:
    store = make_store()
    store.add("ABCDEFGH", time.time(), CUSTOM.model_dump())
    assert store.get_settings("ABCDEFGH") == CUSTOM.model_dump()

    # Unknown codes have no settings and cannot be given any.
    store.set_settings("ZZZZZZZZ", CUSTOM.model_dump())
    assert store.get_settings("ZZZZZZZZ") is None

    store.remove("ABCDEFGH")
    assert store.get_settings("ABCDEFGH") is None


def test_settings_can_be_replaced(make_store: StoreFactory) -> None:
    store = make_store()
    store.add("ABCDEFGH", time.time())
    assert store.get_settings("ABCDEFGH") is None

    store.set_settings("ABCDEFGH", CUSTOM.model_dump())

    assert make_store().get_settings("ABCDEFGH") == CUSTOM.model_dump()


def test_access_goes_with_the_code(make_store: StoreFactory) -> None:
    store = make_store()
    store.add("ABCDEFGH", time.time())
    assert store.get_access("ABCDEFGH") is None

    store.set_access("ABCDEFGH", ACCESS)
    store.set_access("ZZZZZZZZ", ACCESS)

    assert make_store().get_access("ABCDEFGH") == ACCESS
    assert store.get_access("ZZZZZZZZ") is None
    store.remove("ABCDEFGH")
    assert store.get_access("ABCDEFGH") is None


def test_purging_drops_the_records_of_expired_codes(make_store: StoreFactory) -> None:
    store = make_store()
    store.add("OLDCODE1", time.time() - ROOM_TTL_SECONDS - 1, CUSTOM.model_dump())
    store.set_access("OLDCODE1", ACCESS)

    store.purge_expired(time.time())

    assert store.get_settings("OLDCODE1") is None
    assert store.get_access("OLDCODE1") is None


def test_codes_survive_a_restart_over_the_same_store(make_store: StoreFactory) -> None:
    code = RoomManager(store=make_store()).create_room(CUSTOM)

    revived = RoomManager(store=make_store()).get(code)

    assert revived is not None
    assert revived.settings == CUSTOM


def test_max_rooms_is_enforced_against_the_durable_count(
    make_store: StoreFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(room_manager_module, "MAX_ROOMS", MAX_ROOMS)
    store = make_store()
    now = time.time()
    for index in range(MAX_ROOMS):
        store.add(f"CODE{index:04d}", now)

    manager = RoomManager(store=make_store())
    with pytest.raises(RuntimeError, match="Maximum number of rooms"):
        manager.create_room()

    store.remove("CODE0000")
    assert manager.create_room()


# --- Redis specifics --------------------------------------------------------------------


def test_a_code_created_on_one_instance_is_valid_on_another() -> None:
    redis = FakeRedis()
    a = RoomManager(store=RedisRoomStore(redis))
    b = RoomManager(store=RedisRoomStore(redis))
    code = a.create_room()
    room = a.get(code)
    assert room is not None and room.apply_settings(CUSTOM, set_by="sam") is not None
    a.save_settings(code, room.settings)

    revived = b.get(code)

    assert revived is not None
    assert revived.settings == CUSTOM
    assert b.durable_room_count == 1


def test_redis_layout_is_one_sorted_set_and_a_hash_per_record() -> None:
    redis = FakeRedis()
    store = RedisRoomStore(redis)

    store.add("ABCDEFGH", 1234.5, CUSTOM.model_dump())
    store.set_access("ABCDEFGH", ACCESS)

    assert redis.zsets[REDIS_KEY_PREFIX] == {"ABCDEFGH": 1234.5}
    assert set(redis.hashes) == {f"{REDIS_KEY_PREFIX}:settings", f"{REDIS_KEY_PREFIX}:access"}


def test_an_outage_falls_back_to_the_codes_this_instance_has_seen(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = Clock()
    redis = FakeRedis(clock)
    here = RedisRoomStore(redis, clock=clock)
    elsewhere = RedisRoomStore(redis, clock=clock)
    here.add("MINE1234", time.time(), CUSTOM.model_dump())
    elsewhere.add("THEIRS12", time.time())
    redis.down = True

    with caplog.at_level(logging.WARNING):
        assert here.exists("MINE1234")
        assert here.get_settings("MINE1234") == CUSTOM.model_dump()
        assert not here.exists("THEIRS12")
        here.add("DURING12", time.time())  # never raises
        assert here.exists("DURING12")
    assert sum("room store unreachable" in r.getMessage() for r in caplog.records) == 1
    assert here.available is False

    redis.down = False
    clock.advance(31)
    assert here.exists("THEIRS12")
    assert here.available is True


# --- choosing a store --------------------------------------------------------------------


def _store_module() -> object:
    return sys.modules[build_room_store.__module__]


@pytest.fixture
def fake_connect(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    redis = FakeRedis()
    monkeypatch.setattr(_store_module(), "connect_redis", lambda url, purpose: redis)
    monkeypatch.setenv("SYNERGO_REDIS_URL", "redis://redis:6379/0")
    monkeypatch.delenv(ROOM_STORE_ENV, raising=False)
    monkeypatch.delenv(STATE_PATH_ENV, raising=False)
    return redis


def test_redis_url_selects_the_redis_store(fake_connect: FakeRedis) -> None:
    store = build_room_store()

    assert isinstance(store, RedisRoomStore)
    assert store.client is fake_connect
    assert isinstance(store.local, MemoryRoomStore)


def test_the_state_path_becomes_the_local_copy(
    fake_connect: FakeRedis, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(STATE_PATH_ENV, str(tmp_path / "synergo.db"))

    store = build_room_store()

    assert isinstance(store, RedisRoomStore)
    assert isinstance(store.local, SqliteRoomStore)


def test_room_store_local_keeps_codes_off_redis(
    fake_connect: FakeRedis, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(ROOM_STORE_ENV, "local")
    monkeypatch.setenv(STATE_PATH_ENV, str(tmp_path / "synergo.db"))

    assert isinstance(build_room_store(), SqliteRoomStore)


def test_an_unknown_room_store_value_is_ignored_with_a_warning(
    fake_connect: FakeRedis, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv(ROOM_STORE_ENV, "postgres")

    with caplog.at_level(logging.WARNING):
        store = build_room_store()

    assert isinstance(store, RedisRoomStore)
    assert "Ignoring SYNERGO_ROOM_STORE" in caplog.text


def test_without_the_redis_package_the_local_store_is_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_store_module(), "connect_redis", lambda url, purpose: None)
    monkeypatch.setenv("SYNERGO_REDIS_URL", "redis://redis:6379/0")
    monkeypatch.delenv(STATE_PATH_ENV, raising=False)

    assert isinstance(build_room_store(), MemoryRoomStore)
