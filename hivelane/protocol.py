"""Hivelane relay wire format, as seen from a client.

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
    "aider",
    "human",
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
    "aider",
    "human",
    "unknown",
)

# Opaque repository identity (see hivelane.daemon.compute_repo_id). Empty means
# "unknown repo", which is also what a client too old to send one implies.
RepoId = Annotated[str, Field(max_length=64)]


# Granularity at which an edit check (or its broadcast) found the conflict.
DecisionScope = Literal["symbol", "lines", "file", "intent", "plan", "branch"]

# How a peer is involved: edited the file, plans to, or only mentioned it.
PeerKind = Literal["edited", "planned", "intent"]


# End-to-end encrypted payload: opaque base64 ciphertext a client produced with
# the team's shared room key. The relay never decrypts or inspects it; it only
# copies the sender's blob onto the matching broadcast (and onto PeerTouch /
# PlanConflict records derived from that sender's messages). Empty in rooms
# that do not use e2e. Bounded so one member cannot make the relay hold or fan
# out megabytes per message; an oversized blob fails validation and the message
# is dropped.
E2E_MAX_CHARS = 256 * 1024
E2EBlob = Annotated[str, Field(max_length=E2E_MAX_CHARS)]


class FileChange(BaseModel):
    file_path: str
    diff: str
    # Names of functions/classes the change touches ("<imports>" for an
    # import-block edit) and 1-based inclusive line ranges in the new file.
    # Empty when the client could not extract them (or predates them).
    symbols: list[str] = []
    line_ranges: list[tuple[int, int]] = []


# Must match server/models.py PROTOCOL_VERSION. Bump both together when the
# message contract changes in a way an older peer cannot satisfy.
PROTOCOL_VERSION = 1
# Oldest client protocol the relay accepts. The daemon never enforces it; it is
# mirrored so VersionRejected's default, and therefore its schema, matches.
MIN_SUPPORTED_PROTOCOL = 1


# --- Client -> relay ---------------------------------------------------------


class RoomSettings(BaseModel):
    """Team-wide conflict windows from the repo's .hivelane.toml.

    Sent on join so the relay enforces what the team committed, not its own
    defaults. Bounded so one member cannot disable blocking room-wide by
    accident (a zero block window still warns).
    """

    block_window_seconds: float = Field(default=20.0, ge=0, le=600)
    warn_window_seconds: float = Field(default=60.0, ge=0, le=3600)
    # The team committed `e2e = true`: members encrypt prompts, diffs and
    # paths into each message's e2e blob. The relay only stores and
    # broadcasts the flag so every client (and the dashboard) knows.
    e2e: bool = False
    # Hours of room events the relay keeps for the dashboard's replay view
    # (served on history_request). 0 keeps no history. Capped so a room
    # cannot make the relay hold more than a day of events.
    history_hours: float = Field(default=0, ge=0, le=24)


class JoinMessage(BaseModel):
    type: Literal["join"] = "join"
    name: Optional[str] = None
    agent: AgentKind = "unknown"
    # Defaults to what this build speaks so no caller can under-declare. The
    # relay's twin defaults to 0 instead, meaning "a client too old to say".
    protocol_version: int = PROTOCOL_VERSION
    repo_id: RepoId = ""
    # Optional: a member that carries a committed .hivelane.toml shares its
    # windows with the room. None leaves the room's current settings alone.
    settings: Optional[RoomSettings] = None
    # Secret returned once by POST /rooms. A join carrying the matching
    # token marks that socket as a room owner (may approve joins and set
    # the room policy). A wrong token is treated as no token.
    owner_token: Optional[str] = None


class ActivityMessage(BaseModel):
    type: Literal["activity"] = "activity"
    user_id: str
    timestamp: float = Field(default_factory=time.time)
    prompt: Optional[str] = None
    changes: list[FileChange]
    agent: AgentKind = "unknown"
    repo_id: RepoId = ""
    branch: str = ""
    base_commit: str = ""
    e2e: E2EBlob = ""


class IntentMessage(BaseModel):
    type: Literal["intent"] = "intent"
    user_id: str
    prompt: str
    timestamp: float = Field(default_factory=time.time)
    repo_id: RepoId = ""
    e2e: E2EBlob = ""


class EditCheckRequest(BaseModel):
    type: Literal["edit_check"] = "edit_check"
    request_id: str
    file_path: str
    symbol: Optional[str] = None
    repo_id: RepoId = ""
    symbols: list[str] = []
    line_ranges: list[tuple[int, int]] = []
    branch: str = ""
    e2e: E2EBlob = ""


class FileRequest(BaseModel):
    type: Literal["file_request"] = "file_request"
    file_path: str
    repo_id: RepoId = ""
    e2e: E2EBlob = ""
    # Optional, like an edit_check's: what the requester means to edit, so
    # holders are judged at the same granularity (a teammate on another
    # branch, or in another function, does not hold the file).
    branch: str = ""
    symbols: list[str] = []


class HistoryRequest(BaseModel):
    type: Literal["history_request"] = "history_request"
    since: float = 0


ClientMessage = ActivityMessage | IntentMessage | EditCheckRequest


# --- Relay -> client ---------------------------------------------------------


class PeerTouch(BaseModel):
    user_id: str
    agent: AgentKind = "unknown"
    seconds_ago: float
    prompt: Optional[str] = None
    symbols: list[str] = []
    branch: str = ""
    kind: PeerKind = "edited"
    e2e: E2EBlob = ""


class EditCheckResponse(BaseModel):
    type: Literal["edit_check_response"] = "edit_check_response"
    request_id: str
    decision: Decision
    file_path: str
    others: list[PeerTouch] = []
    reason: str = ""
    scope: DecisionScope = "file"
    decision_id: str = ""
    # 1 = next in line for this file once its holder lapses; 0 = not queued.
    queue_position: int = 0
    # Something actionable to do meanwhile, built from room state (a free
    # file nearby, the module's tests, or how long to wait). May be empty.
    suggestion: str = ""


class ActivityUpdate(BaseModel):
    type: Literal["activity_update"] = "activity_update"
    user_id: str
    timestamp: float
    prompt: Optional[str] = None
    changes: list[FileChange]
    caption: Optional[str] = None
    agent: AgentKind = "unknown"
    repo_id: RepoId = ""
    branch: str = ""
    e2e: E2EBlob = ""


class IntentUpdate(BaseModel):
    type: Literal["intent_update"] = "intent_update"
    user_id: str
    agent: AgentKind = "unknown"
    prompt: str
    timestamp: float
    repo_id: RepoId = ""
    e2e: E2EBlob = ""


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
    scope: DecisionScope = "file"
    decision_id: str = ""
    queue_position: int = 0
    suggestion: str = ""
    e2e: E2EBlob = ""


class PlanMessage(BaseModel):
    type: Literal["plan"] = "plan"
    user_id: str
    summary: str = ""
    files: list[str] = []
    symbols: list[str] = []
    repo_id: RepoId = ""
    branch: str = ""
    ttl_seconds: float = Field(1800, ge=60, le=86400)
    e2e: E2EBlob = ""
    # Echoed on the plan_conflicts reply, so a client with several plans in
    # flight can match each reply to its plan. Optional: "" when unset.
    request_id: str = Field("", max_length=128)


class DecisionFeedback(BaseModel):
    type: Literal["decision_feedback"] = "decision_feedback"
    decision_id: str
    useful: bool


class PlanConflict(BaseModel):
    file_path: str
    user_id: str
    agent: AgentKind = "unknown"
    kind: Literal["edited", "planned"]
    summary: str = ""
    seconds_ago: float
    e2e: E2EBlob = ""


class PlanConflicts(BaseModel):
    type: Literal["plan_conflicts"] = "plan_conflicts"
    conflicts: list[PlanConflict] = []
    # The plan's request_id ("" for a plan that sent none). Every plan gets
    # exactly one reply, even one the relay did not record.
    request_id: str = ""
    # Why the plan was not recorded ("rate_limited", "invalid"); "" when it
    # was, in which case conflicts is the full answer.
    reason: str = ""


class PlanUpdate(BaseModel):
    type: Literal["plan_update"] = "plan_update"
    user_id: str
    agent: AgentKind = "unknown"
    summary: str = ""
    files: list[str] = []
    symbols: list[str] = []
    repo_id: RepoId = ""
    branch: str = ""
    timestamp: float
    expires_at: float
    e2e: E2EBlob = ""


class DecisionFeedbackUpdate(BaseModel):
    type: Literal["decision_feedback_update"] = "decision_feedback_update"
    decision_id: str
    useful_count: int = 0
    noise_count: int = 0


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
    e2e: bool = False
    history_hours: float = 0
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


class JoinPending(BaseModel):
    """Sent to a joiner held at the door of an approval-required room. The
    socket stays open; the next frame is either the normal post-join traffic
    (approved) or room_rejected (declined, timed out, or the queue is full)."""

    type: Literal["join_pending"] = "join_pending"
    request_id: str
    reason: str


class JoinRequest(BaseModel):
    """Sent to every owner socket when someone waits for approval. name is
    "dashboard viewer" for a spectator. Answered with join_decision."""

    type: Literal["join_request"] = "join_request"
    request_id: str
    name: str
    agent: AgentKind = "unknown"
    timestamp: float = Field(default_factory=time.time)


class JoinResolved(BaseModel):
    """How a pending join request ended. Sent to every owner socket, so each
    owner's list stays current: approved/declined by an owner (or approved
    because approval was switched off), timed_out, or withdrawn (the joiner
    disconnected while waiting). Also sent to the joiner itself, with outcome
    "approved", the moment it is admitted: frames a joiner sends before that
    are discarded, so a client waits for it. Declined and timed-out joiners
    get room_rejected instead."""

    type: Literal["join_resolved"] = "join_resolved"
    request_id: str
    outcome: Literal["approved", "declined", "timed_out", "withdrawn"]


class JoinDecision(BaseModel):
    """Owner -> relay: admit or decline a pending joiner. Ignored from a
    socket that did not present the room's owner token."""

    type: Literal["join_decision"] = "join_decision"
    request_id: str
    approve: bool


