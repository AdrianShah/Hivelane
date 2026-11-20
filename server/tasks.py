"""Room tasks: a shared task list that members claim under a lease.

A member creates a task (a title, an optional body, the files it covers and
the tasks it depends on). Anyone may claim a *ready* task: open, with every
dependency done or cancelled. The claim is a lease: it lapses unless renewed,
explicitly or by the claimer's own edits to the task's files (see
Room.queue_edit_check), and a lapsed task is open again. While a task is
claimed its files are *reserved*: another member's edit_check on one of them
is blocked (scope "task", Room.evaluate_edit), unless the claimer is one of
the requester's own names (its daemon's main name or agent lanes).

A disconnect does not release a claim (a daemon reconnects under the same
name); only done, release, cancel or the lease ending does.

Pure bookkeeping, like file_queue.py: no sockets and no clock of its own.
Room supplies `now` and broadcasts the tasks each call returns.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Optional

try:
    from .models import Task, TaskNote
except ImportError:  # Render rootDir=server
    from models import Task, TaskNote  # type: ignore[no-redef]

# Open and claimed tasks per room.
MAX_OPEN_TASKS = 200
# Done and cancelled tasks kept for the dashboard, most recent first.
MAX_CLOSED_TASKS = 50
# Notes per task (the oldest are dropped).
MAX_NOTES = 20
# Lease bounds (seconds); TaskRequest enforces the same range.
DEFAULT_LEASE_SECONDS = 1800.0
MIN_LEASE_SECONDS = 60.0
MAX_LEASE_SECONDS = 8 * 3600.0
# A lease renewed by the claimer's edits is re-broadcast at most this often,
# so a busy claimer does not turn every edit into a task_update.
RENEW_BROADCAST_SECONDS = 60.0

_CLOSED = ("done", "cancelled")


class TaskError(Exception):
    """An operation refused; `reason` is the task_reply reason."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def normalize_id(raw: str) -> str:
    """'t12', ' T12 ' and '12' all mean T12."""
    text = (raw or "").strip().upper()
    if text.isdigit():
        text = f"T{text}"
    return text


