"""Wire format for every WebSocket message, as the relay sees it.

Mirrored on purpose in hivelane/protocol.py (the daemon must not import the
relay package) and in frontend/src/types.ts. Mirrors drift, so the copies are
pinned by tests: server/tests/test_protocol_contract.py requires every message
here to have the same JSON schema as its hivelane/protocol.py twin and a
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
# and simply never ask permission, so conflict prevention would be silently off;
# the handshake rejects it instead.
PROTOCOL_VERSION = 3
MIN_SUPPORTED_PROTOCOL = 1
# Protocol 3 brought v3 e2e room keys (hivelane/crypto.py). An older member's
# hashes never match a v3 member's, so in an e2e room it would edit unchecked:
# the relay refuses it there instead (cleartext rooms still accept it).
MIN_E2E_PROTOCOL = 3

# Answer to "may I edit this file right now?", an agent's PreToolUse hook maps
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
DecisionScope = Literal["symbol", "lines", "file", "intent", "plan", "branch", "task", "pending"]

# How a peer is involved: edited the file, plans to, only mentioned it, or
# has unpushed changes to it.
PeerKind = Literal["edited", "planned", "intent", "pending"]


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
    # Fingerprint of the file version line_ranges are numbered in (the
    # content after this change). Line numbers are only compared between
    # equal versions; across versions (two checkouts) symbols decide. "" if
    # unknown (an older client).
    version: str = Field("", max_length=64)


# --- Client -> Server ---------------------------------------------------------


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
    # None marks a spectator (the dashboard), which receives broadcasts but is
    # not a room member and skips name validation entirely.
    name: Optional[str] = None
    agent: AgentKind = "unknown"
    # 0 means a client old enough not to declare one. Spectators are exempt:
    # the dashboard only consumes broadcasts and cannot silently under-report.
    protocol_version: int = 0
    repo_id: RepoId = ""
    # Optional: a member that carries a committed .hivelane.toml shares its
    # windows with the room. None leaves the room's current settings alone.
    settings: Optional[RoomSettings] = None
    # Secret returned once by POST /rooms. A join carrying the matching
    # token marks that socket as a room owner (may approve joins and set
    # the room policy). A wrong token is treated as no token.
    owner_token: Optional[str] = None
    # True when the client discovered this room code by querying GET /rooms/by-repo.
    # The relay enforces approval for any repo-bound room regardless of this flag so
    # a client cannot bypass approval by omitting it.
    via_repo_lookup: bool = False
    # The invite secret from a share link (`#invite=` in the dashboard URL,
    # personal config for the CLI). A join carrying the room's current one is
    # admitted without approval in rooms that require an invite. A wrong or
    # rotated secret is treated exactly like no secret.
    invite_secret: Optional[str] = None
    # The per-member token the relay sent in member_token after an earlier
    # admission (approved or by invite). Presenting it skips approval until
    # an owner revokes it. A wrong or revoked token counts as no token.
    member_token: Optional[str] = None

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
    # Members that are agents of the requester's own chat (the daemon's agent
    # lanes): their touches are a hand-off and are left out of the verdict,
    # and siblings' (two subagents of one chat) warn at most. Only the
    # requester's own decision changes, which its daemon could override anyway.
    same_chat: list[str] = []
    siblings: list[str] = []
    # Every name the requester's daemon speaks under in this room (its main
    # name and its agent lanes): reservations of tasks claimed by any of
    # them do not block it. Advisory, like same_chat.
    own_names: list[str] = []
    # Fingerprint of the file version line_ranges are numbered in (the
    # requester's current content); see FileChange.version.
    base_version: str = Field("", max_length=64)


class FileRequest(BaseModel):
    """Ask the member(s) holding a file for it, and join the file's
    wait queue (an edit_check that comes back block enqueues the sender
    too). The relay answers, when the file frees up and the sender is
    first in line, with file_released."""

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
    """Spectator -> relay: replay the room's kept events newer than
    since (0 = everything kept). Answered with history_chunk frames, the
    last with done=true; a room keeping no history answers with one
    empty, done chunk."""

    type: Literal["history_request"] = "history_request"
    since: float = 0


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
    through the debounced feed), an agent is blocked waiting on it."""

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
    so a busy agent's file probes don't drown the dashboard, the narrowed
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

