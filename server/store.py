"""Durable registry of which room codes exist.

Only the codes (and each room's conflict-window settings) are persisted, never
room contents: in-room state is disposable (file touches expire inside the
room's warn window and are swept), so a restarted relay rebuilds coordination
state from live activity within a minute. Keeping the codes is what lets a
daemon rejoin the same room after a deploy; keeping the settings is what stops
that revived room from quietly falling back to the default windows.

Settings are stored as an opaque JSON object: the store does not interpret
them (room_manager validates them on the way back out), so adding a setting
needs no schema change. Access control (the SHA-256 of the room's owner token
and whether joins need an owner's approval; see Room.access_record) is a
second opaque object of the same kind. The owner token itself is never stored.

Stdlib only, so this works on any host with a writable disk and adds nothing to
operate. With several relay instances, SYNERGO_REDIS_URL moves the registry
into Redis (RedisRoomStore) so a code created on one instance is valid on all
of them; the `redis` package is imported only then.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from typing import Any, Callable, Optional, Protocol

try:
    from .directory import REDIS_URL_ENV, RedisGuard, connect_redis
except ImportError:  # Render rootDir=server
    if __package__:  # imported as server.*: a real missing dependency, not the flat tree
        raise
    from directory import REDIS_URL_ENV, RedisGuard, connect_redis  # type: ignore[no-redef]

log = logging.getLogger(__name__)

# A room code stops being valid after this long, so the table cannot grow
# forever. Long enough that a code shared on a Monday still works all week.
ROOM_TTL_SECONDS = 7 * 24 * 60 * 60.0

STATE_PATH_ENV = "SYNERGO_STATE_PATH"
# "local" keeps room codes on this instance (SQLite or memory) even when
# SYNERGO_REDIS_URL is set, e.g. when Redis is there for rate limits only.
ROOM_STORE_ENV = "SYNERGO_ROOM_STORE"
ROOM_STORE_CHOICES = ("redis", "local")
REDIS_KEY_PREFIX = "synergo:rooms"

# Plain JSON object; see the module docstring for why it stays untyped here.
StoredSettings = dict[str, Any]
# Same: {"owner_hash": str | None, "require_approval": bool}, read by Room.
StoredAccess = dict[str, Any]


class RoomStore(Protocol):
    """Set of currently valid room codes with their creation times and settings."""

    def add(
        self,
        room_id: str,
        created_at: float,
        settings: Optional[StoredSettings] = None,
    ) -> None: ...

    def exists(self, room_id: str) -> bool:
        """True if the code is known and not older than ROOM_TTL_SECONDS."""
        ...

    def get_settings(self, room_id: str) -> Optional[StoredSettings]:
        """The room's stored settings, or None if it has none (or is unknown)."""
        ...

    def set_settings(self, room_id: str, settings: StoredSettings) -> None:
        """Replace a known room's settings; a no-op for an unknown code."""
        ...

    def get_access(self, room_id: str) -> Optional[StoredAccess]:
        """The room's owner-token hash and join policy, or None if unset."""
        ...

    def set_access(self, room_id: str, access: StoredAccess) -> None:
        """Replace a known room's access record; a no-op for an unknown code."""
        ...

    def remove(self, room_id: str) -> None: ...

    def count(self) -> int:
        """Number of unexpired codes."""
        ...

    def purge_expired(self, now: float) -> int:
        """Drop codes older than the TTL. Returns how many were dropped."""
        ...


class MemoryRoomStore:
    """Default store: codes live as long as the process does."""

    def __init__(self) -> None:
        self._created_at: dict[str, float] = {}
        self._settings: dict[str, StoredSettings] = {}
        self._access: dict[str, StoredAccess] = {}

    def add(
        self,
        room_id: str,
        created_at: float,
        settings: Optional[StoredSettings] = None,
    ) -> None:
        self._created_at[room_id] = created_at
        if settings is None:
            self._settings.pop(room_id, None)
        else:
            self._settings[room_id] = dict(settings)

    def exists(self, room_id: str) -> bool:
        created_at = self._created_at.get(room_id)
        return created_at is not None and created_at > time.time() - ROOM_TTL_SECONDS

    def get_settings(self, room_id: str) -> Optional[StoredSettings]:
        settings = self._settings.get(room_id)
        return None if settings is None else dict(settings)

    def set_settings(self, room_id: str, settings: StoredSettings) -> None:
        if room_id in self._created_at:
            self._settings[room_id] = dict(settings)

    def get_access(self, room_id: str) -> Optional[StoredAccess]:
        access = self._access.get(room_id)
        return None if access is None else dict(access)

    def set_access(self, room_id: str, access: StoredAccess) -> None:
        if room_id in self._created_at:
            self._access[room_id] = dict(access)

    def remove(self, room_id: str) -> None:
        self._created_at.pop(room_id, None)
        self._settings.pop(room_id, None)
        self._access.pop(room_id, None)

    def count(self) -> int:
        cutoff = time.time() - ROOM_TTL_SECONDS
        return sum(1 for created_at in self._created_at.values() if created_at > cutoff)

    def purge_expired(self, now: float) -> int:
        cutoff = now - ROOM_TTL_SECONDS
        expired = [
            room_id
            for room_id, created_at in self._created_at.items()
            if created_at <= cutoff
        ]
        for room_id in expired:
            del self._created_at[room_id]
            self._settings.pop(room_id, None)
            self._access.pop(room_id, None)
        return len(expired)


