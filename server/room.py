"""In-memory state for one workspace ("room"). A process may hold many Room
instances at once (see room_manager.RoomManager), each is fully isolated.
Ephemeral by design: the room *code* is durable (store.RoomStore), but nothing
in a Room survives a relay restart; it re-forms from live activity, and every
touch older than the conflict window would be ignored anyway."""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Callable, Optional

from fastapi import WebSocket
from pydantic import BaseModel

try:
    from . import config, suggest
    from .file_queue import (
        CLAIM_MAX_SECONDS,
        MAX_QUEUED_PATH_CHARS,
        SWEEP_INTERVAL_SECONDS as QUEUE_SWEEP_INTERVAL_SECONDS,
        FileQueues,
        QueueEntry,
        QueueKey,
        QueueNotice,
    )
    from .history import RoomHistory
    from .limits import TokenBucket
    from .models import (
        ActivityMessage,
        DecisionFeedbackUpdate,
        DecisionScope,
        FileReleased,
        FileRequested,
        JoinRequest,
        JoinResolved,
        PeerKind,
        PlanConflict,
        PlanMessage,
        PlanUpdate,
        ActivityUpdate,
        AgentKind,
        Decision,
        IntentUpdate,
        RoomPolicyUpdate,
        RoomSettings,
        RoomSettingsUpdate,
    )
except ImportError:  # Render rootDir=server
    import config  # type: ignore[no-redef]
    import suggest  # type: ignore[no-redef]
    from file_queue import (  # type: ignore[no-redef]
        CLAIM_MAX_SECONDS,
        MAX_QUEUED_PATH_CHARS,
        SWEEP_INTERVAL_SECONDS as QUEUE_SWEEP_INTERVAL_SECONDS,
        FileQueues,
        QueueEntry,
        QueueKey,
        QueueNotice,
    )
    from history import RoomHistory  # type: ignore[no-redef]
    from limits import TokenBucket  # type: ignore[no-redef]
    from models import (
        ActivityMessage,
        DecisionFeedbackUpdate,
        DecisionScope,
        FileReleased,
        FileRequested,
        JoinRequest,
        JoinResolved,
        PeerKind,
        PlanConflict,
        PlanMessage,
        PlanUpdate,
        ActivityUpdate,
        AgentKind,
        Decision,
        IntentUpdate,
        RoomPolicyUpdate,
        RoomSettings,
        RoomSettingsUpdate,
    )

log = logging.getLogger(__name__)

# Observers of room events (server/webhooks.py registers one): called with
# (room, payload) for every room-wide broadcast as it is queued, and for every
# join_request sent to owners. Synchronous and expected to be cheap; one that
# raises is logged and skipped, so a notification bug cannot stall the feed.
EventSink = Callable[["Room", dict], None]
_event_sinks: list[EventSink] = []


def add_event_sink(sink: EventSink) -> None:
    if sink not in _event_sinks:
        _event_sinks.append(sink)


def remove_event_sink(sink: EventSink) -> None:
    if sink in _event_sinks:
        _event_sinks.remove(sink)

# Defaults for a room nobody has configured. A room's own RoomSettings (from
# POST /rooms or a member's committed .hivelane.toml) replace these per room.
#
# How long a peer's last touch of a file stays eligible for conflict comparison
# (the "warn" window).
CONFLICT_WINDOW_SECONDS = 60.0

# A touch past the comparison window is invisible to every reader, so it is swept
# instead of being kept for the life of the room. Tied to the window on purpose:
# widening one without the other would start pruning touches still in use. A
# room with a wider warn window retains touches for that long instead (see
# Room.retention_seconds); a narrower one keeps this floor, so narrowing the
# window and widening it again does not lose touches in between.
ACTIVITY_RETENTION_SECONDS = CONFLICT_WINDOW_SECONDS
ACTIVITY_PRUNE_INTERVAL_SECONDS = 60.0

# Inside this window a peer is presumed mid-edit, so an incoming edit is denied
# outright rather than merely flagged.
EDIT_BLOCK_WINDOW_SECONDS = 20.0

# How long a declared intent still describes what a peer is doing. Longer than
# the touch window because stating a task precedes editing, but bounded: an
# unbounded intent warns about the same file for the rest of the session.
# Not a room setting: it describes how long a stated task stays current, not
# how close two edits may land.
INTENT_WINDOW_SECONDS = 300.0

# Debounce room-wide broadcasts into a single Hivelane feed frame.
BROADCAST_FLUSH_SECONDS = 0.5

# Two line-range edits closer than this are treated as the same hunk: git's
# merge context makes near-adjacent edits conflict too.
LINE_PROXIMITY = 10

# The symbol a client sends for an import-block edit. It intersects only with
# itself: two agents both adding imports collide, but one agent's import line
# says nothing about a teammate editing an unrelated function.
IMPORTS_SYMBOL = "<imports>"


def _env_number(name: str, default: float) -> float:
    """A relay-core knob from the environment; a bad value keeps the default."""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        logging.getLogger(__name__).warning("Ignoring %s=%r: not a number", name, raw)
        return default
    return value if value >= 0 else default


# How long one outbound frame may take to reach one socket. A socket that
# does not drain in time is dropped (closed) rather than allowed to hold up
# the rest of the room. HIVELANE_SEND_TIMEOUT_SECONDS.
SEND_TIMEOUT_SECONDS = _env_number("HIVELANE_SEND_TIMEOUT_SECONDS", 2.0)

# Byte budget for one outbound frame. The daemon's websockets client refuses
# frames over 1 MiB (and reconnects into the same traffic), so feeds are split
# and derived e2e copies trimmed to stay under this.
OUTBOUND_FRAME_BUDGET = int(_env_number("HIVELANE_OUTBOUND_FRAME_BYTES", 768 * 1024))

# Per-room bounds on the touch table (files x members), so no member can grow
# relay memory without limit: at most this many touches (oldest evicted), and
# at most this many bytes of retained diffs and e2e blobs. 0 = unlimited.
MAX_TOUCHES_PER_ROOM = int(_env_number("HIVELANE_MAX_TOUCHES_PER_ROOM", 5000))
MAX_TOUCH_BYTES_PER_ROOM = int(_env_number("HIVELANE_MAX_TOUCH_BYTES_PER_ROOM", 64 * 1024 * 1024))
# A touch keeps only this much of its diff: the diff is only read for Gemini
# conflict classification, which uses the first 2,500 characters anyway.
STORED_DIFF_BYTES = 4 * 1024
# Paths longer than this are not tracked at all (real paths are far shorter;
# an e2e hash is 34 characters). HIVELANE_MAX_PATH_CHARS.
MAX_TRACKED_PATH_CHARS = config.MAX_PATH_CHARS
# Symbols and line ranges a merged touch remembers (newest kept).
MAX_SYMBOLS_PER_TOUCH = 256
MAX_RANGES_PER_TOUCH = 256
# Plans and intents kept per member (one per repo; oldest evicted).
MAX_PLANS_PER_MEMBER = 8
MAX_INTENTS_PER_MEMBER = 8
# Joiners from one address that may wait for approval at once.
MAX_PENDING_JOINS_PER_IP = int(_env_number("HIVELANE_MAX_PENDING_JOINS_PER_IP", 2))

# Feedback is kept for this many recent decisions per room (LRU).
# HIVELANE_MAX_TRACKED_DECISIONS; 0 = unlimited.
MAX_TRACKED_DECISIONS = config.MAX_TRACKED_DECISIONS

# Joiners that may wait for an owner's approval at once, per room. Bounded so a
# script holding sockets open cannot grow the queue (or the owners' screens).
# HIVELANE_MAX_PENDING_JOINS; 0 = unlimited.
MAX_PENDING_JOINS = config.MAX_PENDING_JOINS

# Bytes of randomness in an owner token (POST /rooms returns it once).
OWNER_TOKEN_BYTES = 32

# Paths this room has seen (edit checks, file requests), remembered per
# (repo_id, path) for edit-check suggestions. Bounded LRU.
# HIVELANE_MAX_KNOWN_PATHS; 0 = unlimited.
MAX_KNOWN_PATHS = config.MAX_KNOWN_PATHS


def new_owner_token() -> str:
    """A fresh owner secret: 32 random bytes, URL-safe base64 (43 characters)."""
    return secrets.token_urlsafe(OWNER_TOKEN_BYTES)


