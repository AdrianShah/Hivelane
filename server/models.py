"""Wire format for every WebSocket message. Single source of truth shared by
the relay, the watcher, and (mirrored in TypeScript) the frontend."""

from __future__ import annotations

import time
from typing import Literal, Optional

from pydantic import BaseModel, Field

Verdict = Literal["overlapping", "related", "unrelated"]


class FileChange(BaseModel):
    file_path: str
    diff: str


# --- Client -> Server ---------------------------------------------------------


class JoinMessage(BaseModel):
    type: Literal["join"] = "join"
    # None marks a spectator (the dashboard), which receives broadcasts but is
    # not a room member and skips name validation entirely.
    name: Optional[str] = None

    @property
    def is_spectator(self) -> bool:
        return self.name is None


class ActivityMessage(BaseModel):
    type: Literal["activity"] = "activity"
    user_id: str
    timestamp: float = Field(default_factory=time.time)
    prompt: Optional[str] = None
    changes: list[FileChange]


# --- Server -> Client --------------------------------------------------------


class ActivityUpdate(BaseModel):
    type: Literal["activity_update"] = "activity_update"
    user_id: str
    timestamp: float
    prompt: Optional[str] = None
    changes: list[FileChange]
    caption: Optional[str] = None


class ConflictAlert(BaseModel):
    type: Literal["conflict_alert"] = "conflict_alert"
    users: list[str]
    file_path: str
    verdict: Verdict
    explanation: str
    timestamp: float = Field(default_factory=time.time)


class UserJoined(BaseModel):
    type: Literal["user_joined"] = "user_joined"
    user_id: str


class UserLeft(BaseModel):
    type: Literal["user_left"] = "user_left"
    user_id: str


class NameRejected(BaseModel):
    type: Literal["name_rejected"] = "name_rejected"
    reason: str


def placeholder_caption(message: ActivityMessage) -> str:
    """Stand-in until Gemini captioning lands. Same shape, no network call."""
    count = len(message.changes)
    if count == 1:
        return f"{message.user_id}'s agent is editing {message.changes[0].file_path}"
    return f"{message.user_id}'s agent is editing {count} files"
