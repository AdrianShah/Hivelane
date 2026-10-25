"""Per-file wait queues: who gets a file next once its holder lets go.

A blocked edit_check (or an explicit file_request) puts the requester in line
for that (repo_id, file_path). The relay then works the line itself:

* **Holders** are the members whose touch of the file still blocks the
  person at the head of the line (Room.evaluate_edit, with the head's own
  symbols and line ranges), plus anyone holding a fresh *claim* (below).
  Each holder hears once per requester, via file_requested, that someone is
  waiting.
* **Release.** When the head has no holders left (every blocking touch aged
  past the block window, or its owner left), the head is sent file_released
  and gets RELEASE_GRACE_SECONDS to take the file. During the grace every
  other member's edit_check on the file is blocked behind the head, even
  though no touch blocks it any more: being next in line has to mean
  something. If a holder comes back (edits again) the release is withdrawn
  and the head waits again.
* **Take.** An edit_check that passes (allow/warn) takes the requester out of
  the line. If others are still waiting, the taker holds a *claim*: its edit
  has been allowed but its activity has not reached the relay yet, so
  without the claim the next in line would be released into the middle of
  the taker's edit. A claim ends when the taker's touch arrives (the touch
  then blocks as any touch does), after CLAIM_MAX_SECONDS (or the block
  window, if shorter), or when the taker leaves. Only a member that was in
  the line takes a claim.
* **Expiry.** An entry lasts the warn window from its latest request (a
  blocked retry or a file_request refreshes it, keeping its place), a
  released head lasts its grace, and an entry goes as soon as its member
  leaves. Lines are bounded (MAX_QUEUE_LENGTH, one entry per member) and so
  is the number of files with a line (MAX_QUEUED_FILES).

Nothing here can deadlock: every state that makes anyone wait ends on a
clock, a touch after the block window, a claim after the block window, a
release after the grace, an entry after the warn window, and when a grace
lapses the next in line is considered in the same pass. Two agents each
holding the file the other wants stop editing (both are blocked), so both
touches lapse and both lines move.

Pure bookkeeping: no sockets, no clock of its own. Room supplies the
decision engine and delivers the notices sweep() returns.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Literal, Optional

try:
    from . import config
except ImportError:  # Render rootDir=server
    import config  # type: ignore[no-redef]

# The bounds below come from server/config.py; 0 means no bound.
#
# One file's line (HIVELANE_FILE_QUEUE_MAX_LENGTH). A member is in a line at
# most once, so the default of 0 bounds it by the room's membership.
MAX_QUEUE_LENGTH = config.FILE_QUEUE_MAX_LENGTH
# Files with a line at once, per room (HIVELANE_FILE_QUEUE_MAX_FILES).
MAX_QUEUED_FILES = config.FILE_QUEUE_MAX_FILES
# Paths longer than this are not queued (a relay-side memory bound; real repo
# paths are far shorter, and an e2e hash is 34 characters).
# HIVELANE_MAX_PATH_CHARS.
MAX_QUEUED_PATH_CHARS = config.MAX_PATH_CHARS
# How long the head of a line has to take a released file before the next
# member in line is considered.
RELEASE_GRACE_SECONDS = 10.0
# A claim covers the gap between an allowed edit and its activity reaching
# the relay (the daemon batches for 1.5 s), so it lapses well before the block
# window: a taker whose edit produced no activity (a failed tool call, an
# identical write) must not hold everyone off for the whole window.
CLAIM_MAX_SECONDS = 5.0
# The room's periodic sweep, while any line or claim exists. Lines are also
# swept lazily on every member message.
SWEEP_INTERVAL_SECONDS = 1.0

# (repo_id, file_path): a file's identity on the relay (see models.RepoId).
QueueKey = tuple[str, str]


@dataclass
class QueueEntry:
    """One member waiting for one file."""

    user_id: str
    agent: str
    enqueued_at: float
    refreshed_at: float
    # The e2e blob of the request that queued it, copied onto the
    # file_requested a holder gets and the file_released the member gets.
    e2e: str = ""
    # What the member last asked to edit, so its release is judged at the
    # same granularity as its edit_check was.
    symbols: list[str] = field(default_factory=list)
    line_ranges: list[tuple[int, int]] = field(default_factory=list)
    branch: str = ""
    # Set while the member is at the head and the file has been released to it.
    released_at: Optional[float] = None
    # Holders already told this member is waiting.
    notified: set[str] = field(default_factory=set)


@dataclass
class QueueNotice:
    """A point-to-point frame Room has to send: file_released to `to_user`
    (whose own entry is `entry`), or file_requested to a holder `to_user`
    about the waiting `entry`."""

    kind: Literal["released", "requested"]
    to_user: str
    key: QueueKey
    entry: QueueEntry
    at: float


# (key, head entry, now) -> user_ids whose touches or claims keep the head out.
BlockersFn = Callable[[QueueKey, QueueEntry, float], list[str]]
# (key, claimer, claimed_at) -> True once the claimer's own touch has arrived.
SettledFn = Callable[[QueueKey, str, float], bool]


class FileQueues:
    def __init__(
        self,
        *,
        grace_seconds: float = RELEASE_GRACE_SECONDS,
        max_length: int = MAX_QUEUE_LENGTH,
        max_files: int = MAX_QUEUED_FILES,
    ) -> None:
        self.grace_seconds = grace_seconds
        self.max_length = max_length
        self.max_files = max_files
        self._queues: dict[QueueKey, list[QueueEntry]] = {}
        # key -> {user_id: claimed_at}
        self._claims: dict[QueueKey, dict[str, float]] = {}

    def __bool__(self) -> bool:
        """True while any line or claim needs sweeping."""
        return bool(self._queues or self._claims)

    # --- reading ----------------------------------------------------------------

    def keys(self) -> list[QueueKey]:
        return list(self._queues)

    def queue(self, key: QueueKey) -> list[QueueEntry]:
        return list(self._queues.get(key, ()))

    def head(self, key: QueueKey) -> Optional[QueueEntry]:
        queue = self._queues.get(key)
        return queue[0] if queue else None

    def entry(self, key: QueueKey, user_id: str) -> Optional[QueueEntry]:
        for entry in self._queues.get(key, ()):
            if entry.user_id == user_id:
                return entry
        return None

    def position(self, key: QueueKey, user_id: str) -> int:
        """1-based place of user_id in key's line; 0 when not in it."""
        for index, entry in enumerate(self._queues.get(key, ())):
            if entry.user_id == user_id:
                return index + 1
        return 0

    def claim_keys(self) -> list[QueueKey]:
        return list(self._claims)

    def claimers(self, key: QueueKey, *, exclude: str = "") -> dict[str, float]:
        """Members holding a claim on key (user_id -> claimed_at), as of the
        last sweep, other than exclude."""
        return {
            user_id: at
            for user_id, at in self._claims.get(key, {}).items()
            if user_id != exclude
        }

    # --- writing ----------------------------------------------------------------

    def enqueue(
        self,
        key: QueueKey,
        user_id: str,
        agent: str,
        now: float,
        *,
        e2e: str = "",
        symbols: Optional[list[str]] = None,
        line_ranges: Optional[list[tuple[int, int]]] = None,
        branch: str = "",
    ) -> int:
        """Join key's line, or refresh an existing place in it (same position,
        expiry restarted, latest request kept). Returns the 1-based position;
        0 when the line or the room's table of lines is full."""
        if MAX_QUEUED_PATH_CHARS > 0 and len(key[1]) > MAX_QUEUED_PATH_CHARS:
            return 0
        queue = self._queues.get(key)
        if queue is not None:
            for index, entry in enumerate(queue):
                if entry.user_id == user_id:
                    entry.refreshed_at = now
                    if e2e:
                        entry.e2e = e2e
                    if symbols is not None:
                        entry.symbols = list(symbols)
                    if line_ranges is not None:
                        entry.line_ranges = [tuple(r) for r in line_ranges]
                    entry.branch = branch or entry.branch
                    return index + 1
            if self.max_length > 0 and len(queue) >= self.max_length:
                return 0
        elif self.max_files > 0 and len(self._queues) >= self.max_files:
            return 0
        else:
            queue = self._queues[key] = []
        queue.append(
            QueueEntry(
                user_id=user_id,
                agent=agent,
                enqueued_at=now,
                refreshed_at=now,
                e2e=e2e,
                symbols=list(symbols or []),
                line_ranges=[tuple(r) for r in (line_ranges or [])],
                branch=branch,
            )
        )
        return len(queue)

    def take(self, key: QueueKey, user_id: str, now: float) -> None:
        """user_id's edit of key was just allowed: out of the line, and a
        claim on the file while anyone else still waits for it.

        Only a member that was in the line takes a claim. Anyone else whose
        edit passes (a different function, say) never queued for the file,
        and must not block everyone behind a claim it never asked for."""
        queue = self._queues.get(key)
        if queue is None:
            return
        if not any(entry.user_id == user_id for entry in queue):
            return
        queue[:] = [entry for entry in queue if entry.user_id != user_id]
        if queue:
            self._claims.setdefault(key, {})[user_id] = now
        else:
            del self._queues[key]

    def remove_user(self, user_id: str) -> None:
        """A member left: its places in line and its claims go with it."""
        for key in list(self._queues):
            queue = self._queues[key]
            queue[:] = [entry for entry in queue if entry.user_id != user_id]
            if not queue:
                del self._queues[key]
        for key in list(self._claims):
            self._claims[key].pop(user_id, None)
            if not self._claims[key]:
                del self._claims[key]

    def clear(self) -> None:
        self._queues.clear()
        self._claims.clear()

    # --- the sweep -------------------------------------------------------------

    def sweep(
        self,
        now: float,
        *,
        warn_window: float,
        block_window: float,
        blockers: BlockersFn,
        settled: SettledFn,
        is_active: Callable[[str], bool],
    ) -> list[QueueNotice]:
        """Expire what has lapsed, then release or keep each line's head.

        Returns the notices to deliver: file_released for a head whose file
        just freed up, file_requested for a holder not yet told about the
        head waiting on it.
        """
        for key in list(self._claims):
            claims = self._claims[key]
            for user_id, at in list(claims.items()):
                if (
                    not is_active(user_id)
                    or now - at > min(block_window, CLAIM_MAX_SECONDS)
                    or settled(key, user_id, at)
                ):
                    del claims[user_id]
            if not claims:
                del self._claims[key]

        notices: list[QueueNotice] = []
        for key in list(self._queues):
            queue = self._queues[key]
            queue[:] = [entry for entry in queue if self._alive(entry, now, warn_window, is_active)]
            if not queue:
                del self._queues[key]
                continue
            head = queue[0]
            holders = [user for user in blockers(key, head, now) if user != head.user_id]
            if holders:
                # Someone holds the file (again): the head keeps waiting.
                head.released_at = None
                for holder in holders:
                    if holder not in head.notified:
                        head.notified.add(holder)
                        notices.append(QueueNotice("requested", holder, key, head, now))
            elif head.released_at is None:
                head.released_at = now
                notices.append(QueueNotice("released", head.user_id, key, head, now))
        return notices

    def _alive(
        self,
        entry: QueueEntry,
        now: float,
        warn_window: float,
        is_active: Callable[[str], bool],
    ) -> bool:
        if not is_active(entry.user_id):
            return False
        if entry.released_at is not None:
            # A released head that did not take the file in its grace makes
            # way for the next in line.
            return now - entry.released_at <= self.grace_seconds
        return now - entry.refreshed_at <= warn_window