def hash_owner_token(token: str) -> str:
    """What the relay keeps instead of the token: its SHA-256, hex.

    Unsalted on purpose: the token is 256 random bits, so there is nothing to
    precompute, and a stolen hash cannot be replayed as a token.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()

# Files where "different function" is no comfort: manifests, lockfiles and
# config are edited as a whole, so they are always judged at file level.
_ALWAYS_WARN_BASENAMES = (
    "package.json",
    "pyproject.toml",
    "requirements*.txt",
    "*.lock",
    "go.mod",
    "Cargo.toml",
    "tsconfig*.json",
    ".env*",
    "Dockerfile",
    ".hivelane.toml",
)


def is_always_warn_file(file_path: str) -> bool:
    """True for imports/config files that are never judged by symbol."""
    parts = file_path.replace("\\", "/").split("/")
    basename = parts[-1]
    if any(fnmatch.fnmatchcase(basename, pattern) for pattern in _ALWAYS_WARN_BASENAMES):
        return True
    return ".github" in parts[:-1] and basename.endswith((".yml", ".yaml"))


def symbols_intersect(a: list[str], b: list[str]) -> bool:
    """True when two symbol lists share a name. "<imports>" is a name like any
    other: it matches only "<imports>" on the other side."""
    if not a or not b:
        return False
    return not set(a).isdisjoint(b)


def plan_symbols_intersect(a: list[str], b: list[str]) -> bool:
    """symbols_intersect for plans, which are not tied to one file: an import
    edit is file-local, so "<imports>" never matches across plans or files."""
    return symbols_intersect(
        [s for s in a if s != IMPORTS_SYMBOL], [s for s in b if s != IMPORTS_SYMBOL]
    )


def _merged_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for start, end in sorted((min(a, b), max(a, b)) for a, b in ranges):
        if out and start <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], end))
        else:
            out.append((start, end))
    return out


def ranges_near(
    a: list[tuple[int, int]], b: list[tuple[int, int]], proximity: int = LINE_PROXIMITY
) -> bool:
    """True when any range of a overlaps or lies within proximity lines of b.

    Sorted sweep, O((n + m) log(n + m)): the queue sweep calls this for every
    waiting entry against every touch, so the quadratic loop it replaced
    could cost seconds per frame with 256 ranges a side."""
    if not a or not b:
        return False
    xs = _merged_ranges(a)
    ys = _merged_ranges(b)
    i = j = 0
    while i < len(xs) and j < len(ys):
        a_start, a_end = xs[i]
        b_start, b_end = ys[j]
        if a_start <= b_end + proximity and b_start <= a_end + proximity:
            return True
        if a_end < b_end:
            i += 1
        else:
            j += 1
    return False


def _branches_differ(a: str, b: str) -> bool:
    # Empty on either side means "unknown": treated as the same branch.
    return bool(a) and bool(b) and a != b


DEFAULT_SETTINGS = RoomSettings(
    block_window_seconds=EDIT_BLOCK_WINDOW_SECONDS,
    warn_window_seconds=CONFLICT_WINDOW_SECONDS,
)


def settings_problem(settings: RoomSettings) -> Optional[str]:
    """Why these settings cannot be enforced, or None if they can.

    The per-field bounds live on the model; this is the cross-field rule a
    model field cannot express. A block window wider than the warn window would
    block on touches that check_edit never looks at.
    """
    if settings.warn_window_seconds < settings.block_window_seconds:
        return (
            f"warn window ({settings.warn_window_seconds:g}s) is shorter than "
            f"block window ({settings.block_window_seconds:g}s)"
        )
    return None


@dataclass
class FileTouch:
    user_id: str
    file_path: str
    diff: str
    prompt: Optional[str]
    timestamp: float
    agent: AgentKind = "unknown"
    # Which repository the path is relative to (see models.RepoId). Two touches
    # conflict only when both the path and the repo_id match.
    repo_id: str = ""
    symbols: list[str] = field(default_factory=list)
    line_ranges: list[tuple[int, int]] = field(default_factory=list)
    branch: str = ""
    # How the peer is involved: an edit, a declared plan, or an intent mention.
    kind: PeerKind = "edited"
    # The e2e blob of the message this touch came from, copied verbatim onto
    # the PeerTouch / PlanConflict a peer receives (see models.E2EBlob).
    e2e: str = ""
    # When each symbol / line range was last edited. A peer's touch is the
    # union of its batches inside the window, so a block on a function edited
    # two batches ago is not lost when the latest batch edits another one.
    # Empty for touches built elsewhere (plans, intents): then every symbol
    # counts as edited at `timestamp`.
    symbol_seen: dict[str, float] = field(default_factory=dict)
    range_seen: list[tuple[int, int, float]] = field(default_factory=list)
    # Bytes this touch holds in the room's touch budget.
    cost: int = 0

    def symbol_time(self, names: set[str]) -> float:
        """When the newest of `names` (all in self.symbols) was edited."""
        return max(
            (self.symbol_seen.get(name, self.timestamp) for name in names),
            default=self.timestamp,
        )

    def ranges_time(self, ranges: list[tuple[int, int]], proximity: int) -> Optional[float]:
        """When the newest of this touch's ranges near `ranges` was edited,
        or None when none is near."""
        if not ranges_near(ranges, self.line_ranges, proximity):
            return None
        if not self.range_seen:
            return self.timestamp
        times = [
            seen
            for start, end, seen in self.range_seen
            if ranges_near(ranges, [(start, end)], proximity)
        ]
        return max(times) if times else None


@dataclass
class EditVerdict:
    """Room.evaluate_edit's answer: the decision, the peers that justified it
    (most relevant first) and the granularity at which it was decided."""

    decision: Decision
    peers: list[FileTouch]
    scope: DecisionScope = "file"


@dataclass
class QueueOutcome:
    """Room.queue_edit_check's answer: the decision after the file's wait
    queue had its say. reason is set only when the queue itself blocked (the
    usual reason sentence describes touches, which then did not block)."""

    decision: Decision
    peers: list[FileTouch]
    scope: DecisionScope = "file"
    reason: str = ""
    # 1-based place in the file's line; 0 = not queued.
    queue_position: int = 0
    wait: Optional[suggest.WaitHint] = None
    # file_requested frames for the holders, to send after the reply.
    notices: list[QueueNotice] = field(default_factory=list)


@dataclass
class _DecisionVotes:
    scope: DecisionScope
    useful: int = 0
    noise: int = 0
    voters: set[str] = field(default_factory=set)


@dataclass
class FileOverlap:
    """One file that the current user and another active user both touched recently."""

    file_path: str
    current_diff: str
    other: FileTouch


def _trim_per_member(store: dict, user_id: str, limit: int) -> None:
    """Keep at most `limit` (user_id, repo) entries for user_id in an
    insertion-ordered dict, dropping its oldest."""
    keys = [key for key in store if key[0] == user_id]
    for key in keys[: max(0, len(keys) - limit)]:
        del store[key]


_MENTION_CACHE: OrderedDict[tuple[str, ...], re.Pattern[str]] = OrderedDict()


def _mention_pattern(needles: tuple[str, ...]) -> re.Pattern[str]:
    """A regex matching any needle as a whole word (or path segment)."""
    pattern = _MENTION_CACHE.get(needles)
    if pattern is None:
        alternatives = "|".join(re.escape(needle) for needle in needles)
        pattern = re.compile(rf"(?<![a-z0-9_])(?:{alternatives})(?![a-z0-9_])")
        _MENTION_CACHE[needles] = pattern
        while len(_MENTION_CACHE) > 1024:
            _MENTION_CACHE.popitem(last=False)
    return pattern


def _dumps(payload: object) -> str:
    """One frame's text, exactly as Starlette's send_json would write it."""
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def _settle(decision: asyncio.Future, approve: bool) -> None:
    if not decision.done():
        decision.set_result(approve)


@dataclass
class PendingJoin:
    """Someone held at the door of an approval-required room.

    `decision` resolves True (admit) or False (decline) when an owner answers,
    or True when the policy is switched off (everyone waiting is admitted).
    """

    request_id: str
    name: str
    agent: AgentKind
    created_at: float
    decision: asyncio.Future
    # Where the joiner connects from (bounds pending joins per address).
    ip: str = ""

    def request(self) -> JoinRequest:
        return JoinRequest(
            request_id=self.request_id,
            name=self.name,
            agent=self.agent,
            timestamp=self.created_at,
        )


class Room:
    def __init__(self, settings: Optional[RoomSettings] = None) -> None:
        # Conflict windows in force. Never None and always valid: callers that
        # load settings from elsewhere validate them first (apply_settings).
        self._settings = (
            DEFAULT_SETTINGS.model_copy() if settings is None else settings.model_copy()
        )
        # Who last changed the settings, and when (for room_settings frames).
        self._settings_set_by: Optional[str] = None
        self._settings_updated_at = time.time()
        self._members: dict[str, WebSocket] = {}
        self._member_agents: dict[str, AgentKind] = {}
        self._spectators: set[WebSocket] = set()
        # file_path -> {(repo_id, user_id): FileTouch}, latest edit per user
        # per file per repo. Together the two keys make a touch's identity
        # (repo_id, file_path): the same relative path in two different repos
        # is two different files. The path stays the outer key so a lookup by
        # path is still one dict hit.
        self._file_activity: dict[str, dict[tuple[str, str], FileTouch]] = {}
        # Every touch, least recently updated first, keyed (path, repo, user):
        # the eviction order for the per-room touch bounds.
        self._touch_order: OrderedDict[tuple[str, str, str], None] = OrderedDict()
        self._touch_bytes = 0
        # Message-clock timestamp of the last sweep (see _prune_file_activity).
        self._last_prune_at = 0.0
        # Last broadcast activity per (user, repo) (for spectator catch-up).
        self._latest_updates: dict[tuple[str, str], ActivityUpdate] = {}
        # Last declared intent per (user, repo) (for spectator catch-up and
        # the edit-check escalation heuristic). Per repo: a daemon serves
        # several sessions and repos, and one repo's intent must not erase
        # another's.
        self._latest_intents: dict[tuple[str, str], IntentUpdate] = {}
        # Latest declared plan per (member, repo), for the same reason.
        self._plans: dict[tuple[str, str], PlanUpdate] = {}
        # decision_id -> votes, least recently used first; bounded.
        self._decisions: OrderedDict[str, _DecisionVotes] = OrderedDict()
        # (user_id, channel, scope) -> last accepted time.monotonic(); each
        # channel (and each repo within one) is limited independently (see
        # is_activity_rate_limited).
        self._last_activity_at: dict[tuple[str, str, str], float] = {}
        # Queued room broadcasts → flushed as one hivelane_feed every 500ms.
        # Appending never awaits; deliveries are serialized by _deliver_lock
        # (so each socket sees feeds in order), which no broadcast() waits on.
        self._broadcast_queue: list[dict] = []
        self._flush_task: Optional[asyncio.Task] = None
        self._deliver_lock = asyncio.Lock()
        # Sockets that missed a send deadline: skipped by every later send
        # and being closed; their handler's own cleanup removes them.
        self._stalled: set[WebSocket] = set()
        # Sockets still receiving their catch-up: feed frames for them are
        # held here and sent, in order, once the catch-up is done.
        self._catching_up: dict[WebSocket, list[str]] = {}
        # Connections inside websocket_endpoint that are not (yet) members or
        # spectators: mid-handshake, or waiting for approval. A room with any
        # is not empty, so it is not evicted from under them.
        self.connecting = 0
        # True once anyone has completed a join (RoomManager evicts rooms that
        # nobody ever joined after a short TTL).
        self.ever_joined = False
        # Coalesced activity broadcasts held back by the rate limit, per
        # (user, repo), and the tasks that will publish them (server/main.py).
        self.deferred_activity: dict[tuple[str, str], ActivityMessage] = {}
        # Background caption tasks, chained per (user, repo) so captioned
        # updates go out in the order their activity arrived.
        self.caption_tail: dict[tuple[str, str], asyncio.Task] = {}
        # Gemini calls running for this room (server/main.py caps them).
        self.gemini_in_flight = 0
        # --- access control (see the "access" section) ---
        # SHA-256 of the owner token POST /rooms returned; None for rooms
        # created before owner tokens, which therefore have no owners.
        self._owner_hash: Optional[str] = None
        self._require_approval = False
        # Sockets (members or spectators) that presented the owner token.
        self._owner_sockets: set[WebSocket] = set()
        # request_id -> joiner waiting for approval, oldest first.
        self._pending: OrderedDict[str, PendingJoin] = OrderedDict()
        # Inbound member messages per minute, summed over every member in the
        # room. None when ROOM_MESSAGE_LIMIT is 0 (unlimited). Edit checks
        # and spectators have budgets of their own (edit_check_quota,
        # spectator_quota), so neither a dashboard nor a noisy member's
        # activity can starve the edit checks agents block on.
        self.message_quota: Optional[TokenBucket] = (
            TokenBucket(config.ROOM_MESSAGE_LIMIT) if config.ROOM_MESSAGE_LIMIT > 0 else None
        )
        self._edit_check_quotas: dict[str, TokenBucket] = {}
        # The room code, set by RoomManager ("" for a Room made elsewhere).
        self.code = ""
        # Opt-in replay buffer of broadcast feed events (history_hours > 0).
        self.history = RoomHistory(self._settings.history_hours)
        # Who waits for which file (see file_queue.py), the task sweeping
        # them while any exist, and the paths seen here (suggestions).
        self.file_queues = FileQueues()
        self._queue_sweeper: Optional[asyncio.Task] = None
        self._known_paths: OrderedDict[tuple[str, str], None] = OrderedDict()

    # --- membership ----------------------------------------------------------

    def is_taken(self, user_id: str) -> bool:
        return user_id in self._members

    def is_active(self, user_id: str) -> bool:
        """True if user_id is still connected. Intentionally the same check as
        is_taken: a disconnected peer's last touch is excluded from conflict
        comparisons (recent_other_touches(active_only=True)) even if it
        happened moments ago, there's no point flagging a conflict with
        someone who already left. Not a bug; do not "fix" by tracking a grace
        period unless product behavior changes."""
        return user_id in self._members

    @property
    def member_ids(self) -> list[str]:
        return list(self._members)

    @property
    def member_count(self) -> int:
        return len(self._members)

    @property
    def spectator_count(self) -> int:
        return len(self._spectators)

    @property
    def is_empty(self) -> bool:
        """True once no members, spectators, connections still joining or
        joiners waiting for approval remain, safe to evict from the
        RoomManager."""
        return (
            not self._members
            and not self._spectators
            and self.connecting <= 0
            and not self._pending
        )

    def add_member(
        self, user_id: str, socket: WebSocket, agent: AgentKind = "unknown"
    ) -> None:
        self._members[user_id] = socket
        self._member_agents[user_id] = agent
        self.ever_joined = True

    def member_agent(self, user_id: str) -> AgentKind:
        return self._member_agents.get(user_id, "unknown")

    def member_socket(self, user_id: str) -> Optional[WebSocket]:
        return self._members.get(user_id)

    def remove_member(self, user_id: str, socket: Optional[WebSocket] = None) -> bool:
        """Deregister user_id and everything it left behind. With `socket`,
        only when that socket is still the one registered under the name: a
        handler whose connection was replaced must not evict its successor.
        Returns True when the member was removed."""
        if socket is not None:
            self._stalled.discard(socket)
            self._catching_up.pop(socket, None)
            if self._members.get(user_id) is not socket:
                return False
        current = self._members.pop(user_id, None)
        if current is not None:
            self._owner_sockets.discard(current)
            self._stalled.discard(current)
            self._catching_up.pop(current, None)
        self._member_agents.pop(user_id, None)
        for store in (self._latest_updates, self._latest_intents, self._plans):
            for key in [k for k in store if k[0] == user_id]:
                del store[key]
        for key in [k for k in self._last_activity_at if k[0] == user_id]:
            self._last_activity_at.pop(key, None)
        for key in [k for k in self.deferred_activity if k[0] == user_id]:
            self.deferred_activity.pop(key, None)
        self._edit_check_quotas.pop(user_id, None)
        # Its places in line and its claims; the files it held are released
        # by the next tick_file_queues().
        self.file_queues.remove_user(user_id)
        return current is not None

    # --- settings ------------------------------------------------------------

    @property
    def settings(self) -> RoomSettings:
        return self._settings.model_copy()

    @property
    def retention_seconds(self) -> float:
        """How long a touch is kept before the sweep drops it: the warn window,
        but never less than the default so a briefly narrowed window does not
        throw away touches a later, wider one would still compare against."""
        return max(self._settings.warn_window_seconds, ACTIVITY_RETENTION_SECONDS)

    def settings_update(self) -> RoomSettingsUpdate:
        """The current settings as a room_settings frame (join catch-up)."""
        return RoomSettingsUpdate(
            block_window_seconds=self._settings.block_window_seconds,
            warn_window_seconds=self._settings.warn_window_seconds,
            e2e=self._settings.e2e,
            history_hours=self._settings.history_hours,
            set_by=self._settings_set_by,
            timestamp=self._settings_updated_at,
        )

    def apply_settings(
        self, settings: RoomSettings, set_by: Optional[str] = None
    ) -> Optional[RoomSettingsUpdate]:
        """Adopt new conflict windows. Returns the frame to broadcast, or None
        when nothing changed.

        Invalid settings are logged and ignored rather than raised: they come
        from a member's committed config, and refusing the member over it would
        take their coordination offline for a typo. Keeping the old windows is
        the safe reading of "this config is broken".
        """
        problem = settings_problem(settings)
        if problem is not None:
            log.warning(
                "Ignoring room settings from %s: %s; keeping block=%gs warn=%gs",
                set_by or "the relay",
                problem,
                self._settings.block_window_seconds,
                self._settings.warn_window_seconds,
            )
            return None
        if settings == self._settings:
            return None
        log.info(
            "Room settings changed by %s: block %gs -> %gs, warn %gs -> %gs, "
            "e2e %s -> %s",
            set_by or "the relay",
            self._settings.block_window_seconds,
            settings.block_window_seconds,
            self._settings.warn_window_seconds,
            settings.warn_window_seconds,
            self._settings.e2e,
            settings.e2e,
        )
        self._settings = settings.model_copy()
        self._settings_set_by = set_by
        self._settings_updated_at = time.time()
        self.history.configure(self._settings.history_hours)
        return self.settings_update()

    def add_spectator(self, socket: WebSocket) -> None:
        self._spectators.add(socket)
        self.ever_joined = True

    def remove_spectator(self, socket: WebSocket) -> None:
        self._spectators.discard(socket)
        self._owner_sockets.discard(socket)
        self._stalled.discard(socket)
        self._catching_up.pop(socket, None)

    # --- catch-up ------------------------------------------------------------
    #
    # A socket added to the room starts receiving feeds at once, but its
    # catch-up (a snapshot taken right after it was added) is still being
    # sent. Feeds for it are held until the catch-up is out, so the snapshot
    # never lands after newer state.

    def begin_catch_up(self, socket: WebSocket) -> None:
        self._catching_up.setdefault(socket, [])

    async def finish_catch_up(self, socket: WebSocket) -> None:
        """Send the feeds held during the catch-up, then go live."""
        while True:
            held = self._catching_up.get(socket)
            if not held:
                # Nothing held (or the socket is gone): live from here, with
                # no await between this check and the switch.
                self._catching_up.pop(socket, None)
                return
            self._catching_up[socket] = []
            for text in held:
                if not await self._send_text_with_timeout(socket, text):
                    self._catching_up.pop(socket, None)
                    self._drop_socket(None, socket)
                    return

    # --- access --------------------------------------------------------------
    #
    # The room code admits anyone who has it. Rooms created with an owner
    # token can additionally require an owner to approve each new joiner.

    def set_owner_hash(self, owner_hash: Optional[str]) -> None:
        self._owner_hash = owner_hash

    @property
    def has_owner(self) -> bool:
        return self._owner_hash is not None

    def verify_owner_token(self, token: Optional[str]) -> bool:
        """True if token is this room's owner token. Compared as hashes in
        constant time, so timing says nothing about how close a guess was."""
        if not token or self._owner_hash is None:
            return False
        return hmac.compare_digest(hash_owner_token(token), self._owner_hash)

    def add_owner(self, socket: WebSocket) -> None:
        self._owner_sockets.add(socket)

    def is_owner(self, socket: WebSocket) -> bool:
        return socket in self._owner_sockets

    @property
    def owner_count(self) -> int:
        return len(self._owner_sockets)

    @property
    def require_approval(self) -> bool:
        return self._require_approval

    def policy_update(self) -> RoomPolicyUpdate:
        return RoomPolicyUpdate(require_approval=self._require_approval)

    def set_require_approval(self, value: bool) -> Optional[RoomPolicyUpdate]:
        """Change the join policy. Returns the frame to broadcast, or None
        when nothing changed. Switching approval off admits everyone waiting:
        the rule they were held by no longer exists."""
        if value == self._require_approval:
            return None
        self._require_approval = value
        if not value:
            for pending in list(self._pending.values()):
                self.resolve_pending(pending.request_id, True)
        return self.policy_update()

    def access_record(self) -> dict[str, object]:
        """What the store keeps about access. Never the token itself."""
        return {
            "owner_hash": self._owner_hash,
            "require_approval": self._require_approval,
        }

    def load_access(self, record: Optional[dict]) -> None:
        """Restore access_record() output from the store. Tolerates a missing
        or foreign record: a room without one has no owner and no approval."""
        if not isinstance(record, dict):
            return
        owner_hash = record.get("owner_hash")
        self._owner_hash = (
            owner_hash if isinstance(owner_hash, str) and owner_hash else None
        )
        # Approval without an owner would hold every joiner until timeout.
        self._require_approval = bool(record.get("require_approval")) and self.has_owner

    def open_pending(
        self,
        name: str,
        agent: AgentKind = "unknown",
        now: Optional[float] = None,
        ip: str = "",
    ) -> Optional[PendingJoin]:
        """Queue a joiner for approval, or None when MAX_PENDING_JOINS wait
        (or MAX_PENDING_JOINS_PER_IP from this joiner's address: one leaked
        code must not let a single host squat every slot)."""
        if MAX_PENDING_JOINS > 0 and len(self._pending) >= MAX_PENDING_JOINS:
            return None
        if (
            ip
            and MAX_PENDING_JOINS_PER_IP > 0
            and sum(1 for p in self._pending.values() if p.ip == ip)
            >= MAX_PENDING_JOINS_PER_IP
        ):
            return None
        pending = PendingJoin(
            request_id=uuid.uuid4().hex,
            name=name,
            agent=agent,
            created_at=time.time() if now is None else now,
            decision=asyncio.get_running_loop().create_future(),
            ip=ip,
        )
        self._pending[pending.request_id] = pending
        return pending

    def resolve_pending(self, request_id: str, approve: bool) -> bool:
        """Answer a pending join. False for an unknown or already-answered id.

        The waiting joiner's future belongs to the loop its socket runs on.
        Under uvicorn that is the only loop; anything else (e.g. a test
        client that gives each socket its own loop) is handed over safely.
        """
        pending = self._pending.get(request_id)
        if pending is None or pending.decision.done():
            return False
        loop = pending.decision.get_loop()
        try:
            running: Optional[asyncio.AbstractEventLoop] = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            _settle(pending.decision, approve)
        else:
            loop.call_soon_threadsafe(_settle, pending.decision, approve)
        return True

    def close_pending(self, request_id: str) -> None:
        pending = self._pending.pop(request_id, None)
        if pending is not None and not pending.decision.done():
            pending.decision.cancel()

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def pending_requests(self) -> list[JoinRequest]:
        """Unanswered requests, oldest first (an owner's catch-up)."""
        return [
            pending.request()
            for pending in self._pending.values()
            if not pending.decision.done()
        ]

    async def send_to_owners(self, message: BaseModel) -> None:
        """Point-to-point to every owner socket, immediately rather than via
        the feed: someone is waiting at the door."""
        payload = message.model_dump()
        if payload.get("type") == "join_request":
            self._emit(payload)
        text = _dumps(payload)
        owners = [s for s in self._owner_sockets if s not in self._stalled]
        results = await self._send_to_all(owners, text)
        for socket, ok in zip(owners, results):
            if not ok:
                log.warning("Dropping unreachable owner socket")
                self._owner_sockets.discard(socket)

    async def announce_resolved(self, request_id: str, outcome: str) -> None:
        """Tell every owner a request is settled, so no other owner's screen
        keeps offering to approve someone who already got in (or left)."""
        await self.send_to_owners(JoinResolved(request_id=request_id, outcome=outcome))

    def allow_message(self) -> bool:
        """Spend one unit of the room's inbound message quota."""
        return self.message_quota is None or self.message_quota.take()

    def allow_edit_check(self, user_id: str) -> bool:
        """Spend one unit of user_id's own edit-check budget (the room's
        message limit, per member). Edit checks never draw on the shared room
        quota, so nobody else's traffic can make them fail open."""
        if config.ROOM_MESSAGE_LIMIT <= 0:
            return True
        bucket = self._edit_check_quotas.get(user_id)
        if bucket is None:
            bucket = self._edit_check_quotas[user_id] = TokenBucket(config.ROOM_MESSAGE_LIMIT)
        return bucket.take()

    def is_activity_rate_limited(
        self,
        user_id: str,
        min_interval_seconds: float = 0.5,
        channel: str = "activity",
        scope: str = "",
    ) -> bool:
        """Return True if this user sent this kind of message too recently.

        On acceptance, records the current time for the next check. Each
        channel gets its own budget: an agent broadcasts its intent and then
        edits within milliseconds, so a shared budget would drop the first
        activity of every prompt. `scope` (the repo_id) splits a channel
        further: a daemon flushes one activity per repo back to back.

        Monotonic: a wall-clock step backwards must not silence a member.
        """
        now = time.monotonic()
        key = (user_id, channel, scope)
        last = self._last_activity_at.get(key)
        if last is not None and now - last < min_interval_seconds:
            return True
        self._last_activity_at[key] = now
        return False

    # --- file activity -------------------------------------------------------

    def _prune_file_activity(self, now: float) -> None:
        """Drop touches that can no longer affect a comparison.

        Measured against the incoming message's timestamp rather than wall clock,
        the same clock find_overlaps treats as now, and throttled so a busy room
        does not sweep on every batch.
        """
        if now - self._last_prune_at < ACTIVITY_PRUNE_INTERVAL_SECONDS:
            return
        self._last_prune_at = now
        retention = self.retention_seconds
        for file_path in list(self._file_activity):
            touches = self._file_activity[file_path]
            for key in [
                key
                for key, touch in touches.items()
                if now - touch.timestamp > retention
            ]:
                self._drop_touch(file_path, key)

    def _drop_touch(self, file_path: str, key: tuple[str, str]) -> None:
        touches = self._file_activity.get(file_path)
        if touches is None:
            return
        touch = touches.pop(key, None)
        if touch is not None:
            self._touch_bytes -= touch.cost
            self._touch_order.pop((file_path, *key), None)
        if not touches:
            del self._file_activity[file_path]

    def _enforce_touch_bounds(self) -> None:
        """Evict least recently updated touches past the per-room bounds."""
        while self._touch_order and (
            (MAX_TOUCHES_PER_ROOM > 0 and len(self._touch_order) > MAX_TOUCHES_PER_ROOM)
            or (MAX_TOUCH_BYTES_PER_ROOM > 0 and self._touch_bytes > MAX_TOUCH_BYTES_PER_ROOM)
        ):
            file_path, repo_id, user_id = next(iter(self._touch_order))
            self._drop_touch(file_path, (repo_id, user_id))

    @property
    def touch_count(self) -> int:
        return len(self._touch_order)

    @property
    def touch_bytes(self) -> int:
        return self._touch_bytes

    def record_activity(self, message: ActivityMessage) -> None:
        """Merge a batch into each file's touch from this sender.

        A touch is the union of the sender's batches on that file inside the
        retention window: each symbol and line range keeps its own time, so a
        batch that edits bar does not make an edit to foo two batches ago
        invisible. A change without symbols (or ranges) resets that part to
        unknown, so the file is judged whole: an edit somewhere unknown cannot
        be called "a different function"."""
        self._prune_file_activity(message.timestamp)
        now = message.timestamp
        horizon = self.retention_seconds
        changes = [
            change
            for change in message.changes
            if not (MAX_TRACKED_PATH_CHARS > 0 and len(change.file_path) > MAX_TRACKED_PATH_CHARS)
        ]
        # A message's e2e blob is one string shared by all its touches; charge
        # each touch its share.
        e2e_share = len(message.e2e) // max(1, len(changes))
        key = (message.repo_id, message.user_id)
        for change in changes:
            touches = self._file_activity.setdefault(change.file_path, {})
            previous = touches.get(key)
            symbol_seen: dict[str, float] = {}
            range_seen: list[tuple[int, int, float]] = []
            if change.symbols:
                if previous is not None and previous.symbols:
                    symbol_seen = {
                        name: seen
                        for name, seen in (
                            previous.symbol_seen
                            or {name: previous.timestamp for name in previous.symbols}
                        ).items()
                        if now - seen <= horizon
                    }
                for name in change.symbols:
                    symbol_seen.pop(name, None)
                    symbol_seen[name] = now
                while len(symbol_seen) > MAX_SYMBOLS_PER_TOUCH:
                    symbol_seen.pop(next(iter(symbol_seen)))
            if change.line_ranges:
                fresh = [(int(a), int(b)) for a, b in change.line_ranges]
                if previous is not None and previous.line_ranges:
                    earlier = previous.range_seen or [
                        (a, b, previous.timestamp) for a, b in previous.line_ranges
                    ]
                    range_seen = [
                        (a, b, seen)
                        for a, b, seen in earlier
                        if now - seen <= horizon and (a, b) not in fresh
                    ]
                range_seen.extend((a, b, now) for a, b in fresh)
                del range_seen[:-MAX_RANGES_PER_TOUCH]
            diff = change.diff
            if len(diff) > STORED_DIFF_BYTES:
                diff = diff[:STORED_DIFF_BYTES]
            touch = FileTouch(
                user_id=message.user_id,
                file_path=change.file_path,
                diff=diff,
                prompt=message.prompt,
                timestamp=now,
                agent=message.agent,
                repo_id=message.repo_id,
                symbols=list(symbol_seen),
                line_ranges=[(a, b) for a, b, _ in range_seen],
                branch=message.branch,
                e2e=message.e2e,
                symbol_seen=symbol_seen,
                range_seen=range_seen,
            )
            touch.cost = (
                len(diff)
                + len(change.file_path)
                + len(message.prompt or "")
                + e2e_share
                + 32 * (len(symbol_seen) + len(range_seen))
                + 256
            )
            if previous is not None:
                self._touch_bytes -= previous.cost
            touches[key] = touch
            self._touch_bytes += touch.cost
            order_key = (change.file_path, *key)
            self._touch_order.pop(order_key, None)
            self._touch_order[order_key] = None
        self._enforce_touch_bounds()

    def remember_update(self, update: ActivityUpdate) -> None:
        key = (update.user_id, update.repo_id)
        self._latest_updates.pop(key, None)
        self._latest_updates[key] = update
        _trim_per_member(self._latest_updates, update.user_id, MAX_INTENTS_PER_MEMBER)

    def latest_updates(self) -> list[ActivityUpdate]:
        return list(self._latest_updates.values())

    # --- intent --------------------------------------------------------------

    def record_intent(
        self,
        user_id: str,
        prompt: str,
        timestamp: float,
        agent: AgentKind = "unknown",
        repo_id: str = "",
        e2e: str = "",
    ) -> IntentUpdate:
        """Store the user's latest intent and return it as the broadcast record.

        repo_id is kept with the intent so a prompt naming main.py only warns
        peers working in the same repository.
        """
        update = IntentUpdate(
            user_id=user_id,
            agent=agent,
            prompt=prompt,
            timestamp=timestamp,
            repo_id=repo_id,
            e2e=e2e,
        )
        key = (user_id, repo_id)
        self._latest_intents.pop(key, None)
        self._latest_intents[key] = update
        _trim_per_member(self._latest_intents, user_id, MAX_INTENTS_PER_MEMBER)
        return update

    def latest_intents(self) -> list[IntentUpdate]:
        return list(self._latest_intents.values())

    def recent_other_touches(
        self,
        file_path: str,
        user_id: str,
        now: float,
        *,
        repo_id: str = "",
        active_only: bool = True,
        window_seconds: Optional[float] = None,
    ) -> list[FileTouch]:
        """Other users' recent touches of the same file, in the same repo,
        within window_seconds (default: this room's warn window). A window of
        zero turns touch comparison off rather than matching same-instant
        touches only."""
        if window_seconds is None:
            window_seconds = self._settings.warn_window_seconds
        if window_seconds <= 0:
            return []
        touches = self._file_activity.get(file_path, {})
        results: list[FileTouch] = []
        for (touch_repo, other_id), touch in touches.items():
            if other_id == user_id or touch_repo != repo_id:
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
        against peers, not against a just-written self entry.
        """
        overlaps: list[FileOverlap] = []
        for change in message.changes:
            others = self.recent_other_touches(
                change.file_path,
                message.user_id,
                message.timestamp,
                repo_id=message.repo_id,
            )
            # Same file but provably different functions: not worth a Gemini
            # call or an alert. Config files are always judged whole.
            if change.symbols and not is_always_warn_file(change.file_path):
                others = [
                    touch
                    for touch in others
                    if not touch.symbols
                    or symbols_intersect(change.symbols, touch.symbols)
                ]
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

    def _intent_mentions(
        self, file_path: str, user_id: str, now: float, repo_id: str = ""
    ) -> list[FileTouch]:
        """Active peers whose recent declared intent names this file, newest first.

        Matching on the bare stem as well as the basename catches prompts that
        talk about the module rather than the file ("refactor the Room
        broadcast" vs server/room.py). Stems under 3 characters are skipped
        because they match almost any sentence. Only intents declared in the
        same repo count: "fix main.py" in one project says nothing about
        main.py in another.
        """
        basename = file_path.rsplit("/", 1)[-1].lower()
        stem = basename.rsplit(".", 1)[0]
        needles = [basename]
        if len(stem) >= 3:
            needles.append(stem)
        # Whole words only: the stem "app" must not match "happy" or
        # "mapping", nor "form" match "format".
        pattern = _mention_pattern(tuple(needles))

        matches: list[FileTouch] = []
        for (other_id, _repo), intent in self._latest_intents.items():
            if other_id == user_id or not self.is_active(other_id):
                continue
            if intent.repo_id != repo_id:
                continue
            if now - intent.timestamp > INTENT_WINDOW_SECONDS:
                continue
            if not pattern.search(intent.prompt.lower()):
                continue
            matches.append(
                FileTouch(
                    user_id=other_id,
                    file_path=file_path,
                    diff="",
                    prompt=intent.prompt,
                    timestamp=intent.timestamp,
                    agent=intent.agent,
                    repo_id=intent.repo_id,
                    kind="intent",
                    e2e=intent.e2e,
                )
            )
        matches.sort(key=lambda touch: touch.timestamp, reverse=True)
        return matches

    def check_edit(
        self,
        user_id: str,
        file_path: str,
        now: float,
        repo_id: str = "",
        *,
        symbols: Optional[list[str]] = None,
        line_ranges: Optional[list[tuple[int, int]]] = None,
        branch: str = "",
    ) -> tuple[Decision, list[FileTouch]]:
        """Decide whether user_id may edit file_path (in repo_id) right now.

        Returns the decision plus the peers that justified it, most relevant
        first. evaluate_edit also returns the scope.
        """
        verdict = self.evaluate_edit(
            user_id,
            file_path,
            now,
            repo_id,
            symbols=symbols,
            line_ranges=line_ranges,
            branch=branch,
        )
        return verdict.decision, verdict.peers

    def _judge_touch(
        self,
        touch: FileTouch,
        now: float,
        symbols: list[str],
        line_ranges: list[tuple[int, int]],
        branch: str,
        always_warn: bool,
    ) -> tuple[Decision, DecisionScope]:
        """Decision table for one peer touch already inside the warn window."""
        if _branches_differ(branch, touch.branch):
            # Not a live collision, but a merge conflict waiting to happen.
            return "warn", "branch"
        block_window = self._settings.block_window_seconds
        warn_window = self._settings.warn_window_seconds

        def hard_at(when: float) -> Decision:
            # Each part of a merged touch is judged by its own age.
            return "block" if block_window > 0 and now - when <= block_window else "warn"

        hard = hard_at(touch.timestamp)
        # Symbols edited before the warn window no longer describe the peer.
        touch_symbols = [
            name
            for name in touch.symbols
            if now - touch.symbol_seen.get(name, touch.timestamp) <= warn_window
        ]
        if symbols and touch_symbols:
            shared = set(symbols).intersection(touch_symbols)
            if shared:
                return hard_at(touch.symbol_time(shared)), "symbol"
            if always_warn:
                return hard, "file"
            # Symbol extraction can mislabel a hunk on a definition boundary;
            # literally overlapping lines are a collision whatever the names say.
            if line_ranges and touch.line_ranges:
                when = touch.ranges_time(line_ranges, 0)
                if when is not None:
                    return hard_at(when), "lines"
            return "warn", "symbol"
        if line_ranges and touch.line_ranges:
            when = touch.ranges_time(line_ranges, LINE_PROXIMITY)
            if when is not None:
                return hard_at(when), "lines"
            if always_warn:
                return hard, "file"
            return "warn", "lines"
        return hard, "file"

    def evaluate_edit(
        self,
        user_id: str,
        file_path: str,
        now: float,
        repo_id: str = "",
        *,
        symbols: Optional[list[str]] = None,
        line_ranges: Optional[list[tuple[int, int]]] = None,
        branch: str = "",
    ) -> EditVerdict:
        """The full decision table: block/warn/allow plus the deciding scope.

        Order: peer touches (judged by branch, then symbol, line or file
        granularity), then active plans naming the file or a requested
        symbol, then the intent-mention fallback. Both windows are this room's
        settings; a block window of zero never blocks, so the room still warns.
        A request without symbols or line ranges gets the legacy file-level
        answer.
        """
        wanted_symbols = list(symbols or [])
        wanted_ranges = [(int(a), int(b)) for a, b in (line_ranges or [])]
        always_warn = is_always_warn_file(file_path)
        others = self.recent_other_touches(
            file_path,
            user_id,
            now,
            repo_id=repo_id,
            active_only=True,
            window_seconds=self._settings.warn_window_seconds,
        )
        others.sort(key=lambda touch: touch.timestamp, reverse=True)

        judged = [
            (touch, *self._judge_touch(
                touch, now, wanted_symbols, wanted_ranges, branch, always_warn
            ))
            for touch in others
        ]
        blocking = [(touch, scope) for touch, decision, scope in judged if decision == "block"]
        if blocking:
            return EditVerdict("block", [touch for touch, _ in blocking], blocking[0][1])
        if judged:
            # Same-branch peers first (stable sort keeps newest-first within):
            # a live edit matters more than a future merge conflict.
            judged.sort(key=lambda item: item[2] == "branch")
            return EditVerdict("warn", [touch for touch, _, _ in judged], judged[0][2])

        planned = self._plan_mentions(file_path, user_id, now, repo_id, wanted_symbols)
        if planned:
            return EditVerdict("warn", planned, "plan")

        mentions = self._intent_mentions(file_path, user_id, now, repo_id)
        if mentions:
            return EditVerdict("warn", mentions, "intent")
        return EditVerdict("allow", [], "file")

    # --- plans ---------------------------------------------------------------

    def _active_plans(self, now: float, exclude: str, repo_id: str) -> list[PlanUpdate]:
        return [
            plan
            for (other_id, _repo), plan in self._plans.items()
            if other_id != exclude
            and self.is_active(other_id)
            and plan.repo_id == repo_id
            and plan.expires_at > now
        ]

    def _plan_mentions(
        self,
        file_path: str,
        user_id: str,
        now: float,
        repo_id: str,
        symbols: list[str],
    ) -> list[FileTouch]:
        """Active peers whose plan names this file, or (for a plan that names
        no files) a requested symbol. A plan's symbols belong to its files:
        `main` in a plan for api/server.py says nothing about cli/tool.py."""
        matches: list[FileTouch] = []
        for plan in self._active_plans(now, user_id, repo_id):
            if plan.files:
                if file_path not in plan.files:
                    continue
            elif not plan_symbols_intersect(symbols, plan.symbols):
                continue
            matches.append(
                FileTouch(
                    user_id=plan.user_id,
                    file_path=file_path,
                    diff="",
                    prompt=plan.summary or None,
                    timestamp=plan.timestamp,
                    agent=plan.agent,
                    repo_id=plan.repo_id,
                    symbols=list(plan.symbols),
                    branch=plan.branch,
                    kind="planned",
                    e2e=plan.e2e,
                )
            )
        matches.sort(key=lambda touch: touch.timestamp, reverse=True)
        return matches

    def record_plan(
        self, message: PlanMessage, now: float, agent: AgentKind = "unknown"
    ) -> PlanUpdate:
        """Store message as its sender's current plan, replacing any earlier one."""
        update = PlanUpdate(
            user_id=message.user_id,
            agent=agent,
            summary=message.summary,
            files=list(message.files),
            symbols=list(message.symbols),
            repo_id=message.repo_id,
            branch=message.branch,
            timestamp=now,
            expires_at=now + message.ttl_seconds,
            e2e=message.e2e,
        )
        key = (message.user_id, message.repo_id)
        self._plans.pop(key, None)
        self._plans[key] = update
        _trim_per_member(self._plans, message.user_id, MAX_PLANS_PER_MEMBER)
        return update

    def plan_conflicts(self, plan: PlanUpdate, now: float) -> list[PlanConflict]:
        """Other members' active plans and recent edits this plan collides with,
        by file, or by symbol where one side names no files (symbols are only
        meaningful within the files they belong to). One entry per (file,
        user, kind), newest first."""
        conflicts: dict[tuple[str, str, str], PlanConflict] = {}
        planned_files = set(plan.files)
        for other in self._active_plans(now, plan.user_id, plan.repo_id):
            shared = [f for f in other.files if f in planned_files]
            if (
                not shared
                and (not planned_files or not other.files)
                and plan_symbols_intersect(plan.symbols, other.symbols)
            ):
                shared = list(other.files[:1]) or list(plan.files[:1]) or [""]
            for file_path in shared:
                conflicts.setdefault(
                    (file_path, other.user_id, "planned"),
                    PlanConflict(
                        file_path=file_path,
                        user_id=other.user_id,
                        agent=other.agent,
                        kind="planned",
                        summary=other.summary,
                        seconds_ago=max(0.0, now - other.timestamp),
                        e2e=other.e2e,
                    ),
                )
        window = self._settings.warn_window_seconds
        for file_path, touches in self._file_activity.items():
            for (touch_repo, other_id), touch in touches.items():
                if other_id == plan.user_id or touch_repo != plan.repo_id:
                    continue
                if not self.is_active(other_id) or now - touch.timestamp > window:
                    continue
                if planned_files:
                    if file_path not in planned_files:
                        continue
                elif not plan_symbols_intersect(plan.symbols, touch.symbols):
                    continue
                conflicts.setdefault(
                    (file_path, other_id, "edited"),
                    PlanConflict(
                        file_path=file_path,
                        user_id=other_id,
                        agent=touch.agent,
                        kind="edited",
                        summary=touch.prompt or "",
                        seconds_ago=max(0.0, now - touch.timestamp),
                        e2e=touch.e2e,
                    ),
                )
        return sorted(conflicts.values(), key=lambda conflict: conflict.seconds_ago)

    def active_plans(self, now: Optional[float] = None) -> list[PlanUpdate]:
        """Unexpired plans of connected members (spectator catch-up)."""
        now = time.time() if now is None else now
        return [
            plan
            for (user_id, _repo), plan in self._plans.items()
            if plan.expires_at > now and self.is_active(user_id)
        ]

    # --- wait queues (file_queue.py) -----------------------------------------

    def remember_path(self, repo_id: str, file_path: str) -> None:
        """Remember a path this room has seen, for edit-check suggestions.
        Hashed (e2e) paths are not kept: nothing can be suggested from them."""
        if (
            not file_path
            or file_path.startswith(suggest.HASH_PREFIX)
            or (MAX_QUEUED_PATH_CHARS > 0 and len(file_path) > MAX_QUEUED_PATH_CHARS)
        ):
            return
        key = (repo_id, file_path)
        self._known_paths.pop(key, None)
        self._known_paths[key] = None
        while MAX_KNOWN_PATHS > 0 and len(self._known_paths) > MAX_KNOWN_PATHS:
            self._known_paths.popitem(last=False)

    def _queue_blockers(self, key: QueueKey, entry: QueueEntry, now: float) -> list[str]:
        """Who keeps entry's member out of the file right now: peers whose
        touches block it (judged with what it last asked to edit) and anyone
        holding a claim."""
        repo_id, file_path = key
        verdict = self.evaluate_edit(
            entry.user_id,
            file_path,
            now,
            repo_id,
            symbols=entry.symbols,
            line_ranges=entry.line_ranges,
            branch=entry.branch,
        )
        holders = [touch.user_id for touch in verdict.peers] if verdict.decision == "block" else []
        holders.extend(self.file_queues.claimers(key, exclude=entry.user_id))
        return list(dict.fromkeys(holders))

    def _claim_settled(self, key: QueueKey, user_id: str, claimed_at: float) -> bool:
        """True once a claimer's own touch of the file has arrived (it then
        blocks like any touch, at symbol granularity)."""
        repo_id, file_path = key
        touch = self._file_activity.get(file_path, {}).get((repo_id, user_id))
        return touch is not None and touch.timestamp >= claimed_at

    def sweep_file_queues(self, now: float) -> list[QueueNotice]:
        """Expire, release and notify (the notices are returned, not sent)."""
        if not self.file_queues:
            return []
        return self.file_queues.sweep(
            now,
            warn_window=self._settings.warn_window_seconds,
            block_window=self._settings.block_window_seconds,
            blockers=self._queue_blockers,
            settled=self._claim_settled,
            is_active=self.is_active,
        )

    async def tick_file_queues(self, now: Optional[float] = None) -> None:
        """Sweep the wait queues and deliver what that produced. Called on
        every member message (lazy), when a member leaves, and once a second
        by the room's sweeper while any line or claim exists."""
        if not self.file_queues:
            return
        notices = self.sweep_file_queues(time.time() if now is None else now)
        await self.deliver_queue_notices(notices)
        self.ensure_queue_sweeper()

    def _notify_holders(
        self, key: QueueKey, entry: QueueEntry, holders: list[str], now: float
    ) -> list[QueueNotice]:
        """file_requested for each holder not yet told that entry waits."""
        notices: list[QueueNotice] = []
        for holder in dict.fromkeys(holders):
            if holder == entry.user_id or holder in entry.notified or not self.is_active(holder):
                continue
            entry.notified.add(holder)
            notices.append(QueueNotice("requested", holder, key, entry, now))
        return notices

    def queue_edit_check(
        self,
        user_id: str,
        agent: AgentKind,
        repo_id: str,
        file_path: str,
        verdict: EditVerdict,
        now: float,
        *,
        e2e: str = "",
        symbols: Optional[list[str]] = None,
        line_ranges: Optional[list[tuple[int, int]]] = None,
        branch: str = "",
    ) -> QueueOutcome:
        """Put an edit check's verdict through the file's wait queue.

        Call tick_file_queues() (or sweep_file_queues()) first, so lapsed
        holders have been released. Then:

        * block: the requester joins (or keeps its place in) the line, and
          the holders are told once that it waits;
        * not blocked by any touch, but the file was just released to the
          member ahead, or someone else just took it from the line (a
          claim): blocked anyway, behind them;
        * otherwise the verdict stands, and a requester that was in line
          (or passes while others wait) takes the file.
        """
        key = (repo_id, file_path)
        self.remember_path(repo_id, file_path)
        queues = self.file_queues
        block_window = self._settings.block_window_seconds
        request = {"e2e": e2e, "symbols": symbols, "line_ranges": line_ranges, "branch": branch}

        if verdict.decision == "block":
            position = queues.enqueue(key, user_id, agent, now, **request)
            entry = queues.entry(key, user_id)
            notices: list[QueueNotice] = []
            if entry is not None:
                if entry is queues.head(key):
                    # A holder is back: any release to this member is void.
                    entry.released_at = None
                notices = self._notify_holders(
                    key, entry, [touch.user_id for touch in verdict.peers], now
                )
            newest = max((touch.timestamp for touch in verdict.peers), default=now)
            age = max(0.0, now - newest)
            return QueueOutcome(
                "block",
                verdict.peers,
                verdict.scope,
                queue_position=position,
                wait=suggest.WaitHint(block_window - age, f"they edited it {int(age)}s ago"),
                notices=notices,
            )

        claimers = queues.claimers(key, exclude=user_id)
        head = queues.head(key)
        ahead: Optional[FileTouch] = None
        reason = ""
        wait: Optional[suggest.WaitHint] = None
        if claimers:
            taker, since = max(claimers.items(), key=lambda item: item[1])
            reason = f"{taker} just took {file_path} from the wait queue ({int(now - since)}s ago)"
            wait = suggest.WaitHint(
                min(block_window, CLAIM_MAX_SECONDS) - (now - since), f"{taker} just took it"
            )
            ahead = FileTouch(
                user_id=taker,
                file_path=file_path,
                diff="",
                prompt=None,
                timestamp=since,
                agent=self.member_agent(taker),
                repo_id=repo_id,
                kind="planned",
            )
        elif head is not None and head.user_id != user_id and head.released_at is not None:
            since = head.released_at
            reason = (
                f"{head.user_id} is next in line for {file_path} "
                f"(released to them {int(now - since)}s ago)"
            )
            wait = suggest.WaitHint(
                queues.grace_seconds - (now - since), f"{head.user_id} is next in line"
            )
            ahead = FileTouch(
                user_id=head.user_id,
                file_path=file_path,
                diff="",
                prompt=None,
                timestamp=since,
                agent=head.agent,  # type: ignore[arg-type]
                repo_id=repo_id,
                kind="planned",
                e2e=head.e2e,
            )
        if ahead is not None:
            position = queues.enqueue(key, user_id, agent, now, **request)
            entry = queues.entry(key, user_id)
            notices = (
                self._notify_holders(key, entry, list(claimers), now) if entry is not None else []
            )
            return QueueOutcome(
                "block",
                [ahead],
                "file",
                reason=reason,
                queue_position=position,
                wait=wait,
                notices=notices,
            )

        if head is not None:
            queues.take(key, user_id, now)
        return QueueOutcome(verdict.decision, verdict.peers, verdict.scope)

    def queue_file_request(
        self,
        user_id: str,
        agent: AgentKind,
        repo_id: str,
        file_path: str,
        now: float,
        *,
        e2e: str = "",
        branch: str = "",
        symbols: Optional[list[str]] = None,
    ) -> list[QueueNotice]:
        """An explicit ask for a file: join its line and tell the holders, or,
        when nothing keeps the requester out and nobody is ahead of it,
        release the file to it at once (it then has the grace to take it).
        Re-asking keeps the requester's place; a released requester is sent
        file_released again."""
        key = (repo_id, file_path)
        self.remember_path(repo_id, file_path)
        queues = self.file_queues
        probe = queues.entry(key, user_id) or QueueEntry(
            user_id=user_id,
            agent=agent,
            enqueued_at=now,
            refreshed_at=now,
            symbols=list(symbols or []),
            branch=branch,
        )
        if symbols:
            probe.symbols = list(symbols)
        if branch:
            probe.branch = branch
        holders = self._queue_blockers(key, probe, now)
        head = queues.head(key)
        if (
            queues.enqueue(
                key, user_id, agent, now, e2e=e2e, symbols=symbols, branch=branch
            )
            == 0
        ):
            # The line (or the room's table of lines) is full. When nothing
            # holds the file, say so at once; otherwise there is no frame for
            # "line full", and the requester's own timeout answers it.
            if not holders and head is None:
                released = QueueEntry(
                    user_id=user_id, agent=agent, enqueued_at=now, refreshed_at=now, e2e=e2e
                )
                released.released_at = now
                return [QueueNotice("released", user_id, key, released, now)]
            return []
        entry = queues.entry(key, user_id)
        assert entry is not None
        if not holders and (head is None or head.user_id == user_id):
            if entry.released_at is None:
                entry.released_at = now
            return [QueueNotice("released", user_id, key, entry, now)]
        if holders and entry is queues.head(key):
            entry.released_at = None
        return self._notify_holders(key, entry, holders, now)

    def _suggestion_inputs(
        self, user_id: str, repo_id: str, now: float
    ) -> tuple[set[str], set[str], list[str]]:
        """(known paths, paths an active teammate is touching, planning or
        queued for, the requester's own plan files), all in repo_id."""
        known = {path for rid, path in self._known_paths if rid == repo_id}
        busy: set[str] = set()
        window = self._settings.warn_window_seconds
        for path, touches in self._file_activity.items():
            for (rid, other_id), touch in touches.items():
                if rid != repo_id:
                    continue
                known.add(path)
                if (
                    other_id != user_id
                    and self.is_active(other_id)
                    and now - touch.timestamp <= window
                ):
                    busy.add(path)
        own_plan: list[str] = []
        for (other_id, _repo), plan in self._plans.items():
            if plan.repo_id != repo_id or plan.expires_at <= now or not self.is_active(other_id):
                continue
            known.update(plan.files)
            if other_id == user_id:
                own_plan = list(plan.files)
            else:
                busy.update(plan.files)
        for rid, path in [*self.file_queues.keys(), *self.file_queues.claim_keys()]:
            if rid == repo_id:
                busy.add(path)
        return known, busy, own_plan

    def edit_suggestion(
        self,
        decision: Decision,
        user_id: str,
        repo_id: str,
        file_path: str,
        now: float,
        wait: Optional[suggest.WaitHint] = None,
    ) -> str:
        """The edit_check_response.suggestion for a block or warn."""
        if decision == "allow":
            return ""
        known, busy, own_plan = self._suggestion_inputs(user_id, repo_id, now)
        return suggest.build_suggestion(
            decision,
            file_path,
            known=known,
            busy=busy,
            own_plan=own_plan,
            wait=wait,
            always_warn=is_always_warn_file,
        )

    def _queue_frame(self, notice: QueueNotice) -> BaseModel:
        repo_id, file_path = notice.key
        if notice.kind == "released":
            return FileReleased(
                file_path=file_path,
                repo_id=repo_id,
                timestamp=notice.entry.released_at or notice.at,
                e2e=notice.entry.e2e,
            )
        return FileRequested(
            file_path=file_path,
            repo_id=repo_id,
            by=notice.entry.user_id,
            agent=notice.entry.agent,  # type: ignore[arg-type]
            timestamp=notice.at,
            e2e=notice.entry.e2e,
        )

    async def deliver_queue_notices(self, notices: list[QueueNotice]) -> None:
        """Point-to-point, immediately: someone is waiting, or it is their turn.
        Each send is bounded by SEND_TIMEOUT_SECONDS, so a slow holder delays
        the frame that triggered the sweep by at most that much."""
        for notice in notices:
            frame = self._queue_frame(notice)
            if await self.send_to_member(notice.to_user, frame):
                log.debug(
                    "%s for %s to %s",
                    frame.type,  # type: ignore[attr-defined]
                    notice.entry.user_id if notice.kind == "requested" else "the next in line",
                    notice.to_user,
                )

    async def send_to_member(self, user_id: str, message: BaseModel) -> bool:
        socket = self._members.get(user_id)
        if socket is None or socket in self._stalled:
            return False
        if await self._send_text_with_timeout(socket, _dumps(message.model_dump())):
            return True
        log.warning("Could not reach %s for a point-to-point frame", user_id)
        self._drop_socket(user_id, socket)
        return False

    def ensure_queue_sweeper(self) -> None:
        """Run the once-a-second sweep while any line or claim exists. The
        task ends by itself when the last one goes."""
        if not self.file_queues:
            return
        task = self._queue_sweeper
        if task is not None and not task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # no loop: a synchronous unit test drives sweeps itself
            return
        self._queue_sweeper = loop.create_task(self._sweep_queues_periodically())

    async def _sweep_queues_periodically(self) -> None:
        while self.file_queues:
            await asyncio.sleep(QUEUE_SWEEP_INTERVAL_SECONDS)
            try:
                await self.tick_file_queues()
            except Exception:
                log.exception("File queue sweep failed")

    # --- decision feedback ---------------------------------------------------

    def register_decision(self, decision_id: str, scope: DecisionScope) -> None:
        """Track a broadcast warn/block so dashboards can vote on it."""
        self._decisions[decision_id] = _DecisionVotes(scope=scope)
        self._decisions.move_to_end(decision_id)
        while MAX_TRACKED_DECISIONS > 0 and len(self._decisions) > MAX_TRACKED_DECISIONS:
            self._decisions.popitem(last=False)

    def decision_scope(self, decision_id: str) -> Optional[DecisionScope]:
        votes = self._decisions.get(decision_id)
        return None if votes is None else votes.scope

    def record_feedback(
        self, decision_id: str, useful: bool, voter: str
    ) -> Optional[DecisionFeedbackUpdate]:
        """Count one vote. None for an unknown (or evicted) decision, or for a
        voter who already voted on it."""
        votes = self._decisions.get(decision_id)
        if votes is None or voter in votes.voters:
            return None
        votes.voters.add(voter)
        if useful:
            votes.useful += 1
        else:
            votes.noise += 1
        self._decisions.move_to_end(decision_id)
        return DecisionFeedbackUpdate(
            decision_id=decision_id,
            useful_count=votes.useful,
            noise_count=votes.noise,
        )

    # --- broadcast -----------------------------------------------------------

    async def broadcast(self, message: BaseModel) -> None:
        """Queue a room-wide event and flush as a Hivelane feed batch (~500ms).

        Never waits on any socket: it only appends to the queue (async for the
        callers' sake). Point-to-point replies still use send() and go out
        immediately.
        """
        payload = message.model_dump()
        self._emit(payload)
        self._broadcast_queue.append(payload)
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._flush_broadcast_queue())

    async def _flush_broadcast_queue(self) -> None:
        await asyncio.sleep(BROADCAST_FLUSH_SECONDS)
        # Deliveries run one at a time, so every socket gets feeds in order.
        # Each is bounded (SEND_TIMEOUT_SECONDS per socket, all sockets at
        # once), so a flush waiting here waits at most that long.
        async with self._deliver_lock:
            events = self._broadcast_queue
            self._broadcast_queue = []
            if self._flush_task is asyncio.current_task():
                self._flush_task = None
            if not events:
                return
            log.debug("Flushing Hivelane feed (%d event(s))", len(events))
            # Kept exactly as spectators get them (a no-op while history is off).
            self.history.record(events)
            for text in feed_frames(events):
                await self._deliver_text(text)

    def _emit(self, payload: dict) -> None:
        for sink in list(_event_sinks):
            try:
                sink(self, payload)
            except Exception:
                log.exception("Room event sink failed")

    async def _deliver(self, payload: dict) -> None:
        """Send one frame to every member and spectator (see _deliver_text)."""
        await self._deliver_text(_dumps(payload))

    async def _deliver_text(self, text: str) -> None:
        """Send one serialized frame to every member and spectator at once.

        Serialized once, sent concurrently, each send bounded by
        SEND_TIMEOUT_SECONDS. A socket that misses the deadline (or fails) is
        dropped: skipped from then on and closed, so one stalled dashboard
        or sleeping laptop cannot hold up the room. Membership itself is
        left to the member's own handler, which removes it on disconnect
        (removing by name here could evict a reconnected successor).
        Sockets still receiving their catch-up get the frame afterwards."""
        targets: list[tuple[Optional[str], WebSocket]] = [
            (user_id, socket) for user_id, socket in self._members.items()
        ]
        targets.extend((None, socket) for socket in self._spectators)
        live: list[tuple[Optional[str], WebSocket]] = []
        for user_id, socket in targets:
            if socket in self._stalled:
                continue
            held = self._catching_up.get(socket)
            if held is not None:
                held.append(text)
                continue
            live.append((user_id, socket))
        if not live:
            return
        results = await self._send_to_all([socket for _, socket in live], text)
        for (user_id, socket), ok in zip(live, results):
            if not ok:
                self._drop_socket(user_id, socket)

    async def _send_to_all(self, sockets: list[WebSocket], text: str) -> list[bool]:
        """Send to every socket at once (one slow socket costs at most
        SEND_TIMEOUT_SECONDS, not one per socket). A single target is sent to
        in the calling task: no extra task is needed, and a caller already
        being cancelled (a handler's cleanup) still gets its frame out."""
        if not sockets:
            return []
        if len(sockets) == 1:
            return [await self._send_text_with_timeout(sockets[0], text)]
        return list(
            await asyncio.gather(
                *(self._send_text_with_timeout(socket, text) for socket in sockets)
            )
        )

    async def _send_text_with_timeout(self, socket: WebSocket, text: str) -> bool:
        try:
            if SEND_TIMEOUT_SECONDS > 0:
                await asyncio.wait_for(_send_text(socket, text), SEND_TIMEOUT_SECONDS)
            else:
                await _send_text(socket, text)
        except Exception:
            return False
        return True

    def _drop_socket(self, user_id: Optional[str], socket: WebSocket) -> None:
        """Stop sending to a socket that failed or stalled, and close it."""
        if socket in self._stalled:
            return
        log.warning(
            "Dropping unreachable or slow socket (%s)",
            "spectator" if user_id is None else f"user={user_id}",
        )
        self._stalled.add(socket)
        self._owner_sockets.discard(socket)
        if user_id is None:
            self._spectators.discard(socket)
        try:
            asyncio.get_running_loop().create_task(_close_stalled(socket))
        except RuntimeError:
            pass

    async def send(self, socket: WebSocket, message: BaseModel) -> None:
        await socket.send_json(message.model_dump())


async def _send_text(socket: WebSocket, text: str) -> None:
    send_text = getattr(socket, "send_text", None)
    if send_text is not None:
        await send_text(text)
    else:  # test doubles that only implement send_json
        await socket.send_json(json.loads(text))


# Close code for a socket dropped for missing a send deadline: "try again
# later" (RFC 6455 1013). Clients reconnect as after any other drop.
WS_CLOSE_TOO_SLOW = 1013


async def _close_stalled(socket: WebSocket) -> None:
    try:
        await asyncio.wait_for(
            socket.close(code=WS_CLOSE_TOO_SLOW, reason="Too slow to keep up"),
            max(SEND_TIMEOUT_SECONDS, 1.0),
        )
    except Exception:
        pass


_FEED_HEAD = '{"type":"hivelane_feed","events":['
_FEED_TAIL = "]}"


def feed_frames(events: list[dict], budget: Optional[int] = None) -> list[str]:
    """The hivelane_feed frame(s) for a batch of events, serialized once.

    Split so that no frame exceeds `budget` bytes (OUTBOUND_FRAME_BUDGET): a
    burst of large e2e activity would otherwise make one feed bigger than a
    daemon accepts, and it would reconnect into the same feed. An event that
    alone exceeds the budget goes in a frame of its own."""
    limit = OUTBOUND_FRAME_BUDGET if budget is None else budget
    frames: list[str] = []
    chunk: list[str] = []
    empty = len(_FEED_HEAD) + len(_FEED_TAIL)
    size = empty
    for event in events:
        text = _dumps(event)
        cost = len(text.encode("utf-8")) + 1
        if chunk and limit > 0 and size + cost > limit:
            frames.append(_FEED_HEAD + ",".join(chunk) + _FEED_TAIL)
            chunk, size = [], empty
        chunk.append(text)
        size += cost
    if chunk:
        frames.append(_FEED_HEAD + ",".join(chunk) + _FEED_TAIL)
    return frames


def fit_e2e(records: list, base_bytes: int, budget: Optional[int] = None) -> int:
    """Blank the e2e copies on derived records (PeerTouch, PlanConflict),
    from the last one back, until the frame fits the outbound budget.

    Each record otherwise carries a full copy of its peer's blob, so a reply
    about many files can grow far past what a daemon accepts. Returns how
    many copies were dropped; the records stay, with their plain fields."""
    limit = OUTBOUND_FRAME_BUDGET if budget is None else budget
    if limit <= 0:
        return 0
    total = base_bytes + sum(len(getattr(r, "e2e", "") or "") for r in records)
    dropped = 0
    for record in reversed(records):
        if total <= limit:
            break
        blob = getattr(record, "e2e", "")
        if blob:
            total -= len(blob)
            record.e2e = ""
            dropped += 1
    return dropped
