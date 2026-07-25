"""In-memory state for the single global room. Ephemeral by design: nothing
here survives a server restart."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

from fastapi import WebSocket
from pydantic import BaseModel

from .models import ActivityMessage

log = logging.getLogger(__name__)

# How long a file's last touch stays eligible for conflict comparison.
CONFLICT_WINDOW_SECONDS = 120.0


@dataclass
class FileTouch:
    user_id: str
    file_path: str
    diff: str
    prompt: Optional[str]
    timestamp: float


class Room:
    def __init__(self) -> None:
        self._members: dict[str, WebSocket] = {}
        self._spectators: set[WebSocket] = set()
        # file_path -> most recent touch per user, used by conflict detection.
        self._file_activity: dict[str, dict[str, FileTouch]] = {}
        self._lock = asyncio.Lock()

    # --- membership ----------------------------------------------------------

    def is_taken(self, user_id: str) -> bool:
        return user_id in self._members

    @property
    def member_ids(self) -> list[str]:
        return list(self._members)

    def add_member(self, user_id: str, socket: WebSocket) -> None:
        self._members[user_id] = socket

    def remove_member(self, user_id: str) -> None:
        self._members.pop(user_id, None)

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

    def recent_other_touches(
        self, file_path: str, user_id: str, now: float
    ) -> list[FileTouch]:
        """Other users' recent touches of the same file. Consumed by the
        conflict-detection step; recording already happens today."""
        touches = self._file_activity.get(file_path, {})
        return [
            touch
            for other_id, touch in touches.items()
            if other_id != user_id and now - touch.timestamp <= CONFLICT_WINDOW_SECONDS
        ]

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
