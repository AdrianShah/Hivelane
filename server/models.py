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


# --- Client -> Server ---------------------------------------------------------


class RoomSettings(BaseModel):
    """Team-wide conflict windows from the repo's .synergo.toml.

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
    # Secret returned once by POST /rooms. A join carrying the matching
    # token marks that socket as a room owner (may approve joins and set
    # the room policy). A wrong token is treated as no token.
    owner_token: Optional[str] = None

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
    branch: str = ""
    base_commit: str = ""
    e2e: E2EBlob = ""


class IntentMessage(BaseModel):
    """What the agent is currently trying to do, derived from the user's prompt.
    Declared up front so peers can be warned before they collide with it."""

    type: Literal["intent"] = "intent"
    user_id: str
    prompt: str
    timestamp: float = Field(default_factory=time.time)
    repo_id: RepoId = ""
    e2e: E2EBlob = ""


class EditCheckRequest(BaseModel):
    """Pre-edit permission probe. request_id correlates the response on an
    agent's blocking hook."""

    type: Literal["edit_check"] = "edit_check"
    request_id: str
    file_path: str
    symbol: Optional[str] = None
    repo_id: RepoId = ""
    symbols: list[str] = []
    line_ranges: list[tuple[int, int]] = []
    branch: str = ""
    e2e: E2EBlob = ""


# --- Server -> Client --------------------------------------------------------


class PeerTouch(BaseModel):
    user_id: str
    agent: AgentKind = "unknown"
    seconds_ago: float
    prompt: Optional[str] = None
    symbols: list[str] = []
    branch: str = ""
    kind: PeerKind = "edited"
    e2e: E2EBlob = ""


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


class EditCheckResponse(BaseModel):
    """Point-to-point answer to an EditCheckRequest. Sent immediately (never
    through the debounced feed) — an agent is blocked waiting on it."""

    type: Literal["edit_check_response"] = "edit_check_response"
    request_id: str
    decision: Decision
    file_path: str
    others: list[PeerTouch] = []
    reason: str = ""
    scope: DecisionScope = "file"
    decision_id: str = ""


class IntentUpdate(BaseModel):
    type: Literal["intent_update"] = "intent_update"
    user_id: str
    agent: AgentKind = "unknown"
    prompt: str
    timestamp: float
    repo_id: RepoId = ""
    e2e: E2EBlob = ""


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
    scope: DecisionScope = "file"
    decision_id: str = ""
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
    e2e: bool = False
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