class TaskBoard:
    def __init__(
        self,
        *,
        max_open: int = MAX_OPEN_TASKS,
        max_closed: int = MAX_CLOSED_TASKS,
        max_notes: int = MAX_NOTES,
    ) -> None:
        self.max_open = max_open
        self.max_closed = max_closed
        self.max_notes = max_notes
        # Insertion order is creation order: "oldest ready" is the first match.
        self._tasks: dict[str, Task] = {}
        self._lease_seconds: dict[str, float] = {}
        self._announced_lease: dict[str, float] = {}
        self._next = 1

    # --- reading -------------------------------------------------------------

    def get(self, task_id: str) -> Task:
        task = self._tasks.get(normalize_id(task_id))
        if task is None:
            raise TaskError("not_found")
        return task

    def all(self) -> list[Task]:
        return list(self._tasks.values())

    def list(self, repo_id: Optional[str] = None, include_closed: bool = False) -> list[Task]:
        return [
            task
            for task in self._tasks.values()
            if (repo_id is None or task.repo_id == repo_id)
            and (include_closed or task.status not in _CLOSED)
        ]

    def is_ready(self, task: Task) -> bool:
        """Open, and nothing it depends on is still to do. A dependency that
        is gone (pruned once closed) no longer holds anything up."""
        if task.status != "open":
            return False
        for dep in task.depends_on:
            other = self._tasks.get(dep)
            if other is not None and other.status not in _CLOSED:
                return False
        return True

    def has_claims(self) -> bool:
        return any(task.status == "claimed" for task in self._tasks.values())

    def reservation_for(
        self, repo_id: str, path: str, requester: str, exempt: Iterable[str], now: float
    ) -> Optional[Task]:
        """The live claimed task reserving path for someone other than the
        requester (or anyone in exempt), or None."""
        mine = {requester.lower(), *(name.lower() for name in exempt)}
        for task in self._tasks.values():
            if (
                task.status == "claimed"
                and task.repo_id == repo_id
                and path in task.files
                and (task.lease_until or 0) > now
                and (task.claimer or "").lower() not in mine
            ):
                return task
        return None

    # --- writing -------------------------------------------------------------

    def create(
        self,
        user_id: str,
        now: float,
        *,
        repo_id: str = "",
        title: str = "",
        body: str = "",
        files: Iterable[str] = (),
        depends_on: Iterable[str] = (),
        e2e: str = "",
    ) -> Task:
        if not title.strip() and not e2e:
            raise TaskError("invalid")
        if len(self.list()) >= self.max_open:
            raise TaskError("limit")
        deps: list[str] = []
        for raw in depends_on:
            dep = normalize_id(raw)
            if dep not in self._tasks:
                raise TaskError("not_found")
            if dep not in deps:
                deps.append(dep)
        task = Task(
            task_id=f"T{self._next}",
            repo_id=repo_id,
            title=title.strip(),
            body=body,
            files=list(dict.fromkeys(f for f in files if f)),
            depends_on=deps,
            created_by=user_id,
            created_at=now,
            updated_at=now,
            e2e=e2e,
        )
        self._next += 1
        self._tasks[task.task_id] = task
        return task

    def claim(
        self,
        user_id: str,
        now: float,
        *,
        task_id: str = "",
        next_ready: bool = False,
        repo_id: str = "",
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
    ) -> Task:
        if next_ready:
            task = next(
                (t for t in self._tasks.values() if t.repo_id == repo_id and self.is_ready(t)),
                None,
            )
            if task is None:
                raise TaskError("none_ready")
        else:
            task = self.get(task_id)
            if task.status == "claimed" and task.claimer == user_id:
                return self.renew(user_id, now, task_id=task.task_id, lease_seconds=lease_seconds)
            if task.status == "claimed":
                raise TaskError("claimed")
            if not self.is_ready(task):
                raise TaskError("not_ready")
        lease = _clamp(lease_seconds)
        task.status = "claimed"
        task.claimer = user_id
        task.claimed_at = now
        task.lease_until = now + lease
        task.updated_at = now
        self._lease_seconds[task.task_id] = lease
        self._announced_lease[task.task_id] = task.lease_until
        return task

    def renew(
        self, user_id: str, now: float, *, task_id: str, lease_seconds: float = DEFAULT_LEASE_SECONDS
    ) -> Task:
        task = self._claimed_by(user_id, task_id)
        lease = _clamp(lease_seconds)
        task.lease_until = now + lease
        task.updated_at = now
        self._lease_seconds[task.task_id] = lease
        self._announced_lease[task.task_id] = task.lease_until
        return task

    def release(self, user_id: str, now: float, *, task_id: str) -> Task:
        task = self._claimed_by(user_id, task_id)
        self._reopen(task, now)
        return task

    def done(self, user_id: str, now: float, *, task_id: str, text: str = "", e2e: str = "") -> Task:
        task = self._claimed_by(user_id, task_id)
        self._close(task, "done", user_id, now, text, e2e)
        return task

    def cancel(self, user_id: str, now: float, *, task_id: str, text: str = "", e2e: str = "") -> Task:
        task = self.get(task_id)
        if task.status in _CLOSED:
            raise TaskError("closed")
        if user_id not in (task.created_by, task.claimer):
            raise TaskError("not_owner")
        self._close(task, "cancel", user_id, now, text, e2e)
        return task

    def note(self, user_id: str, now: float, *, task_id: str, text: str = "", e2e: str = "") -> Task:
        task = self.get(task_id)
        if user_id not in (task.created_by, task.claimer):
            raise TaskError("not_owner")
        if not text.strip() and not e2e:
            raise TaskError("invalid")
        self._add_note(task, TaskNote(user_id=user_id, text=text, timestamp=now, e2e=e2e))
        task.updated_at = now
        return task

    def renew_on_edit(self, names: Iterable[str], repo_id: str, path: str, now: float) -> list[Task]:
        """The claimer edited one of its task's files: extend the lease.
        Returns the tasks whose new lease is worth announcing."""
        mine = {name.lower() for name in names}
        announce: list[Task] = []
        for task in self._tasks.values():
            if (
                task.status == "claimed"
                and task.repo_id == repo_id
                and path in task.files
                and (task.claimer or "").lower() in mine
                and (task.lease_until or 0) > now
            ):
                lease = self._lease_seconds.get(task.task_id, DEFAULT_LEASE_SECONDS)
                task.lease_until = max(task.lease_until or 0, now + lease)
                if task.lease_until - self._announced_lease.get(task.task_id, 0) >= RENEW_BROADCAST_SECONDS:
                    task.updated_at = now
                    self._announced_lease[task.task_id] = task.lease_until
                    announce.append(task)
        return announce

    def sweep(self, now: float) -> list[Task]:
        """Claims whose lease lapsed go back to open; returns them."""
        expired = [
            task
            for task in self._tasks.values()
            if task.status == "claimed" and (task.lease_until or 0) <= now
        ]
        for task in expired:
            self._reopen(task, now)
        return expired

    def next_wakeup(self) -> Optional[float]:
        """When the soonest lease lapses (None with no claims)."""
        leases = [t.lease_until for t in self._tasks.values() if t.status == "claimed" and t.lease_until]
        return min(leases) if leases else None

    # --- internals -------------------------------------------------------------

    def _claimed_by(self, user_id: str, task_id: str) -> Task:
        task = self.get(task_id)
        if task.status != "claimed" or task.claimer != user_id:
            raise TaskError("not_claimer")
        return task

    def _reopen(self, task: Task, now: float) -> None:
        task.status = "open"
        task.claimer = None
        task.claimed_at = None
        task.lease_until = None
        task.updated_at = now
        self._lease_seconds.pop(task.task_id, None)
        self._announced_lease.pop(task.task_id, None)

    def _close(self, task: Task, kind: str, user_id: str, now: float, text: str, e2e: str) -> None:
        if text.strip() or e2e:
            self._add_note(task, TaskNote(user_id=user_id, text=text, timestamp=now, kind=kind, e2e=e2e))  # type: ignore[arg-type]
        task.status = "done" if kind == "done" else "cancelled"
        task.lease_until = None
        task.updated_at = now
        self._lease_seconds.pop(task.task_id, None)
        self._announced_lease.pop(task.task_id, None)
        self._prune_closed()

    def _add_note(self, task: Task, note: TaskNote) -> None:
        task.notes.append(note)
        if len(task.notes) > self.max_notes:
            del task.notes[: len(task.notes) - self.max_notes]

    def _prune_closed(self) -> None:
        closed = sorted(
            (t for t in self._tasks.values() if t.status in _CLOSED), key=lambda t: t.updated_at
        )
        for task in closed[: max(0, len(closed) - self.max_closed)]:
            del self._tasks[task.task_id]


def _clamp(lease_seconds: float) -> float:
    return min(MAX_LEASE_SECONDS, max(MIN_LEASE_SECONDS, float(lease_seconds)))
