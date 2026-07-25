"""Registry of live Room instances, keyed by short shareable room codes.

The room code is the only access control: whoever has the code can join.
Same trust model as a Google Docs share link, a Kahoot PIN, or a Figma
multiplayer link — no accounts, no passwords, no sessions."""

from __future__ import annotations

import logging
import secrets

try:
    from .room import Room
except ImportError:  # Render rootDir=server
    from room import Room

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
    def __init__(self) -> None:
        self._rooms: dict[str, Room] = {}

    def create_room(self) -> str:
        """Generate a fresh room code and register an empty Room under it."""
        if len(self._rooms) >= MAX_ROOMS:
            raise RuntimeError(f"Maximum number of rooms reached ({MAX_ROOMS})")
        for _ in range(10):  # collisions are astronomically unlikely; bound retries anyway
            room_id = _generate_code()
            if room_id not in self._rooms:
                self._rooms[room_id] = Room()
                log.info("Room %s created (%d room(s) live)", room_id, len(self._rooms))
                return room_id
        raise RuntimeError("Failed to generate a unique room code")

    def get(self, room_id: str) -> Room | None:
        """Strict lookup: rooms exist only after POST /rooms. Returns None if
        the code is unknown (callers must reject the connection)."""
        return self._rooms.get(room_id.strip().upper())

    def cleanup_if_empty(self, room_id: str) -> None:
        normalized = room_id.strip().upper()
        room = self._rooms.get(normalized)
        if room is not None and room.is_empty:
            del self._rooms[normalized]
            log.info("Room %s removed (empty, %d room(s) left)", normalized, len(self._rooms))

    @property
    def room_count(self) -> int:
        return len(self._rooms)