class RoomPolicy(BaseModel):
    """Owner -> relay: change who may join. Ignored from non-owners."""

    type: Literal["room_policy"] = "room_policy"
    require_approval: bool


class RoomPolicyUpdate(BaseModel):
    """The room's join policy, broadcast when an owner changes it and sent in
    every spectator's catch-up."""

    type: Literal["room_policy_update"] = "room_policy_update"
    require_approval: bool


class RateLimited(BaseModel):
    type: Literal["rate_limited"] = "rate_limited"
    reason: str
    retry_after: int


class FileRequested(BaseModel):
    type: Literal["file_requested"] = "file_requested"
    file_path: str
    repo_id: RepoId = ""
    by: str
    agent: AgentKind = "unknown"
    timestamp: float = Field(default_factory=time.time)
    e2e: E2EBlob = ""


class FileReleased(BaseModel):
    type: Literal["file_released"] = "file_released"
    file_path: str
    repo_id: RepoId = ""
    timestamp: float = Field(default_factory=time.time)
    e2e: E2EBlob = ""


class HistoryChunk(BaseModel):
    type: Literal["history_chunk"] = "history_chunk"
    events: list[dict]
    done: bool


class HivelaneFeed(BaseModel):
    type: Literal["hivelane_feed"] = "hivelane_feed"
    events: list[dict]


# Every top-level wire message, by direction. protocol/generate.py and the
# contract tests enumerate these, so a message missing here is a message
# nothing checks.
CLIENT_TO_RELAY: tuple[type[BaseModel], ...] = (
    JoinMessage,
    ActivityMessage,
    IntentMessage,
    EditCheckRequest,
    PlanMessage,
    DecisionFeedback,
    JoinDecision,
    RoomPolicy,
    FileRequest,
    HistoryRequest,
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
    PlanConflicts,
    PlanUpdate,
    DecisionFeedbackUpdate,
    JoinPending,
    JoinRequest,
    JoinResolved,
    RoomPolicyUpdate,
    FileRequested,
    FileReleased,
    HistoryChunk,
    HivelaneFeed,
)