class SqliteRoomStore:
    """Room codes in one sqlite file, so they outlive the process.

    Writes are tiny and rare (one per room created or removed) and every call
    comes from the event loop thread, so the connection is used directly rather
    than through a thread pool. A storage failure is logged and degrades to
    "code unknown" instead of raising: losing rejoin-after-restart is bad, but
    taking the relay down with it is worse.
    """

    def __init__(self, path: str) -> None:
        self._path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS rooms ("
            "  room_id TEXT PRIMARY KEY,"
            "  created_at REAL NOT NULL,"
            "  settings TEXT,"
            "  access TEXT"
            ")"
        )
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Bring a file written by an older relay up to the current schema.

        Files from before room settings have no settings column, and CREATE
        TABLE IF NOT EXISTS leaves them alone; their rooms read back as having
        no settings (the defaults), which is what they had."""
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(rooms)")}
        if "settings" not in columns:
            self._conn.execute("ALTER TABLE rooms ADD COLUMN settings TEXT")
            log.info("Migrated room registry at %s: added settings column", self._path)
        # Files from before owner tokens: their rooms read back with no owner
        # and no approval requirement, which is what they had.
        if "access" not in columns:
            self._conn.execute("ALTER TABLE rooms ADD COLUMN access TEXT")
            log.info("Migrated room registry at %s: added access column", self._path)

    def add(
        self,
        room_id: str,
        created_at: float,
        settings: Optional[StoredSettings] = None,
    ) -> None:
        encoded = None if settings is None else json.dumps(settings, sort_keys=True)
        try:
            self._conn.execute(
                "INSERT OR REPLACE INTO rooms (room_id, created_at, settings) "
                "VALUES (?, ?, ?)",
                (room_id, created_at, encoded),
            )
            self._conn.commit()
        except sqlite3.Error:
            log.exception(
                "Could not persist room %s at %s; it will be unknown after a restart",
                room_id,
                self._path,
            )

    def exists(self, room_id: str) -> bool:
        try:
            row = self._conn.execute(
                "SELECT 1 FROM rooms WHERE room_id = ? AND created_at > ?",
                (room_id, time.time() - ROOM_TTL_SECONDS),
            ).fetchone()
        except sqlite3.Error:
            log.exception("Could not read room %s from %s", room_id, self._path)
            return False
        return row is not None

    def get_settings(self, room_id: str) -> Optional[StoredSettings]:
        try:
            row = self._conn.execute(
                "SELECT settings FROM rooms WHERE room_id = ?", (room_id,)
            ).fetchone()
        except sqlite3.Error:
            log.exception(
                "Could not read settings for room %s from %s", room_id, self._path
            )
            return None
        if row is None or row[0] is None:
            return None
        try:
            decoded = json.loads(row[0])
        except (TypeError, ValueError):
            log.warning("Ignoring unreadable settings stored for room %s", room_id)
            return None
        return decoded if isinstance(decoded, dict) else None

    def set_settings(self, room_id: str, settings: StoredSettings) -> None:
        try:
            self._conn.execute(
                "UPDATE rooms SET settings = ? WHERE room_id = ?",
                (json.dumps(settings, sort_keys=True), room_id),
            )
            self._conn.commit()
        except sqlite3.Error:
            log.exception(
                "Could not persist settings for room %s at %s; a restart will "
                "revive it with its previous windows",
                room_id,
                self._path,
            )

    def get_access(self, room_id: str) -> Optional[StoredAccess]:
        try:
            row = self._conn.execute(
                "SELECT access FROM rooms WHERE room_id = ?", (room_id,)
            ).fetchone()
        except sqlite3.Error:
            log.exception(
                "Could not read access for room %s from %s", room_id, self._path
            )
            return None
        if row is None or row[0] is None:
            return None
        try:
            decoded = json.loads(row[0])
        except (TypeError, ValueError):
            log.warning("Ignoring unreadable access record stored for room %s", room_id)
            return None
        return decoded if isinstance(decoded, dict) else None

    def set_access(self, room_id: str, access: StoredAccess) -> None:
        try:
            self._conn.execute(
                "UPDATE rooms SET access = ? WHERE room_id = ?",
                (json.dumps(access, sort_keys=True), room_id),
            )
            self._conn.commit()
        except sqlite3.Error:
            log.exception(
                "Could not persist access for room %s at %s; after a restart it "
                "revives with its previous owner and join policy",
                room_id,
                self._path,
            )

    def remove(self, room_id: str) -> None:
        try:
            self._conn.execute("DELETE FROM rooms WHERE room_id = ?", (room_id,))
            self._conn.commit()
        except sqlite3.Error:
            log.exception("Could not remove room %s from %s", room_id, self._path)

    def count(self) -> int:
        try:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM rooms WHERE created_at > ?",
                (time.time() - ROOM_TTL_SECONDS,),
            ).fetchone()
        except sqlite3.Error:
            log.exception("Could not count rooms in %s", self._path)
            return 0
        return int(row[0])

    def purge_expired(self, now: float) -> int:
        try:
            cursor = self._conn.execute(
                "DELETE FROM rooms WHERE created_at <= ?",
                (now - ROOM_TTL_SECONDS,),
            )
            self._conn.commit()
        except sqlite3.Error:
            log.exception("Could not purge expired rooms from %s", self._path)
            return 0
        return cursor.rowcount


def _decode_record(raw: Any, kind: str, room_id: str) -> Optional[dict[str, Any]]:
    if raw is None:
        return None
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        log.warning("Ignoring unreadable %s stored for room %s", kind, room_id)
        return None
    return decoded if isinstance(decoded, dict) else None


class RedisRoomStore:
    """Room codes in Redis, so every relay instance accepts every code.

    Layout, under `prefix` (default synergo:rooms):

        <prefix>            sorted set, code -> created_at
        <prefix>:settings   hash, code -> settings JSON
        <prefix>:access     hash, code -> access JSON

    The TTL is applied to the creation time, exactly as SqliteRoomStore does,
    and purge_expired trims the set and both hashes together. Each record kind
    is opaque JSON, as in the other stores.

    Every write also goes to `local` (this instance's SQLite or memory store),
    and reads fall back to it while Redis is unreachable, so the codes this
    instance has seen keep resolving through an outage. Codes created on other
    instances are unknown here until Redis is back.
    """

    RECORDS = ("settings", "access")

    def __init__(
        self,
        client: Any,
        *,
        local: Optional[RoomStore] = None,
        prefix: str = REDIS_KEY_PREFIX,
        retry_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.client = client
        self.local: RoomStore = MemoryRoomStore() if local is None else local
        self._index = prefix
        self._record_keys = {kind: f"{prefix}:{kind}" for kind in self.RECORDS}
        self._guard = RedisGuard(
            "room store",
            degraded="room codes fall back to the ones this instance has seen",
            retry_seconds=retry_seconds,
            clock=clock,
        )

    @property
    def available(self) -> bool:
        return self._guard.available

    def add(
        self,
        room_id: str,
        created_at: float,
        settings: Optional[StoredSettings] = None,
    ) -> None:
        self.local.add(room_id, created_at, settings)

        def call() -> None:
            pipe = self.client.pipeline(transaction=True)
            pipe.zadd(self._index, {room_id: created_at})
            # Like SQLite's INSERT OR REPLACE: a re-added code starts over.
            pipe.hdel(self._record_keys["access"], room_id)
            if settings is None:
                pipe.hdel(self._record_keys["settings"], room_id)
            else:
                pipe.hset(self._record_keys["settings"], room_id, json.dumps(settings, sort_keys=True))
            pipe.execute()

        self._guard.run(call, lambda: None)

    def exists(self, room_id: str) -> bool:
        def call() -> bool:
            score = self.client.zscore(self._index, room_id)
            return score is not None and float(score) > time.time() - ROOM_TTL_SECONDS

        return self._guard.run(call, lambda: self.local.exists(room_id))

    def _get(self, kind: str, room_id: str, fallback: Callable[[], Optional[dict[str, Any]]]) -> Optional[dict[str, Any]]:
        return self._guard.run(
            lambda: _decode_record(self.client.hget(self._record_keys[kind], room_id), kind, room_id),
            fallback,
        )

    def _set(self, kind: str, room_id: str, value: dict[str, Any]) -> None:
        def call() -> None:
            # Unknown codes cannot be given records, as in the other stores.
            if self.client.zscore(self._index, room_id) is None:
                return
            self.client.hset(self._record_keys[kind], room_id, json.dumps(value, sort_keys=True))

        self._guard.run(call, lambda: None)

    def get_settings(self, room_id: str) -> Optional[StoredSettings]:
        return self._get("settings", room_id, lambda: self.local.get_settings(room_id))

    def set_settings(self, room_id: str, settings: StoredSettings) -> None:
        self.local.set_settings(room_id, settings)
        self._set("settings", room_id, settings)

    def get_access(self, room_id: str) -> Optional[StoredAccess]:
        return self._get("access", room_id, lambda: self.local.get_access(room_id))

    def set_access(self, room_id: str, access: StoredAccess) -> None:
        self.local.set_access(room_id, access)
        self._set("access", room_id, access)

    def remove(self, room_id: str) -> None:
        self.local.remove(room_id)

        def call() -> None:
            pipe = self.client.pipeline(transaction=True)
            pipe.zrem(self._index, room_id)
            for key in self._record_keys.values():
                pipe.hdel(key, room_id)
            pipe.execute()

        self._guard.run(call, lambda: None)

    def count(self) -> int:
        cutoff = time.time() - ROOM_TTL_SECONDS
        return self._guard.run(
            # "(" makes the bound exclusive: created_at > cutoff, as in SQLite.
            lambda: int(self.client.zcount(self._index, f"({cutoff!r}", "+inf")),
            self.local.count,
        )

    def purge_expired(self, now: float) -> int:
        purged_locally = self.local.purge_expired(now)
        cutoff = now - ROOM_TTL_SECONDS

        def call() -> int:
            pipe = self.client.pipeline(transaction=True)
            pipe.zrangebyscore(self._index, "-inf", cutoff)
            pipe.zremrangebyscore(self._index, "-inf", cutoff)
            expired, removed = pipe.execute()
            if expired:
                pipe = self.client.pipeline(transaction=True)
                for key in self._record_keys.values():
                    pipe.hdel(key, *expired)
                pipe.execute()
            return int(removed)

        return self._guard.run(call, lambda: purged_locally)


def build_room_store() -> RoomStore:
    """Pick a store from the environment.

    - SYNERGO_REDIS_URL set: RedisRoomStore, shared by every instance, with the
      local store below as this instance's copy for Redis outages. Unless
      SYNERGO_ROOM_STORE=local, which keeps codes on this instance only.
    - Otherwise SQLite when SYNERGO_STATE_PATH points at a usable file, and
      memory when it does not.
    """
    choice = (os.environ.get(ROOM_STORE_ENV) or "").strip().lower()
    if choice and choice not in ROOM_STORE_CHOICES:
        log.warning(
            "Ignoring %s=%r: expected one of %s",
            ROOM_STORE_ENV,
            choice,
            ", ".join(ROOM_STORE_CHOICES),
        )
        choice = ""
    redis_url = (os.environ.get(REDIS_URL_ENV) or "").strip()
    client = None
    if redis_url and choice != "local":
        client = connect_redis(redis_url, purpose="room codes")
    elif choice == "redis":
        log.warning(
            "%s=redis needs %s; room codes stay on this instance", ROOM_STORE_ENV, REDIS_URL_ENV
        )
    local = _build_local_store(warn_if_memory=client is None)
    if client is None:
        return local
    log.info(
        "Room codes shared through Redis; this instance keeps its own copy (%s) "
        "for Redis outages",
        type(local).__name__,
    )
    return RedisRoomStore(client, local=local)


def _build_local_store(*, warn_if_memory: bool) -> RoomStore:
    """SQLite when SYNERGO_STATE_PATH points at a usable file, memory otherwise."""
    path = os.environ.get(STATE_PATH_ENV)
    if not path:
        if warn_if_memory:
            log.warning(
                "%s is not set: room codes are kept in memory, so every room becomes "
                "unknown when this process restarts and each deploy kicks all users "
                "out. Set %s to a file on a host with a persistent disk to keep room "
                "codes working across restarts.",
                STATE_PATH_ENV,
                STATE_PATH_ENV,
            )
        return MemoryRoomStore()
    try:
        store = SqliteRoomStore(path)
    except (sqlite3.Error, OSError):
        log.exception(
            "Could not open the room registry at %s; falling back to in-memory "
            "room codes, which do not survive a restart",
            path,
        )
        return MemoryRoomStore()
    log.info("Room codes persisted at %s", path)
    return store
