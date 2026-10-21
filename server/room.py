"""In-memory state for one workspace ("room"). A process may hold many Room
instances at once (see room_manager.RoomManager) — each is fully isolated.
Ephemeral by design: the room *code* is durable (store.RoomStore), but nothing
in a Room survives a relay restart; it re-forms from live activity, and every
touch older than the conflict window would be ignored anyway."""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Optional

from fastapi import WebSocket
from pydantic import BaseModel

try:
    from .models import (
        ActivityMessage,
        DecisionFeedbackUpdate,
        DecisionScope,
        PeerKind,
        PlanConflict,
        PlanMessage,
        PlanUpdate,
        ActivityUpdate,
        AgentKind,
        Decision,
        IntentUpdate,
        RoomSettings,
        RoomSettingsUpdate,
        SynergoFeed,
    )
except ImportError:  # Render rootDir=server
    from models import (
        ActivityMessage,
        DecisionFeedbackUpdate,
        DecisionScope,
        PeerKind,
        PlanConflict,
        PlanMessage,
        PlanUpdate,
        ActivityUpdate,
        AgentKind,
        Decision,
        IntentUpdate,
        RoomSettings,
        RoomSettingsUpdate,
        SynergoFeed,
    )

log = logging.getLogger(__name__)

# Defaults for a room nobody has configured. A room's own RoomSettings (from
# POST /rooms or a member's committed .synergo.toml) replace these per room.
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

# Debounce room-wide broadcasts into a single Synergo feed frame.
BROADCAST_FLUSH_SECONDS = 0.5

# Two line-range edits closer than this are treated as the same hunk: git's
# merge context makes near-adjacent edits conflict too.
LINE_PROXIMITY = 10

# The symbol a client sends for an import-block edit. It intersects with
# every symbol: two agents adding imports collide whatever else they touch.
IMPORTS_SYMBOL = "<imports>"

# Feedback is kept for this many recent decisions per room (LRU).
MAX_TRACKED_DECISIONS = 1000

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
    ".synergo.toml",
)


def is_always_warn_file(file_path: str) -> bool:
    """True for imports/config files that are never judged by symbol."""
    parts = file_path.replace("\\", "/").split("/")
    basename = parts[-1]
    if any(fnmatch.fnmatchcase(basename, pattern) for pattern in _ALWAYS_WARN_BASENAMES):
        return True
    return ".github" in parts[:-1] and basename.endswith((".yml", ".yaml"))


def symbols_intersect(a: list[str], b: list[str]) -> bool:
    """True when two symbol lists share a name; "<imports>" matches anything."""
    if not a or not b:
        return False
    if IMPORTS_SYMBOL in a or IMPORTS_SYMBOL in b:
        return True
    return bool(set(a) & set(b))


def ranges_near(
    a: list[tuple[int, int]], b: list[tuple[int, int]], proximity: int = LINE_PROXIMITY
) -> bool:
    """True when any range of a overlaps or lies within proximity lines of b."""
    for a_start, a_end in a:
        for b_start, b_end in b:
            if a_start <= b_end + proximity and b_start <= a_end + proximity:
                return True
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