# --- pending (unpushed) work ---------------------------------------------------
# Live touches say who is typing now; pending changes say whose work is not
# merged yet. A daemon reports, per repo, the files its user changed but has
# not pushed (uncommitted edits plus commits ahead of the upstream branch, or
# of the default branch's merge-base when there is no upstream). The relay
# keeps the latest report per (member, repo) for as long as it holds, not for
# the touch window, and warns (scope "pending") when someone else edits one of
# those files. In an e2e room paths, symbols, branch and base are h: hashes
# and the real values travel in the blob.

# Files per pending_changes report: a daemon with more sends the first ones
# and sets truncated; the relay keeps the first this many of a longer list.
PENDING_MAX_FILES = 200


class PendingFile(BaseModel):
    path: str
    # Definitions the unpushed changes touch, when the client could extract
    # them; empty means "the file" (judged file-level).
    symbols: list[str] = []
    # Unix seconds the file has been pending since: its oldest unpushed
    # commit, or when the client first saw it changed. 0 when unknown.
    since: float = 0


class PendingChanges(BaseModel):
    """Client -> relay: the sender's full set of unpushed files in repo_id,
    replacing its previous report. Sent when the set changes (and now and
    then unchanged, as a refresh); an empty files list clears it."""

    type: Literal["pending_changes"] = "pending_changes"
    user_id: str = ""
    repo_id: RepoId = ""
    # The local branch and the ref its work is measured against
    # ("origin/main"); "" when unknown.
    branch: str = ""
    base: str = ""
    files: list[PendingFile] = []
    # More files were pending than the report carries.
    truncated: bool = False
    e2e: E2EBlob = ""


class PendingUpdate(BaseModel):
    """Relay -> dashboards: a member's current unpushed files in repo_id (an
    empty list: none). online is False for the last report of a member that
    has since disconnected (kept, with its age, until it lapses)."""

    type: Literal["pending_update"] = "pending_update"
    user_id: str
    agent: AgentKind = "unknown"
    repo_id: RepoId = ""
    branch: str = ""
    base: str = ""
    files: list[PendingFile] = []
    truncated: bool = False
    timestamp: float
    online: bool = True
    e2e: E2EBlob = ""

# --- room tasks ----------------------------------------------------------------
# A shared task list per room: members create tasks, claim them under a lease,
# post notes and complete them. A claimed task's files are reserved: other
# members' edit_checks on them are blocked (scope "task") until the task is
# done, released or its lease lapses. In an e2e room titles, bodies and note
# text travel only inside e2e blobs and files are h: hashes.
#
# source "agent_plan": a step of a coding agent's own plan (a Claude Code todo
# list), created and kept in step by its daemon; `lane` names the agent lane
# that wrote it and `active` marks the step it is on. Such a task reserves its
# files only while active, and `claim` with next=true never picks one.
# A task with repo_id "" belongs to every repo in the room.

TaskStatus = Literal["open", "claimed", "done", "cancelled"]
TaskOp = Literal["create", "claim", "renew", "note", "done", "release", "cancel", "list", "update"]
TaskSource = Literal["manual", "agent_plan"]


class TaskNote(BaseModel):
    user_id: str
    text: str = ""
    timestamp: float
    # "note", or the op that wrote it: "done" (the result) or "cancel".
    kind: Literal["note", "done", "cancel"] = "note"
    e2e: E2EBlob = ""


class Task(BaseModel):
    task_id: str
    repo_id: RepoId = ""
    title: str = ""
    body: str = ""
    files: list[str] = []
    depends_on: list[str] = []
    status: TaskStatus = "open"
    created_by: str
    created_at: float
    updated_at: float
    claimer: Optional[str] = None
    claimed_at: Optional[float] = None
    lease_until: Optional[float] = None
    notes: list[TaskNote] = []
    e2e: E2EBlob = ""
    source: TaskSource = "manual"
    lane: str = ""
    active: bool = False
    # A member the creator thinks should take it; claim next=true offers it
    # to them first. Advisory: anyone may still claim it.
    suggested_for: str = ""
    # Non-empty when a dashboard created the task: a public tag of that
    # dashboard's secret task key (see TaskRequest.dashboard_key), so the
    # dashboard can tell its own tasks. created_by is then the name its
    # viewer typed, which nobody verified.
    dashboard_tag: str = ""


