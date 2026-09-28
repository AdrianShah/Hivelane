"""Durable registry of which room codes exist.

Only the codes are persisted, never room contents: in-room state is disposable
(file touches expire inside room.CONFLICT_WINDOW_SECONDS and are swept), so a
restarted relay rebuilds coordination state from live activity within a minute.
Keeping the codes is what lets a daemon rejoin the same room after a deploy.

Stdlib only, so this works on any host with a writable disk and adds nothing to
operate.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from typing import Protocol

log = logging.getLogger(__name__)

# A room code stops being valid after this long, so the table cannot grow
# forever. Long enough that a code shared on a Monday still works all week.
ROOM_TTL_SECONDS = 7 * 24 * 60 * 60.0

STATE_PATH_ENV = "SYNERGO_STATE_PATH"


class RoomStore(Protocol):
    """Set of currently valid room codes with their creation times."""

    def add(self, room_id: str, created_at: float) -> None: ...

    def exists(self, room_id: str) -> bool:
        """True if the code is known and not older than ROOM_TTL_SECONDS."""
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

    def add(self, room_id: str, created_at: float) -> None:
        self._created_at[room_id] = created_at

    def exists(self, room_id: str) -> bool:
        created_at = self._created_at.get(room_id)
        return created_at is not None and created_at > time.time() - ROOM_TTL_SECONDS

    def remove(self, room_id: str) -> None:
        self._created_at.pop(room_id, None)

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
            "  created_at REAL NOT NULL"
            ")"
        )
        self._conn.commit()

    def add(self, room_id: str, created_at: float) -> None:
        try:
            self._conn.execute(
                "INSERT OR REPLACE INTO rooms (room_id, created_at) VALUES (?, ?)",
                (room_id, created_at),
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
