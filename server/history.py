"""Opt-in room history, for the dashboard's replay view.

A room whose settings say ``history_hours > 0`` (a team's committed
``.hivelane.toml``, sent on join, or the settings given to POST /rooms) keeps
the feed events the relay broadcast to it. What is kept is exactly what
spectators were sent: the ``hivelane_feed`` events, field for field, so an
end-to-end encrypted room keeps only keyed hashes and the members' ciphertext
blobs, never anything the relay could not already read.

Bounded three ways per room, oldest events dropped first:

- at most HISTORY_MAX_EVENTS events,
- at most HISTORY_MAX_BYTES of them (the events' compact JSON size),
- none older than the room's history_hours (at most MAX_HISTORY_HOURS),

and HISTORY_TOTAL_MAX_BYTES across every room in the process, so a hundred
busy rooms cannot add up to an out-of-memory relay.

In memory only: never written to the room store, disk or Redis. A relay
restart loses it, a room that empties out and is evicted takes its history
with it, and setting history_hours back to 0 drops it at once.

The byte caps count compact JSON. Python objects take several times that in
resident memory (roughly 3-8x), which is why the defaults (config.py) are
sized for a 512 MB VM rather than to the JSON.

A member or dashboard asks with ``history_request {since}`` and gets the kept
events newer than ``since`` as ``history_chunk`` frames of at most
HISTORY_CHUNK_EVENTS events and about HISTORY_CHUNK_MAX_BYTES (so no frame
exceeds a client's 1 MiB default frame limit), the last one with
``done: true`` (one empty, done chunk when nothing is kept). Replays are
throttled three ways: each socket may ask once every
HISTORY_REQUEST_INTERVAL_SECONDS, each client address (IPv6 by /64)
HISTORY_REQUESTS_PER_IP_PER_MINUTE times a minute, and each room may send at
most HISTORY_ROOM_REPLAY_BYTES_PER_MINUTE of replay a minute, so opening many
sockets does not multiply the egress. Feed events that carry no ``timestamp`` of
their own (user_joined, user_left, ...) get the relay's broadcast time as
``timestamp`` in the chunk, so a replay can place them; every other field is
as broadcast.
"""

from __future__ import annotations

import json
import logging
import math
import time
import weakref
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Optional

from pydantic import ValidationError

try:
    from . import config, limits, metrics
    from .models import HistoryChunk, HistoryRequest
except ImportError:  # Render rootDir=server
    if __package__:  # imported as server.*: a real missing dependency, not the flat tree
        raise
    import config  # type: ignore[no-redef]
    import limits  # type: ignore[no-redef]
    import metrics  # type: ignore[no-redef]
    from models import HistoryChunk, HistoryRequest  # type: ignore[no-redef]

log = logging.getLogger(__name__)

# Per-room bounds. Large enough for a busy afternoon (a member sends at most
# two activity messages a second, and most frames are a few KB), small enough
# that a room cannot make the relay hold more than this. From server/config.py
# (HIVELANE_HISTORY_MAX_EVENTS, HIVELANE_HISTORY_MAX_BYTES); 0 means no bound.
HISTORY_MAX_EVENTS = config.HISTORY_MAX_EVENTS
HISTORY_MAX_BYTES = config.HISTORY_MAX_BYTES
# Across every room in this process (HIVELANE_HISTORY_TOTAL_MAX_BYTES).
HISTORY_TOTAL_MAX_BYTES = config.HISTORY_TOTAL_MAX_BYTES
# The same bound RoomSettings.history_hours has on the wire.
MAX_HISTORY_HOURS = 24.0
# Events per history_chunk frame: 200 typical events are well under the
# relay's own 512 KB inbound frame cap, so a client never sees a frame larger
# than it would send.
HISTORY_CHUNK_EVENTS = 200
# And at most about this many bytes of events per chunk (one event larger
# than this still goes, alone in its chunk).
HISTORY_CHUNK_MAX_BYTES = 256 * 1024
# One history_request per socket per this many seconds; more are ignored.
HISTORY_REQUEST_INTERVAL_SECONDS = 10.0
# history_requests per client address (IPv6 by /64) per minute.
HISTORY_REQUESTS_PER_IP_PER_MINUTE = 12
# Replay bytes one room may send per minute, over all its sockets.
HISTORY_ROOM_REPLAY_BYTES_PER_MINUTE = 16 * 1024 * 1024

# Every live RoomHistory, for the process-wide cap and the metrics gauges. A
# WeakSet so a room that is dropped without clear() stops counting anyway.
_live: "weakref.WeakSet[RoomHistory]" = weakref.WeakSet()


def total_events() -> int:
    """Events kept across every room in this process (hivelane_history_events)."""
    return sum(history.event_count for history in list(_live))


def total_bytes() -> int:
    """Bytes kept across every room in this process (hivelane_history_bytes)."""
    return sum(history.byte_count for history in list(_live))


