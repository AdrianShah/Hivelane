"""Synergo relay wire format, as seen from a client.

Deliberately duplicated from server/models.py: the daemon installs as a
standalone tool and must not import the relay package. The duplication is
policed rather than trusted: server/tests/test_protocol_contract.py fails when
any message here and its relay twin produce different JSON schemas, and
protocol/schema.json (generated from this module by protocol/generate.py) is
the language-neutral copy other clients can build against.
"""

from __future__ import annotations

import time
from typing import Annotated, Literal, Optional

from pydantic import BaseModel, Field

Decision = Literal["allow", "warn", "block"]

Verdict = Literal["overlapping", "related", "unrelated"]

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

# Opaque repository identity (see synergo.daemon.compute_repo_id). Empty means
# "unknown repo", which is also what a client too old to send one implies.
RepoId = Annotated[str, Field(max_length=64)]


class FileChange(BaseModel):
    file_path: str
    diff: str


# Must match server/models.py PROTOCOL_VERSION. Bump both together when the
# message contract changes in a way an older peer cannot satisfy.
PROTOCOL_VERSION = 1
# Oldest client protocol the relay accepts. The daemon never enforces it; it is
# mirrored so VersionRejected's default, and therefore its schema, matches.
MIN_SUPPORTED_PROTOCOL = 1


# --- Client -> relay ---------------------------------------------------------


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
    name: Optional[str] = None
    agent: AgentKind = "unknown"
    # Defaults to what this build speaks so no caller can under-declare. The
    # relay's twin defaults to 0 instead, meaning "a client too old to say".
    protocol_version: int = PROTOCOL_VERSION
    repo_id: RepoId = ""
    # Optional: a member that carries a committed .synergo.toml shares its
    # windows with the room. None leaves the room's current settings alone.
    settings: Optional[RoomSettings] = None


class ActivityMessage(BaseModel):
    type: Literal["activity"] = "activity"
    user_id: str
    timestamp: float = Field(default_factory=time.time)
    prompt: Optional[str] = None
    changes: list[FileChange]
    agent: AgentKind = "unknown"
    repo_id: RepoId = ""


class IntentMessage(BaseModel):
    type: Literal["intent"] = "intent"
    user_id: str
    prompt: str
    timestamp: float = Field(default_factory=time.time)
    repo_id: RepoId = ""


class EditCheckRequest(BaseModel):
    type: Literal["edit_check"] = "edit_check"
    request_id: str
    file_path: str
    symbol: Optional[str] = None
    repo_id: RepoId = ""


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


class ActivityUpdate(BaseModel):
    type: Literal["activity_update"] = "activity_update"
    user_id: str
    timestamp: float
    prompt: Optional[str] = None
    changes: list[FileChange]
    caption: Optional[str] = None
    agent: AgentKind = "unknown"
    repo_id: RepoId = ""


class IntentUpdate(BaseModel):
    type: Literal["intent_update"] = "intent_update"
    user_id: str
    agent: AgentKind = "unknown"
    prompt: str
    timestamp: float
    repo_id: RepoId = ""


class EditDecision(BaseModel):
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
    type: Literal["rate_limited"] = "rate_limited"
    reason: str
    retry_after: int


class SynergoFeed(BaseModel):
    type: Literal["synergo_feed"] = "synergo_feed"
    events: list[dict]


# Every top-level wire message, by direction. protocol/generate.py and the
# contract tests enumerate these, so a message missing here is a message
# nothing checks.
CLIENT_TO_RELAY: tuple[type[BaseModel], ...] = (
    JoinMessage,
    ActivityMessage,
    IntentMessage,
    EditCheckRequest,
)

RELAY_TO_CLIENT: tuple[type[BaseModel], ...] = (
    EditCheckResponse,
    ActivityUpdate,
    IntentUpdate,
    EditDecision,
    ConflictAlert,
    RoomSettingsUpdate,
    UserJoined,
    UserLeft,
    NameRejected,
    VersionRejected,
    RoomRejected,
    RateLimited,
    SynergoFeed,
)