@dataclass
class EditVerdict:
    """Room.evaluate_edit's answer: the decision, the peers that justified it
    (most relevant first) and the granularity at which it was decided."""

    decision: Decision
    peers: list[FileTouch]
    scope: DecisionScope = "file"


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
        # file_path -> {(repo_id, user_id): FileTouch} — latest edit per user
        # per file per repo. Together the two keys make a touch's identity
        # (repo_id, file_path): the same relative path in two different repos
        # is two different files. The path stays the outer key so a lookup by
        # path is still one dict hit.
        self._file_activity: dict[str, dict[tuple[str, str], FileTouch]] = {}
        # Message-clock timestamp of the last sweep (see _prune_file_activity).
        self._last_prune_at = 0.0
        # Last broadcast activity per connected user (for spectator catch-up).
        self._latest_updates: dict[str, ActivityUpdate] = {}
        # Last declared intent per connected user (for spectator catch-up and
        # the edit-check escalation heuristic).
        self._latest_intents: dict[str, IntentUpdate] = {}
        # Latest declared plan per connected member (replaced on each plan).
        self._plans: dict[str, PlanUpdate] = {}
        # decision_id -> votes, least recently used first; bounded.
        self._decisions: OrderedDict[str, _DecisionVotes] = OrderedDict()
        # Last accepted activity timestamp per member (server rate limiting).
        # (user_id, channel) -> last accepted timestamp; channels are
        # rate-limited independently (see is_activity_rate_limited).
        self._last_activity_at: dict[tuple[str, str], float] = {}
        self._lock = asyncio.Lock()
        # Queued room broadcasts → flushed as one synergo_feed every 500ms.
        self._broadcast_queue: list[dict] = []
        self._flush_task: Optional[asyncio.Task] = None

    # --- membership ----------------------------------------------------------

    def is_taken(self, user_id: str) -> bool:
        return user_id in self._members

    def is_active(self, user_id: str) -> bool:
        """True if user_id is still connected. Intentionally the same check as
        is_taken: a disconnected peer's last touch is excluded from conflict
        comparisons (recent_other_touches(active_only=True)) even if it
        happened moments ago — there's no point flagging a conflict with
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
        """True once no members and no spectators remain — safe to evict from
        the RoomManager."""
        return not self._members and not self._spectators

    def add_member(
        self, user_id: str, socket: WebSocket, agent: AgentKind = "unknown"
    ) -> None:
        self._members[user_id] = socket
        self._member_agents[user_id] = agent

    def member_agent(self, user_id: str) -> AgentKind:
        return self._member_agents.get(user_id, "unknown")

    def remove_member(self, user_id: str) -> None:
        self._members.pop(user_id, None)
        self._member_agents.pop(user_id, None)
        self._latest_updates.pop(user_id, None)
        self._latest_intents.pop(user_id, None)
        self._plans.pop(user_id, None)
        for key in [k for k in self._last_activity_at if k[0] == user_id]:
            self._last_activity_at.pop(key, None)

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
            "Room settings changed by %s: block %gs -> %gs, warn %gs -> %gs",
            set_by or "the relay",
            self._settings.block_window_seconds,
            settings.block_window_seconds,
            self._settings.warn_window_seconds,
            settings.warn_window_seconds,
        )
        self._settings = settings.model_copy()
        self._settings_set_by = set_by
        self._settings_updated_at = time.time()
        return self.settings_update()

    def add_spectator(self, socket: WebSocket) -> None:
        self._spectators.add(socket)

    def remove_spectator(self, socket: WebSocket) -> None:
        self._spectators.discard(socket)

    def is_activity_rate_limited(
        self,
        user_id: str,
        min_interval_seconds: float = 0.5,
        channel: str = "activity",
    ) -> bool:
        """Return True if this user sent this kind of message too recently.

        On acceptance, records the current server time for the next check.
        Each channel gets its own budget: an agent broadcasts its intent and
        then edits within milliseconds, so a shared budget would drop the first
        activity of every prompt.
        """
        now = time.time()
        key = (user_id, channel)
        last = self._last_activity_at.get(key, 0.0)
        if now - last < min_interval_seconds:
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
                del touches[key]
            if not touches:
                del self._file_activity[file_path]

    def record_activity(self, message: ActivityMessage) -> None:
        self._prune_file_activity(message.timestamp)
        for change in message.changes:
            touches = self._file_activity.setdefault(change.file_path, {})
            touches[(message.repo_id, message.user_id)] = FileTouch(
                user_id=message.user_id,
                file_path=change.file_path,
                diff=change.diff,
                prompt=message.prompt,
                timestamp=message.timestamp,
                agent=message.agent,
                repo_id=message.repo_id,
                symbols=list(change.symbols),
                line_ranges=[tuple(r) for r in change.line_ranges],
                branch=message.branch,
            )

    def remember_update(self, update: ActivityUpdate) -> None:
        self._latest_updates[update.user_id] = update

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
        )
        self._latest_intents[user_id] = update
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
        against peers — not against a just-written self entry.
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

        matches: list[FileTouch] = []
        for other_id, intent in self._latest_intents.items():
            if other_id == user_id or not self.is_active(other_id):
                continue
            if intent.repo_id != repo_id:
                continue
            if now - intent.timestamp > INTENT_WINDOW_SECONDS:
                continue
            text = intent.prompt.lower()
            if not any(needle in text for needle in needles):
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
        hard: Decision = (
            "block" if block_window > 0 and now - touch.timestamp <= block_window else "warn"
        )
        if symbols and touch.symbols:
            if symbols_intersect(symbols, touch.symbols):
                return hard, "symbol"
            if always_warn:
                return hard, "file"
            # Symbol extraction can mislabel a hunk on a definition boundary;
            # literally overlapping lines are a collision whatever the names say.
            if line_ranges and touch.line_ranges and ranges_near(
                line_ranges, touch.line_ranges, proximity=0
            ):
                return hard, "lines"
            return "warn", "symbol"
        if line_ranges and touch.line_ranges:
            if ranges_near(line_ranges, touch.line_ranges):
                return hard, "lines"
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
            for other_id, plan in self._plans.items()
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
        """Active peers whose plan names this file or a requested symbol."""
        matches: list[FileTouch] = []
        for plan in self._active_plans(now, user_id, repo_id):
            if file_path not in plan.files and not symbols_intersect(symbols, plan.symbols):
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
        )
        self._plans[message.user_id] = update
        return update

    def plan_conflicts(self, plan: PlanUpdate, now: float) -> list[PlanConflict]:
        """Other members' active plans and recent edits this plan collides with,
        by file or by symbol. One entry per (file, user, kind), newest first."""
        conflicts: dict[tuple[str, str, str], PlanConflict] = {}
        planned_files = set(plan.files)
        for other in self._active_plans(now, plan.user_id, plan.repo_id):
            shared = [f for f in other.files if f in planned_files]
            if not shared and symbols_intersect(plan.symbols, other.symbols):
                shared = list(other.files[:1]) or [""]
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
                    ),
                )
        window = self._settings.warn_window_seconds
        for file_path, touches in self._file_activity.items():
            for (touch_repo, other_id), touch in touches.items():
                if other_id == plan.user_id or touch_repo != plan.repo_id:
                    continue
                if not self.is_active(other_id) or now - touch.timestamp > window:
                    continue
                if file_path not in planned_files and not symbols_intersect(
                    plan.symbols, touch.symbols
                ):
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
                    ),
                )
        return sorted(conflicts.values(), key=lambda conflict: conflict.seconds_ago)

    def active_plans(self, now: Optional[float] = None) -> list[PlanUpdate]:
        """Unexpired plans of connected members (spectator catch-up)."""
        now = time.time() if now is None else now
        return [plan for plan in self._plans.values() if plan.expires_at > now]

    # --- decision feedback ---------------------------------------------------

    def register_decision(self, decision_id: str, scope: DecisionScope) -> None:
        """Track a broadcast warn/block so dashboards can vote on it."""
        self._decisions[decision_id] = _DecisionVotes(scope=scope)
        self._decisions.move_to_end(decision_id)
        while len(self._decisions) > MAX_TRACKED_DECISIONS:
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
        """Queue a room-wide event and flush as a Synergo feed batch (~500ms).

        Point-to-point replies still use send() and go out immediately.
        """
        payload = message.model_dump()
        async with self._lock:
            self._broadcast_queue.append(payload)
            if self._flush_task is None or self._flush_task.done():
                self._flush_task = asyncio.create_task(self._flush_broadcast_queue())

    async def _flush_broadcast_queue(self) -> None:
        await asyncio.sleep(BROADCAST_FLUSH_SECONDS)
        async with self._lock:
            events = self._broadcast_queue
            self._broadcast_queue = []
            self._flush_task = None
        if not events:
            return
        feed = SynergoFeed(events=events)
        log.debug("Flushing Synergo feed (%d event(s))", len(events))
        await self._deliver(feed.model_dump())

    async def _deliver(self, payload: dict) -> None:
        targets = list(self._members.items()) + [(None, s) for s in self._spectators]
        dead_members: list[str] = []
        dead_spectators: list[WebSocket] = []
        async with self._lock:
            for user_id, socket in targets:
                try:
                    await socket.send_json(payload)
                except Exception:
                    log.warning("Dropping unreachable socket (user=%s)", user_id)
                    if user_id is None:
                        dead_spectators.append(socket)
                    else:
                        dead_members.append(user_id)
        for user_id in dead_members:
            self._members.pop(user_id, None)
            self._member_agents.pop(user_id, None)
        for socket in dead_spectators:
            self._spectators.discard(socket)

    async def send(self, socket: WebSocket, message: BaseModel) -> None:
        await socket.send_json(message.model_dump())
