"""Wire format for every WebSocket message, as the relay sees it.

Mirrored on purpose in synergo/protocol.py (the daemon must not import the
relay package) and in frontend/src/types.ts. Mirrors drift, so the copies are
pinned by tests: server/tests/test_protocol_contract.py requires every message
here to have the same JSON schema as its synergo/protocol.py twin and a
TypeScript declaration for each frame the dashboard receives, and
protocol/schema.json is regenerated from the client copy by protocol/generate.py.
"""

from __future__ import annotations

import time
from typing import Annotated, Literal, Optional

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

# Which AI coding tool a daemon is reporting for. Defaults to "unknown" so
# older clients that omit the field stay compatible.
AgentKind = Literal[
    "cursor",
    "claude-code",
    "codex",
    "antigravity",
    "gemini-cli",
    "devin",
    "grok",
    "aider",
    "human",
    "unknown",
]


# Opaque repository identity computed by the daemon (a hash of the normalized
# origin URL, else the root commit). Touches are compared only within one
# repo_id, so two repos that both have src/main.py never collide. Empty means
# unknown, which is what every pre-repo_id client implicitly sends; empty
# matches only empty, preserving the old behaviour between two old clients.
# Bounded because the relay keys in-memory state on it.
RepoId = Annotated[str, Field(max_length=64)]


class FileChange(BaseModel):
    file_path: str
    diff: str


# --- Client -> Server ---------------------------------------------------------


class RoomSettings(BaseModel):
    """Team-wide conflict windows from the repo's .synergo.toml.

    Sent on join so the relay enforces what the team committed, not its own
    defaults. Bounded so one member cannot disable blocking room-wide by
    accident (a zero block window still warns).
    """

    block_window_seconds: float = Field(default=20.0, ge=0, le=600)
    warn_window_seconds: float = Field(default=60.0, ge=0, le=3600)


class JoinMessage(BaseModel):
    type: Literal["join"] = "join"
    # None marks a spectator (the dashboard), which receives broadcasts but is
    # not a room member and skips name validation entirely.
    name: Optional[str] = None
    agent: AgentKind = "unknown"
    # 0 means a client old enough not to declare one. Spectators are exempt:
    # the dashboard only consumes broadcasts and cannot silently under-report.
    protocol_version: int = 0
    repo_id: RepoId = ""
    # Optional: a member that carries a committed .synergo.toml shares its
    # windows with the room. None leaves the room's current settings alone.
    settings: Optional[RoomSettings] = None

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
    repo_id: RepoId = ""


class IntentMessage(BaseModel):
    """What the agent is currently trying to do, derived from the user's prompt.
    Declared up front so peers can be warned before they collide with it."""

    type: Literal["intent"] = "intent"
    user_id: str
    prompt: str
    timestamp: float = Field(default_factory=time.time)
    repo_id: RepoId = ""


class EditCheckRequest(BaseModel):
    """Pre-edit permission probe. request_id correlates the response on an
    agent's blocking hook."""

    type: Literal["edit_check"] = "edit_check"
    request_id: str
    file_path: str
    symbol: Optional[str] = None
    repo_id: RepoId = ""


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
    repo_id: RepoId = ""


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
    repo_id: RepoId = ""


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
    repo_id: RepoId = ""


class ConflictAlert(BaseModel):
    type: Literal["conflict_alert"] = "conflict_alert"
    users: list[str]
    file_path: str
    verdict: Verdict
    explanation: str
    timestamp: float = Field(default_factory=time.time)


class RoomSettingsUpdate(BaseModel):
    """The conflict windows the relay is enforcing for this room.

    Broadcast whenever a member's join changes them, and sent to every
    spectator on connect, so the dashboard always shows the windows actually in
    force rather than whatever the viewer's own repo would set. Flat rather than
    nesting RoomSettings so a dashboard reads it like every other feed event.
    set_by is None for relay defaults and settings chosen at room creation.
    """

    type: Literal["room_settings"] = "room_settings"
    block_window_seconds: float
    warn_window_seconds: float
    set_by: Optional[str] = None
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


class RateLimited(BaseModel):
    """Sent once, before closing with 4429, to a caller over the per-IP
    connection-attempt budget. Arrives before any join, so spectators and
    daemons alike must handle it."""

    type: Literal["rate_limited"] = "rate_limited"
    reason: str
    retry_after: int


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
