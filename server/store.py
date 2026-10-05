"""Durable registry of which room codes exist.

Only the codes (and each room's conflict-window settings) are persisted, never
room contents: in-room state is disposable (file touches expire inside the
room's warn window and are swept), so a restarted relay rebuilds coordination
state from live activity within a minute. Keeping the codes is what lets a
daemon rejoin the same room after a deploy; keeping the settings is what stops
that revived room from quietly falling back to the default windows.

Settings are stored as an opaque JSON object: the store does not interpret
them (room_manager validates them on the way back out), so adding a setting
needs no schema change.

Stdlib only, so this works on any host with a writable disk and adds nothing to
operate.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from typing import Any, Optional, Protocol

log = logging.getLogger(__name__)

# A room code stops being valid after this long, so the table cannot grow
# forever. Long enough that a code shared on a Monday still works all week.
ROOM_TTL_SECONDS = 7 * 24 * 60 * 60.0

STATE_PATH_ENV = "SYNERGO_STATE_PATH"

# Plain JSON object; see the module docstring for why it stays untyped here.
StoredSettings = dict[str, Any]


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

    def remove(self, room_id: str) -> None:
        self._created_at.pop(room_id, None)
        self._settings.pop(room_id, None)

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
            "  settings TEXT"
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


def build_room_store() -> RoomStore:
    """Pick a store from the environment: sqlite when SYNERGO_STATE_PATH points
    at a usable file, in-memory otherwise."""
    path = os.environ.get(STATE_PATH_ENV)
    if not path:
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
