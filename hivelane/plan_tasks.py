"""An agent's own plan, mirrored as room tasks.

When a coding agent writes a step plan (Claude Code's TodoWrite or its
TaskCreate / TaskUpdate tools, Codex's update_plan), its adapter sends a
`plan` event carrying the steps. The daemon hands them to PlanTasks, which
keeps one room task per step, claimed by the person (the repo connection's
main name) and marked source "agent_plan" with the agent's lane:

    new step             -> task created and claimed at once
    pending              -> claimed, not active (reserves nothing)
    in_progress          -> active: it reserves its files
    completed            -> done
    removed from a list  -> cancelled

While a step is in progress, every file the agent edits is attached to its
task, so the reservation grows with the work. A plan is keyed by the agent
session (agent, session id, subagent id): a re-sent list updates the tasks
it made instead of adding new ones. Steps are matched by the agent's own id
when it has one, then by their text, then by their place in the list (a
step reworded in place keeps its task).

Nothing here runs on the hook's path: submit() and note_edit() only record
what the agent wants and wake a worker task, which talks to the relay
(RoomConnection.task_request, sealed in e2e rooms like every task request)
at its own pace. A plan lives as long as the daemon does; a daemon restart
forgets which tasks it made, and a re-sent list then makes new ones.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol

log = logging.getLogger("hivelane.plan_tasks")

# Plans remembered at once (the oldest is forgotten, its tasks left to lapse).
MAX_PLANS = 64
# Steps per plan; the relay holds 200 open tasks per room in all.
MAX_STEPS = 30
# Files attached to one step (the relay keeps 50 per task).
MAX_STEP_FILES = 50
# A step that is not the active one is held this long between updates (the
# relay's maximum), so a long plan's later steps do not lapse to open...
IDLE_LEASE_SECONDS = 8 * 3600.0
# ...while the active step, which reserves files, lapses like any claim
# unless the agent keeps working (its edits renew it on the relay).
ACTIVE_LEASE_SECONDS = 1800.0

OPEN = ("pending", "in_progress")
PlanKey = tuple[str, str, str]


class TaskSender(Protocol):
    name: str

    async def task_request(self, op: str, repo_id: str, **fields: Any) -> dict[str, Any]: ...


@dataclass(eq=False)
class Step:
    title: str
    status: str
    agent_id: str = ""
    files: list[str] = field(default_factory=list)
    # What the relay has (task_id None: not created yet).
    task_id: Optional[str] = None
    synced: Optional[tuple[str, tuple[str, ...], bool]] = None
    closed: bool = False
    # The relay refused to create it (room list full): not retried until
    # the step changes.
    refused: bool = False


@dataclass(eq=False)
class Plan:
    conn: TaskSender
    repo_id: str
    lane: str
    steps: list[Step] = field(default_factory=list)
    dirty: bool = False
    worker: Optional[asyncio.Task[None]] = None


def _merge_full(plan: Plan, incoming: list[dict[str, str]]) -> None:
    """A whole list: match each step to one we have, the rest are new, and
    the ones it no longer lists are removed."""
    live = [s for s in plan.steps if s.status != "removed"]
    gone = [s for s in plan.steps if s.status == "removed" and not s.closed and s.task_id]
    unmatched = list(live)
    pairs: list[tuple[dict[str, str], Optional[Step]]] = []
    for item in incoming:
        found = None
        if item.get("id"):
            found = next((s for s in unmatched if s.agent_id == item["id"]), None)
        if found is None:
            found = next((s for s in unmatched if s.title == item["content"]), None)
        if found is not None:
            unmatched.remove(found)
        pairs.append((item, found))
    # Reworded in place: the step at the same position keeps its task.
    for index, (item, found) in enumerate(pairs):
        if found is None and index < len(live) and live[index] in unmatched:
            unmatched.remove(live[index])
            pairs[index] = (item, live[index])
    steps: list[Step] = []
    for item, found in pairs:
        status = item.get("status") or "pending"
        if found is None:
            steps.append(Step(title=item["content"], status=status, agent_id=item.get("id", "")))
        else:
            _change(found, item["content"], status)
            found.agent_id = item.get("id") or found.agent_id
            steps.append(found)
    for step in unmatched:
        step.status = "removed"
    plan.steps = steps + unmatched + gone


def _merge_partial(plan: Plan, incoming: list[dict[str, str]]) -> None:
    """Single steps by the agent's id: a new one is appended, a known one
    changes its text and/or status ("" status: as it was)."""
    for item in incoming:
        ident = item.get("id", "")
        found = next((s for s in plan.steps if ident and s.agent_id == ident), None)
        if found is None:
            if item["content"] and item.get("status") != "removed":
                plan.steps.append(Step(title=item["content"], status=item.get("status") or "pending", agent_id=ident))
            continue
        _change(found, item["content"] or found.title, item.get("status") or found.status)


def _change(step: Step, title: str, status: str) -> None:
    if (title, status) != (step.title, step.status):
        step.refused = False
    step.title = title
    if step.closed and status in OPEN:
        # Finished, then back on the list: a closed task cannot reopen, so
        # the step gets a new one.
        step.task_id, step.synced, step.closed, step.files = None, None, False, []
    step.status = status


class PlanTasks:
    def __init__(self) -> None:
        self._plans: OrderedDict[tuple[int, PlanKey], Plan] = OrderedDict()

    # --- what the hooks report (never awaits) ----------------------------------

    def submit(
        self,
        conn: TaskSender,
        key: PlanKey,
        repo_id: str,
        lane: str,
        steps: list[dict[str, str]],
        partial: bool = False,
    ) -> None:
        """The agent wrote (part of) its plan."""
        index = (id(conn), key)
        plan = self._plans.get(index)
        if plan is None:
            plan = Plan(conn=conn, repo_id=repo_id, lane=lane)
            self._plans[index] = plan
            while len(self._plans) > MAX_PLANS:
                self._plans.popitem(last=False)
        else:
            self._plans.move_to_end(index)
        plan.lane = lane or plan.lane
        clean = [s for s in steps if isinstance(s, dict) and isinstance(s.get("content", ""), str)]
        (_merge_partial if partial else _merge_full)(plan, clean)
        # Steps beyond the cap are never created (the agent's list is still
        # its own); removed ones stay until their task is cancelled.
        kept: list[Step] = []
        count = 0
        for step in plan.steps:
            if step.status == "removed":
                if step.task_id and not step.closed:
                    kept.append(step)
                continue
            count += 1
            if count <= MAX_STEPS:
                kept.append(step)
        plan.steps = kept
        self._wake(plan)

    def note_edit(self, conn: TaskSender, key: PlanKey, rel: str) -> None:
        """The agent edited rel: attach it to the step it is on, if any
        (a subagent's edit counts for its parent's step)."""
        plan = self._plans.get((id(conn), key))
        if plan is None or not any(s.status == "in_progress" for s in plan.steps):
            plan = self._plans.get((id(conn), (key[0], key[1], "")))
        if plan is None:
            return
        step = next((s for s in plan.steps if s.status == "in_progress"), None)
        if step is None or rel in step.files or len(step.files) >= MAX_STEP_FILES:
            return
        step.files.append(rel)
        self._wake(plan)

    # --- for tests and status ----------------------------------------------------

    def plan(self, conn: TaskSender, key: PlanKey) -> Optional[Plan]:
        return self._plans.get((id(conn), key))

    async def idle(self) -> None:
        """Wait until every worker has caught up."""
        while True:
            running = [p.worker for p in self._plans.values() if p.worker is not None and not p.worker.done()]
            if not running:
                return
            await asyncio.gather(*running, return_exceptions=True)

    # --- the worker ----------------------------------------------------------------

    def _wake(self, plan: Plan) -> None:
        plan.dirty = True
        if plan.worker is None or plan.worker.done():
            try:
                plan.worker = asyncio.get_running_loop().create_task(self._run(plan))
            except RuntimeError:  # no loop (a sync caller in a test): nothing to send on
                plan.worker = None

    async def _run(self, plan: Plan) -> None:
        while plan.dirty:
            plan.dirty = False
            for step in list(plan.steps):
                try:
                    await self._sync(plan, step)
                except Exception:  # noqa: BLE001 - one bad step must not stop the plan
                    log.exception("plan step sync failed")
            plan.steps = [s for s in plan.steps if not (s.status == "removed" and (s.closed or not s.task_id))]

    async def _request(self, plan: Plan, op: str, **fields: Any) -> dict[str, Any]:
        return await plan.conn.task_request(op, plan.repo_id, **fields)

    async def _sync(self, plan: Plan, step: Step) -> None:
        active = step.status == "in_progress"
        lease = ACTIVE_LEASE_SECONDS if active else IDLE_LEASE_SECONDS
        state = (step.title, tuple(step.files), active)
        if step.task_id is None:
            if step.status == "removed" or step.refused:
                return
            reply = await self._request(
                plan, "create", title=step.title, files=list(step.files), claim=True,
                source="agent_plan", lane=plan.lane, active=active, lease_seconds=lease,
            )
            if not reply.get("ok"):
                step.refused = reply.get("reason") in ("limit", "invalid")
                return
            step.task_id = str((reply.get("task") or {}).get("task_id") or "") or None
            step.synced = state
            if step.task_id and step.status == "completed":
                await self._finish(plan, step)
            return
        if step.closed:
            return
        if step.status == "removed":
            reply = await self._request(plan, "cancel", task_id=step.task_id)
            step.closed = reply.get("ok") or reply.get("reason") in ("closed", "not_found", "not_owner")
            return
        if step.status == "completed":
            await self._finish(plan, step)
            return
        if step.synced == state:
            return
        if active and (step.synced is None or not step.synced[2]):
            # Becoming the active step: take the claim back if its lease lapsed.
            reply = await self._request(plan, "claim", task_id=step.task_id, lease_seconds=lease)
            if reply.get("reason") == "not_found":
                step.task_id, step.synced = None, None
                await self._sync(plan, step)
                return
        reply = await self._request(
            plan, "update", task_id=step.task_id, title=step.title, files=list(step.files),
            active=active, lease_seconds=lease,
        )
        if reply.get("ok"):
            step.synced = state
        elif reply.get("reason") == "not_found":
            # The relay forgot it (restarted): make it again.
            step.task_id, step.synced = None, None
            await self._sync(plan, step)
        elif reply.get("reason") in ("closed", "not_owner"):
            step.closed = reply.get("reason") == "closed"
            step.synced = state

    async def _finish(self, plan: Plan, step: Step) -> None:
        reply = await self._request(plan, "done", task_id=step.task_id)
        if reply.get("reason") == "not_claimer":
            # Its lease lapsed: claim it again to close it.
            await self._request(plan, "claim", task_id=step.task_id)
            reply = await self._request(plan, "done", task_id=step.task_id)
        step.closed = bool(reply.get("ok")) or reply.get("reason") in ("closed", "not_found", "claimed", "not_claimer")
