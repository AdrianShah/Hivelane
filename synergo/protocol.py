"""Synergo relay wire format, as seen from a client.

Deliberately duplicated from server/models.py: the daemon installs as a
standalone tool and must not import the relay package.
"""

from __future__ import annotations

import time
from typing import Literal, Optional

from pydantic import BaseModel, Field

Decision = Literal["allow", "warn", "block"]

AgentKind = Literal[
    "cursor",
    "claude-code",
    "codex",
    "antigravity",
    "gemini-cli",
    "devin",
    "grok",
    "unknown",
]

AGENT_KINDS: tuple[str, ...] = (
    "cursor",
    "claude-code",
    "codex",
    "antigravity",
    "gemini-cli",
    "devin",
    "grok",
    "unknown",
)


class FileChange(BaseModel):
    file_path: str
    diff: str


# --- Client -> relay ---------------------------------------------------------


class JoinMessage(BaseModel):
    type: Literal["join"] = "join"
    name: Optional[str] = None
    agent: AgentKind = "unknown"


class ActivityMessage(BaseModel):
    type: Literal["activity"] = "activity"
    user_id: str
    timestamp: float = Field(default_factory=time.time)
    prompt: Optional[str] = None
    changes: list[FileChange]
    agent: AgentKind = "unknown"


class IntentMessage(BaseModel):
    type: Literal["intent"] = "intent"
    user_id: str
    prompt: str
    timestamp: float = Field(default_factory=time.time)


class EditCheckRequest(BaseModel):
    type: Literal["edit_check"] = "edit_check"
    request_id: str
    file_path: str
    symbol: Optional[str] = None


ClientMessage = ActivityMessage | IntentMessage | EditCheckRequest


# --- Relay -> client ---------------------------------------------------------


class PeerTouch(BaseModel):
    user_id: str
    agent: AgentKind = "unknown"
    seconds_ago: float
    prompt: Optional[str] = None


class EditCheckResponse(BaseModel):
    type: Literal["edit_check_response"] = "edit_check_response"
    request_id: str
    decision: Decision
    file_path: str
    others: list[PeerTouch] = []
    reason: str = ""


class NameRejected(BaseModel):
    type: Literal["name_rejected"] = "name_rejected"
    reason: str


class RoomRejected(BaseModel):
    type: Literal["room_rejected"] = "room_rejected"
    reason: str


class SynergoFeed(BaseModel):
    type: Literal["synergo_feed"] = "synergo_feed"
    events: list[dict]