class TaskRequest(BaseModel):
    """One operation on the room's task list. Every request gets exactly one
    task_reply with the same request_id."""

    type: Literal["task"] = "task"
    request_id: str = Field(..., max_length=128)
    op: TaskOp
    task_id: str = Field("", max_length=16)
    # claim: the oldest ready task of repo_id instead of task_id.
    next: bool = False
    title: str = ""
    body: str = ""
    files: list[str] = []
    depends_on: list[str] = []
    repo_id: RepoId = ""
    lease_seconds: float = Field(1800, ge=60, le=28800)
    # note / done / cancel: the text recorded on the task.
    text: str = ""
    # list: done and cancelled tasks too.
    include_closed: bool = False
    e2e: E2EBlob = ""
    # create: claim it for yourself at once (under lease_seconds).
    claim: bool = False
    source: TaskSource = "manual"
    lane: str = Field("", max_length=64)
    # create / update: the step the claimer is on now (agent_plan tasks).
    # update leaves it as it is when None.
    active: Optional[bool] = None
    suggested_for: str = Field("", max_length=64)
    # From a dashboard only: the name to credit (create) and the dashboard's
    # own random secret, which create records and cancel must present.
    as_name: str = Field("", max_length=64)
    dashboard_key: str = Field("", max_length=128)


class TaskReply(BaseModel):
    type: Literal["task_reply"] = "task_reply"
    request_id: str
    ok: bool
    # Why not ("not_found", "not_ready", "claimed", "not_claimer", "not_owner",
    # "closed", "none_ready", "limit", "rate_limited", "invalid",
    # "not_allowed"); "" when ok.
    reason: str = ""
    task: Optional[Task] = None
    tasks: list[Task] = []


class TaskUpdate(BaseModel):
    """A task changed (dashboards only: members ask with op "list")."""

    type: Literal["task_update"] = "task_update"
    task: Task


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
    history_hours: float = 0
    set_by: Optional[str] = None
    timestamp: float = Field(default_factory=time.time)
    # When the room was created and when its code stops working (the relay
    # keeps codes for a week). 0 from a relay that predates room expiry.
    created_at: float = 0
    expires_at: float = 0


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

class MemberToken(BaseModel):
    """Relay -> a joiner just admitted by approval or by invite link: a
    per-member secret to present as join.member_token next time, so a
    reconnect does not ask an owner again. Sent once; the relay keeps only
    its hash. member_id names the grant (what an owner revokes)."""

    type: Literal["member_token"] = "member_token"
    member_id: str
    token: str


class RememberedMember(BaseModel):
    """One remembered person in room_access: who holds a member token."""

    member_id: str
    name: str
    # "member" for a daemon, "dashboard" for a dashboard viewer.
    kind: Literal["member", "dashboard"] = "member"
    issued_at: float
    # True while a socket admitted with this grant is connected.
    online: bool = False


class RoomAccess(BaseModel):
    """Relay -> owner sockets only: the room's invite link secret (so an
    owner can show the share link again) and the people it remembers. Sent in
    an owner's catch-up and after every change (rotation, a new remembered
    member, a revocation). invite_secret is None for rooms without one
    (created before invite links), where the code alone still admits."""

    type: Literal["room_access"] = "room_access"
    invite_required: bool
    invite_secret: Optional[str] = None
    members: list[RememberedMember] = Field(default_factory=list)


class InviteRotate(BaseModel):
    """Owner -> relay: replace the invite secret; old links stop admitting
    (their holders wait for approval). Remembered members stay. On a room
    without invite links this switches them on. Ignored from non-owners."""

    type: Literal["invite_rotate"] = "invite_rotate"


class MemberRevoke(BaseModel):
    """Owner -> relay: forget a remembered member (its token stops working)
    and disconnect any socket it is connected with. Ignored from non-owners."""

    type: Literal["member_revoke"] = "member_revoke"
    member_id: str = Field(max_length=64)


class RateLimited(BaseModel):
    """Sent once, before closing with 4429, to a caller over the per-IP
    connection-attempt budget. Arrives before any join, so spectators and
    daemons alike must handle it."""

    type: Literal["rate_limited"] = "rate_limited"
    reason: str
    retry_after: int


class FileRequested(BaseModel):
    """Sent to each member currently holding file_path (its latest touch
    is inside the block window) when someone asks for it: once per
    requester per stay in the queue, whether the ask was an explicit
    file_request or a blocked edit_check. by is the requester's name."""

    type: Literal["file_requested"] = "file_requested"
    file_path: str
    repo_id: RepoId = ""
    by: str
    agent: AgentKind = "unknown"
    timestamp: float = Field(default_factory=time.time)
    e2e: E2EBlob = ""


