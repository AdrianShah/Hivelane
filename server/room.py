"""In-memory state for the single global room. Ephemeral by design: nothing
here survives a server restart."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

from fastapi import WebSocket
from pydantic import BaseModel

try:
    from .models import ActivityMessage, ActivityUpdate
except ImportError:  # Render rootDir=server
    from models import ActivityMessage, ActivityUpdate

log = logging.getLogger(__name__)

# How long a peer's last touch of a file stays eligible for conflict comparison.
CONFLICT_WINDOW_SECONDS = 60.0


@dataclass
class FileTouch:
    user_id: str
    file_path: str
    diff: str
    prompt: Optional[str]
    timestamp: float


@dataclass
class FileOverlap:
    """One file that the current user and another active user both touched recently."""

    file_path: str
    current_diff: str
    other: FileTouch


class Room:
    def __init__(self) -> None:
        self._members: dict[str, WebSocket] = {}
        self._spectators: set[WebSocket] = set()
        # file_path -> {user_id: FileTouch} — latest edit per user per file.
        self._file_activity: dict[str, dict[str, FileTouch]] = {}
        # Last broadcast activity per connected user (for spectator catch-up).
        self._latest_updates: dict[str, ActivityUpdate] = {}
        self._lock = asyncio.Lock()

    # --- membership ----------------------------------------------------------

    def is_taken(self, user_id: str) -> bool:
        return user_id in self._members

    def is_active(self, user_id: str) -> bool:
        return user_id in self._members

    @property
    def member_ids(self) -> list[str]:
        return list(self._members)

    def add_member(self, user_id: str, socket: WebSocket) -> None:
        self._members[user_id] = socket

    def remove_member(self, user_id: str) -> None:
        self._members.pop(user_id, None)
        self._latest_updates.pop(user_id, None)

    def add_spectator(self, socket: WebSocket) -> None:
        self._spectators.add(socket)

    def remove_spectator(self, socket: WebSocket) -> None:
        self._spectators.discard(socket)

    # --- file activity -------------------------------------------------------

    def record_activity(self, message: ActivityMessage) -> None:
        for change in message.changes:
            touches = self._file_activity.setdefault(change.file_path, {})
            touches[message.user_id] = FileTouch(
                user_id=message.user_id,
                file_path=change.file_path,
                diff=change.diff,
                prompt=message.prompt,
                timestamp=message.timestamp,
            )

    def remember_update(self, update: ActivityUpdate) -> None:
        self._latest_updates[update.user_id] = update

    def latest_updates(self) -> list[ActivityUpdate]:
        return list(self._latest_updates.values())

    def recent_other_touches(
        self,
        file_path: str,
        user_id: str,
        now: float,
        *,
        active_only: bool = True,
        window_seconds: float = CONFLICT_WINDOW_SECONDS,
    ) -> list[FileTouch]:
        """Other users' recent touches of the same file within the conflict window."""
        touches = self._file_activity.get(file_path, {})
        results: list[FileTouch] = []
        for other_id, touch in touches.items():
            if other_id == user_id:
                continue
            if active_only and not self.is_active(other_id):
                continue
            if now - touch.timestamp > window_seconds:
                continue
            results.append(touch)
        return results

    def find_overlaps(self, message: ActivityMessage) -> list[FileOverlap]:
        """Return per-file overlaps with the most recent active peer (if any).

        Call this BEFORE record_activity so the current batch is compared only
        against peers — not against a just-written self entry.
        """
        overlaps: list[FileOverlap] = []
        for change in message.changes:
            others = self.recent_other_touches(
                change.file_path, message.user_id, message.timestamp
            )
            if not others:
                continue
            other = max(others, key=lambda touch: touch.timestamp)
            overlaps.append(
                FileOverlap(
                    file_path=change.file_path,
                    current_diff=change.diff,
                    other=other,
                )
            )
        return overlaps

    # --- broadcast -----------------------------------------------------------

    async def broadcast(self, message: BaseModel) -> None:
        payload = message.model_dump()
        targets = list(self._members.items()) + [(None, s) for s in self._spectators]
        async with self._lock:
            for user_id, socket in targets:
                try:
                    await socket.send_json(payload)
                except Exception:
                    log.warning("Dropping unreachable socket (user=%s)", user_id)

    async def send(self, socket: WebSocket, message: BaseModel) -> None:
        await socket.send_json(message.model_dump())