def clamp_hours(hours: object) -> float:
    """A usable history_hours: finite, 0..MAX_HISTORY_HOURS, else 0 (off)."""
    if isinstance(hours, bool) or not isinstance(hours, (int, float)):
        return 0.0
    value = float(hours)
    if not math.isfinite(value) or value <= 0:
        return 0.0
    return min(value, MAX_HISTORY_HOURS)


def event_size(event: dict) -> int:
    """The event's compact JSON size in UTF-8 bytes (what the caps count)."""
    return len(
        json.dumps(event, separators=(",", ":"), ensure_ascii=False, default=str).encode(
            "utf-8"
        )
    )


@dataclass(slots=True)
class _Kept:
    at: float  # relay clock (time.time) when the event was broadcast
    size: int
    event: dict


class RoomHistory:
    """One room's ring buffer. Disabled (keeps nothing) while hours is 0."""

    def __init__(
        self,
        hours: float = 0.0,
        *,
        max_events: int = HISTORY_MAX_EVENTS,
        max_bytes: int = HISTORY_MAX_BYTES,
        total_max_bytes: int = HISTORY_TOTAL_MAX_BYTES,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._hours = clamp_hours(hours)
        self.max_events = max_events
        self.max_bytes = max_bytes
        self.total_max_bytes = total_max_bytes
        self._clock = clock
        self._events: deque[_Kept] = deque()
        self._bytes = 0
        _live.add(self)

    # --- configuration -------------------------------------------------------

    @property
    def hours(self) -> float:
        return self._hours

    @property
    def enabled(self) -> bool:
        return self._hours > 0

    @property
    def max_age_seconds(self) -> float:
        return self._hours * 3600.0

    def configure(self, hours: object) -> None:
        """Apply a room's history_hours. 0 turns history off and drops what
        was kept; a shorter window drops what is now too old."""
        self._hours = clamp_hours(hours)
        if not self.enabled:
            self.clear()
        else:
            self._prune(self._clock())

    # --- contents ------------------------------------------------------------

    @property
    def event_count(self) -> int:
        return len(self._events)

    @property
    def byte_count(self) -> int:
        return self._bytes

    def clear(self) -> None:
        self._events.clear()
        self._bytes = 0

    def _drop_oldest(self) -> None:
        kept = self._events.popleft()
        self._bytes -= kept.size

    def _prune(self, now: float) -> None:
        cutoff = now - self.max_age_seconds
        while self._events and self._events[0].at < cutoff:
            self._drop_oldest()
        while self._events and (
            (self.max_events > 0 and len(self._events) > self.max_events)
            or (self.max_bytes > 0 and self._bytes > self.max_bytes)
        ):
            self._drop_oldest()

    def record(self, events: Iterable[dict], now: float | None = None) -> None:
        """Keep a flushed feed batch (the dicts spectators were sent).

        A no-op while history is off. An event too large to fit the room's
        byte cap on its own is skipped rather than flushing everything else.
        """
        if not self.enabled:
            return
        moment = self._clock() if now is None else now
        for event in events:
            if not isinstance(event, dict):
                continue
            try:
                size = event_size(event)
            except (TypeError, ValueError):
                log.debug("History: skipping an event that is not JSON")
                continue
            if self.max_bytes > 0 and size > self.max_bytes:
                log.debug("History: skipping a %d-byte event (cap %d)", size, self.max_bytes)
                continue
            self._events.append(_Kept(moment, size, event))
            self._bytes += size
        self._prune(moment)
        self._enforce_process_cap()

    def _enforce_process_cap(self) -> None:
        """Over the process-wide cap: the room adding events gives up its own
        oldest first, so a quiet room's replay is not erased by a busy one."""
        if self.total_max_bytes <= 0:
            return
        excess = total_bytes() - self.total_max_bytes
        while excess > 0 and self._events:
            excess -= self._events[0].size
            self._drop_oldest()

    def _since_sized(self, since: float, now: float | None) -> list[tuple[dict, int]]:
        if not self.enabled:
            return []
        self._prune(self._clock() if now is None else now)
        out: list[tuple[dict, int]] = []
        for kept in self._events:
            if kept.at <= since:
                continue
            event = kept.event
            if "timestamp" not in event:
                event = {**event, "timestamp": kept.at}
            out.append((event, kept.size))
        return out

    def since(self, since: float = 0.0, now: float | None = None) -> list[dict]:
        """Kept events broadcast after `since` (relay clock), oldest first.

        Events without a timestamp of their own get the broadcast time as
        `timestamp` (a copy; what is kept stays as it was sent)."""
        return [event for event, _ in self._since_sized(since, now)]

    def chunks(
        self,
        since: float = 0.0,
        now: float | None = None,
        size: int = HISTORY_CHUNK_EVENTS,
        max_bytes: int = HISTORY_CHUNK_MAX_BYTES,
    ) -> list[list[dict]]:
        """since() split into history_chunk payloads of at most `size` events
        and about `max_bytes`; always at least one, so the requester always
        gets a frame with done=true."""
        events = self._since_sized(since, now)
        if not events:
            return [[]]
        out: list[list[dict]] = []
        current: list[dict] = []
        current_bytes = 0
        for event, event_bytes in events:
            if current and (
                len(current) >= size or (max_bytes > 0 and current_bytes + event_bytes > max_bytes)
            ):
                out.append(current)
                current, current_bytes = [], 0
            current.append(event)
            current_bytes += event_bytes
        out.append(current)
        return out


class RequestThrottle:
    """One history_request per socket per `interval` seconds.

    Keyed weakly by the socket object, so a closed socket's entry goes with it.
    """

    def __init__(
        self,
        interval: float = HISTORY_REQUEST_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.interval = interval
        self._clock = clock
        self._last: "weakref.WeakKeyDictionary[object, float]" = weakref.WeakKeyDictionary()

    def allow(self, key: object) -> bool:
        now = self._clock()
        last = self._last.get(key)
        if last is not None and now - last < self.interval:
            return False
        self._last[key] = now
        return True

    def reset(self) -> None:
        self._last = weakref.WeakKeyDictionary()


# One throttle for the process: sockets are unique across rooms.
request_throttle = RequestThrottle()

# Per client address, across every socket it opens.
ip_limiter = limits.FixedWindowLimiter(
    limit=HISTORY_REQUESTS_PER_IP_PER_MINUTE,
    window_seconds=60,
    max_keys=config.RATE_LIMIT_MAX_TRACKED_IPS,
)


class RoomReplayBudget:
    """Replay bytes per room per minute (a token bucket per room, keyed
    weakly by the Room so an evicted room's entry goes with it)."""

    def __init__(
        self,
        bytes_per_minute: int = HISTORY_ROOM_REPLAY_BYTES_PER_MINUTE,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.bytes_per_minute = bytes_per_minute
        self._clock = clock
        self._state: "weakref.WeakKeyDictionary[object, list[float]]" = weakref.WeakKeyDictionary()

    def take(self, room: object, amount: int) -> bool:
        """Spend `amount` bytes of the room's budget, if it has them."""
        if self.bytes_per_minute <= 0:
            return True
        now = self._clock()
        try:
            state = self._state.get(room)
        except TypeError:  # not weak-referenceable (a test double)
            return True
        if state is None:
            state = [float(self.bytes_per_minute), now]
            self._state[room] = state
        tokens = min(
            float(self.bytes_per_minute),
            state[0] + (now - state[1]) * self.bytes_per_minute / 60.0,
        )
        state[1] = now
        # One replay larger than a whole minute's budget is still allowed
        # from a full bucket, so a big history is not unreachable.
        if tokens < min(amount, self.bytes_per_minute):
            state[0] = tokens
            return False
        state[0] = tokens - amount
        return True

    def reset(self) -> None:
        self._state = weakref.WeakKeyDictionary()


room_budget = RoomReplayBudget()


def _ip_key(socket: Any) -> Optional[str]:
    try:
        return limits.rate_key(socket)
    except Exception:  # a test double without headers/client
        return None


async def handle_request(room: Any, socket: Any, raw: dict) -> None:
    """Answer a history_request from a member or dashboard socket.

    Point-to-point and immediate (never through the feed), to the requester
    only. A room keeping no history answers with one empty, done chunk, so a
    client can tell "nothing kept" from "no answer". Requests over the
    per-socket budget are ignored. `room` is a server.room.Room (untyped here
    to keep this module free of a room import).
    """
    try:
        request = HistoryRequest.model_validate(raw)
    except ValidationError as exc:
        metrics.messages_dropped_total.inc("invalid")
        log.debug("Ignoring malformed history_request (%d error(s))", exc.error_count())
        return
    if not request_throttle.allow(socket):
        log.debug("Ignoring a history_request inside the %gs budget", request_throttle.interval)
        return
    key = _ip_key(socket)
    if key is not None and not ip_limiter.check(key).allowed:
        log.debug("Ignoring a history_request over the per-address budget")
        return
    history: RoomHistory = room.history
    chunks = history.chunks(request.since)
    total = sum(event_size(event) for chunk in chunks for event in chunk)
    if not room_budget.take(room, total):
        log.debug("Ignoring a history_request over the room's replay budget")
        return
    log.debug(
        "Sending %d history event(s) in %d chunk(s)",
        sum(len(chunk) for chunk in chunks),
        len(chunks),
    )
    for index, events in enumerate(chunks):
        await room.send(socket, HistoryChunk(events=events, done=index == len(chunks) - 1))


# Totals only: a per-room label would make one series per room code (and put
# codes, which are secrets, into the metrics).
metrics.register_gauge(
    "hivelane_history_events",
    "Feed events kept for replay across every room with history on.",
    total_events,
)
metrics.register_gauge(
    "hivelane_history_bytes",
    "Bytes (compact JSON) of feed events kept for replay across every room.",
    total_bytes,
)


def reset_throttles() -> None:
    """Forget every replay throttle (tests)."""
    request_throttle.reset()
    ip_limiter.reset()
    room_budget.reset()