class FileReleased(BaseModel):
    """Sent to the first requester in a file's queue when the file frees
    up: every holder's latest touch aged past the block window, or the
    holders left. The recipient gets a short grace period in which later
    requesters stay blocked behind it; then the next in line is released."""

    type: Literal["file_released"] = "file_released"
    file_path: str
    repo_id: RepoId = ""
    timestamp: float = Field(default_factory=time.time)
    e2e: E2EBlob = ""
    # Why the file is ours now when it was not the holder letting go:
    # "owner" when a room owner released the hold or let us go first.
    reason: str = ""


# --- Resolve actions (owner dashboard -> relay) ---------------------------------
#
# Three things an owner can do about a collision from the dashboard. Each is
# ignored from a socket that did not present the room's owner token, and
# counts against the spectator message budget like every dashboard frame.

# A ping's text is shown in an agent's chat and a desktop notification: short.
NUDGE_MAX_CHARS = 280
# Member names are bounded by the join's display-name rule; this is a
# relay-side memory bound for the frames that name one.
USER_ID_MAX_CHARS = 64


class HoldRelease(BaseModel):
    """Owner -> relay: let a held (blocked) edit go ahead. decision_id names
    the block (EditDecision.decision_id). The relay puts that member first in
    the file's wait queue, lets its edits through the holders' touches for a
    short while, sends it file_released and broadcasts hold_released."""

    type: Literal["hold_release"] = "hold_release"
    decision_id: str = Field(..., max_length=64)


class QueueOrder(BaseModel):
    """Owner -> relay: settle who goes first on a contested file. The named
    member moves to the front of the file's queue (and is let through the
    others' touches for a short while); everyone else involved waits behind
    it with a queue_position."""

    type: Literal["queue_order"] = "queue_order"
    file_path: str = Field(..., max_length=4096)
    repo_id: RepoId = ""
    first_user_id: str = Field(..., max_length=USER_ID_MAX_CHARS)


class Nudge(BaseModel):
    """Owner -> relay: ping one member's agent. The relay hands it to that
    member as `nudged`; its daemon shows it in the agent's next hook output
    and as a desktop notification."""

    type: Literal["nudge"] = "nudge"
    to_user_id: str = Field(..., max_length=USER_ID_MAX_CHARS)
    text: str = Field("", max_length=NUDGE_MAX_CHARS)


class Nudged(BaseModel):
    """Relay -> the pinged member (point to point) and the dashboards (feed):
    an owner pinged user_id. delivered is False when the member was not
    connected to receive it."""

    type: Literal["nudged"] = "nudged"
    user_id: str
    by: str = "room owner"
    text: str = ""
    delivered: bool = True
    timestamp: float = Field(default_factory=time.time)


class HoldReleased(BaseModel):
    """Relay -> feed: an owner released decision_id's hold; user_id may now
    edit file_path. Carries the held edit's e2e blob so encrypted rooms can
    show the path."""

    type: Literal["hold_released"] = "hold_released"
    decision_id: str
    user_id: str
    file_path: str
    repo_id: RepoId = ""
    by: str = "room owner"
    timestamp: float = Field(default_factory=time.time)
    e2e: E2EBlob = ""


class QueueReordered(BaseModel):
    """Relay -> feed: an owner let first_user_id go first on file_path;
    waiting lists everyone else in line, in order."""

    type: Literal["queue_reordered"] = "queue_reordered"
    file_path: str
    repo_id: RepoId = ""
    first_user_id: str
    waiting: list[str] = []
    timestamp: float = Field(default_factory=time.time)
    e2e: E2EBlob = ""


class HistoryChunk(BaseModel):
    """Relay -> spectator answer to history_request: kept feed events in
    timestamp order, in chunks; done marks the last chunk."""

    type: Literal["history_chunk"] = "history_chunk"
    events: list[dict]
    done: bool


class HivelaneFeed(BaseModel):
    """Debounced room broadcast: one WebSocket frame every ~500ms carrying
    all queued events for that room (the dashboard's Hivelane feed)."""

    type: Literal["hivelane_feed"] = "hivelane_feed"
    events: list[dict]


def placeholder_caption(message: ActivityMessage) -> str:
    """The activity_update caption: one deterministic line built from the
    message alone, no network call."""
    count = len(message.changes)
    if count == 0:
        if message.prompt:
            return f"{message.user_id}'s agent received a new prompt"
        return f"{message.user_id}'s agent is idle"
    if count == 1:
        return f"{message.user_id}'s agent is editing {message.changes[0].file_path}"
    return f"{message.user_id}'s agent is editing {count} files"
