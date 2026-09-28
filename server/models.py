"""Wire format for every WebSocket message. Single source of truth shared by
the relay, the watcher, and (mirrored in TypeScript) the frontend."""

from __future__ import annotations

import time
from typing import Literal, Optional

from pydantic import BaseModel, Field

Verdict = Literal["overlapping", "related", "unrelated"]

# Wire-protocol version, bumped when the message contract changes in a way older
# clients cannot satisfy. A daemon that predates edit_check would connect happily
# and simply never ask permission, so conflict prevention would be silently off —
# the handshake rejects it instead.
PROTOCOL_VERSION = 1
MIN_SUPPORTED_PROTOCOL = 1

# Answer to "may I edit this file right now?" — an agent's PreToolUse hook maps
# "block" to a denied tool call.
Decision = Literal["allow", "warn", "block"]

# Which AI coding tool a watcher process is reporting for. Defaults to
# "unknown" so older clients that omit the field stay compatible.
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


class FileChange(BaseModel):
    file_path: str
    diff: str


# --- Client -> Server ---------------------------------------------------------


class JoinMessage(BaseModel):
    type: Literal["join"] = "join"
    # None marks a spectator (the dashboard), which receives broadcasts but is
    # not a room member and skips name validation entirely.
    name: Optional[str] = None
    agent: AgentKind = "unknown"
    # 0 means a client old enough not to declare one. Spectators are exempt:
    # the dashboard only consumes broadcasts and cannot silently under-report.
    protocol_version: int = 0

    @property
    def is_spectator(self) -> bool:
        return self.name is None


class ActivityMessage(BaseModel):
    type: Literal["activity"] = "activity"
    user_id: str
    timestamp: float = Field(default_factory=time.time)
    prompt: Optional[str] = None
    changes: list[FileChange]
    agent: AgentKind = "unknown"


class IntentMessage(BaseModel):
    """What the agent is currently trying to do, derived from the user's prompt.
    Declared up front so peers can be warned before they collide with it."""

    type: Literal["intent"] = "intent"
    user_id: str
    prompt: str
    timestamp: float = Field(default_factory=time.time)


class EditCheckRequest(BaseModel):
    """Pre-edit permission probe. request_id correlates the response on an
    agent's blocking hook."""

    type: Literal["edit_check"] = "edit_check"
    request_id: str
    file_path: str
    symbol: Optional[str] = None


# --- Server -> Client --------------------------------------------------------


class PeerTouch(BaseModel):
    user_id: str
    agent: AgentKind = "unknown"
    seconds_ago: float
    prompt: Optional[str] = None


class ActivityUpdate(BaseModel):
    type: Literal["activity_update"] = "activity_update"
    user_id: str
    timestamp: float
    prompt: Optional[str] = None
    changes: list[FileChange]
    caption: Optional[str] = None
    agent: AgentKind = "unknown"


class EditCheckResponse(BaseModel):
    """Point-to-point answer to an EditCheckRequest. Sent immediately (never
    through the debounced feed) — an agent is blocked waiting on it."""

    type: Literal["edit_check_response"] = "edit_check_response"
    request_id: str
    decision: Decision
    file_path: str
    others: list[PeerTouch] = []
    reason: str = ""


class IntentUpdate(BaseModel):
    type: Literal["intent_update"] = "intent_update"
    user_id: str
    agent: AgentKind = "unknown"
    prompt: str
    timestamp: float


class EditDecision(BaseModel):
    """Feed record of a non-allow edit check. Allow decisions are not broadcast
    so a busy agent's file probes don't drown the dashboard — the narrowed
    decision type keeps the dashboard's union honest."""

    type: Literal["edit_decision"] = "edit_decision"
    user_id: str
    agent: AgentKind = "unknown"
    file_path: str
    decision: Literal["warn", "block"]
    others: list[str] = []
    reason: str = ""
    timestamp: float = Field(default_factory=time.time)


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
    agent: AgentKind = "unknown"


class UserLeft(BaseModel):
    type: Literal["user_left"] = "user_left"
    user_id: str


class NameRejected(BaseModel):
    type: Literal["name_rejected"] = "name_rejected"
    reason: str


class VersionRejected(BaseModel):
    type: Literal["version_rejected"] = "version_rejected"
    reason: str
    server_protocol: int = PROTOCOL_VERSION
    min_supported: int = MIN_SUPPORTED_PROTOCOL


class RoomRejected(BaseModel):
    type: Literal["room_rejected"] = "room_rejected"
    reason: str


class SynergoFeed(BaseModel):
    """Debounced room broadcast: one WebSocket frame every ~500ms carrying
    all queued events for that room (the dashboard's Synergo feed)."""

    type: Literal["synergo_feed"] = "synergo_feed"
    events: list[dict]


def placeholder_caption(message: ActivityMessage) -> str:
    """Stand-in until Gemini captioning lands. Same shape, no network call."""
    count = len(message.changes)
    if count == 0:
        if message.prompt:
            return f"{message.user_id}'s agent received a new prompt"
        return f"{message.user_id}'s agent is idle"
    if count == 1:
        return f"{message.user_id}'s agent is editing {message.changes[0].file_path}"
    return f"{message.user_id}'s agent is editing {count} files"
