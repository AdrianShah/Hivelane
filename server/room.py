"""In-memory state for one workspace ("room"). A process may hold many Room
instances at once (see room_manager.RoomManager) — each is fully isolated.
Ephemeral by design: nothing here survives a server restart."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Optional

from fastapi import WebSocket
from pydantic import BaseModel

try:
    from .models import (
        ActivityMessage,
        ActivityUpdate,
        AgentKind,
        Decision,
        IntentUpdate,
        SynergoFeed,
    )
except ImportError:  # Render rootDir=server
    from models import (
        ActivityMessage,
        ActivityUpdate,
        AgentKind,
        Decision,
        IntentUpdate,
        SynergoFeed,
    )

log = logging.getLogger(__name__)

# How long a peer's last touch of a file stays eligible for conflict comparison.
CONFLICT_WINDOW_SECONDS = 60.0

# Inside this window a peer is presumed mid-edit, so an incoming edit is denied
# outright rather than merely flagged.
EDIT_BLOCK_WINDOW_SECONDS = 20.0

# How long a declared intent still describes what a peer is doing. Longer than
# the touch window because stating a task precedes editing, but bounded: an
# unbounded intent warns about the same file for the rest of the session.
INTENT_WINDOW_SECONDS = 300.0

# Debounce room-wide broadcasts into a single Synergo feed frame.
BROADCAST_FLUSH_SECONDS = 0.5


@dataclass
class FileTouch:
    user_id: str
    file_path: str
    diff: str
    prompt: Optional[str]
    timestamp: float
    agent: AgentKind = "unknown"


@dataclass
class FileOverlap:
    """One file that the current user and another active user both touched recently."""

    file_path: str
    current_diff: str
    other: FileTouch


class Room:
    def __init__(self) -> None:
        self._members: dict[str, WebSocket] = {}
        self._member_agents: dict[str, AgentKind] = {}
        self._spectators: set[WebSocket] = set()
        # file_path -> {user_id: FileTouch} — latest edit per user per file.
        self._file_activity: dict[str, dict[str, FileTouch]] = {}
        # Last broadcast activity per connected user (for spectator catch-up).
        self._latest_updates: dict[str, ActivityUpdate] = {}
        # Last declared intent per connected user (for spectator catch-up and
        # the edit-check escalation heuristic).
        self._latest_intents: dict[str, IntentUpdate] = {}
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
        for key in [k for k in self._last_activity_at if k[0] == user_id]:
            self._last_activity_at.pop(key, None)

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

    def record_activity(self, message: ActivityMessage) -> None:
        for change in message.changes:
            touches = self._file_activity.setdefault(change.file_path, {})
            touches[message.user_id] = FileTouch(
                user_id=message.user_id,
                file_path=change.file_path,
                diff=change.diff,
                prompt=message.prompt,
                timestamp=message.timestamp,
                agent=message.agent,
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
    ) -> None:
        self._latest_intents[user_id] = IntentUpdate(
            user_id=user_id,
            agent=agent,
            prompt=prompt,
            timestamp=timestamp,
        )

    def latest_intents(self) -> list[IntentUpdate]:
        return list(self._latest_intents.values())

    def recent_other_touches(
        self,
        file_path: str,
        user_id: str,
        now: float,
        *,
        active_only: bool = True,
        window_seconds: float = CONFLICT_WINDOW_SECONDS,
    ) -> list[FileTouch]:
        """Other users' recent touches of the same file within the conflict window."""
        touches = self._file_activity.get(file_path, {})
        results: list[FileTouch] = []
        for other_id, touch in touches.items():
            if other_id == user_id:
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
                change.file_path, message.user_id, message.timestamp
            )
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
        self, file_path: str, user_id: str, now: float
    ) -> list[FileTouch]:
        """Active peers whose recent declared intent names this file, newest first.

        Matching on the bare stem as well as the basename catches prompts that
        talk about the module rather than the file ("refactor the Room
        broadcast" vs server/room.py). Stems under 3 characters are skipped
        because they match almost any sentence.
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
                )
            )
        matches.sort(key=lambda touch: touch.timestamp, reverse=True)
        return matches

    def check_edit(
        self, user_id: str, file_path: str, now: float
    ) -> tuple[Decision, list[FileTouch]]:
        """Decide whether user_id may edit file_path right now.

        Returns the decision plus the peers that justified it, newest first.
        """
        others = self.recent_other_touches(
            file_path,
            user_id,
            now,
            active_only=True,
            window_seconds=CONFLICT_WINDOW_SECONDS,
        )
        others.sort(key=lambda touch: touch.timestamp, reverse=True)

        blocking = [
            touch
            for touch in others
            if now - touch.timestamp <= EDIT_BLOCK_WINDOW_SECONDS
        ]
        if blocking:
            return "block", blocking
        if others:
            return "warn", others

        mentions = self._intent_mentions(file_path, user_id, now)
        if mentions:
            return "warn", mentions
        return "allow", []

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
