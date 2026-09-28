"""Registry of live Room instances, keyed by short shareable room codes.

The room code is the only access control: whoever has the code can join.
Same trust model as a Google Docs share link, a Kahoot PIN, or a Figma
multiplayer link — no accounts, no passwords, no sessions."""

from __future__ import annotations

import logging
import secrets
import time
from typing import Optional

try:
    from .room import Room
    from .store import RoomStore, build_room_store
except ImportError:  # Render rootDir=server
    from room import Room
    from store import RoomStore, build_room_store

log = logging.getLogger(__name__)

# Base32-ish alphabet with ambiguous characters removed (0/O, 1/I) so codes
# are easy to read aloud and type on a whiteboard.
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 6

# Hard cap on room size: enough for a small hackathon team, small enough to
# stop accidental (or malicious) pile-on in one workspace.
MAX_MEMBERS = 6

# Cap concurrent live rooms to bound memory and block ghost-room flooding.
MAX_ROOMS = 100


def _generate_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


class RoomManager:
    """Live Room instances plus the durable set of valid room codes.

    Room contents stay in memory, but the codes are recorded in a RoomStore so
    a restart does not invalidate every workspace that is currently shared."""

    def __init__(self, store: Optional[RoomStore] = None) -> None:
        self._rooms: dict[str, Room] = {}
        self._store = build_room_store() if store is None else store

    def create_room(self) -> str:
        """Generate a fresh room code and register an empty Room under it."""
        self._store.purge_expired(time.time())
        # The durable count is the one that matters: after a restart, live rooms
        # start at zero and would otherwise let the registry grow past the cap.
        # The live count still bounds memory if the store is degraded.
        if max(len(self._rooms), self._store.count()) >= MAX_ROOMS:
            raise RuntimeError(f"Maximum number of rooms reached ({MAX_ROOMS})")
        for _ in range(10):  # collisions are astronomically unlikely; bound retries anyway
            room_id = _generate_code()
            if room_id not in self._rooms and not self._store.exists(room_id):
                self._rooms[room_id] = Room()
                self._store.add(room_id, time.time())
                log.info("Room %s created (%d room(s) live)", room_id, len(self._rooms))
                return room_id
        raise RuntimeError("Failed to generate a unique room code")

    def get(self, room_id: str) -> Room | None:
        """Look up a room by code, reviving a known code whose Room is gone.

        Still strict about unknown codes: a typo or an expired code returns None
        and the caller rejects the connection."""
        normalized = room_id.strip().upper()
        room = self._rooms.get(normalized)
        if room is not None:
            return room
        if not self._store.exists(normalized):
            return None
        # Known code with no live Room: the relay restarted. An empty Room is
        # the right starting point — every touch older than the conflict window
        # is ignored anyway, so the room re-forms from live activity.
        room = Room()
        self._rooms[normalized] = room
        log.info("Room %s revived after restart (%d room(s) live)", normalized, len(self._rooms))
        return room

    def cleanup_if_empty(self, room_id: str) -> None:
        normalized = room_id.strip().upper()
        room = self._rooms.get(normalized)
        if room is not None and room.is_empty:
            del self._rooms[normalized]
            # A finished room must stop being a valid code, or it would be
            # revived by the next connection that guesses or replays it.
            self._store.remove(normalized)
            log.info("Room %s removed (empty, %d room(s) left)", normalized, len(self._rooms))

    @property
    def room_count(self) -> int:
        """Rooms live in this process (what /health reports)."""
        return len(self._rooms)

    @property
    def durable_room_count(self) -> int:
        """Valid room codes, including those with no live Room yet."""
        return self._store.count()
