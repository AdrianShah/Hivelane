/**
 * The room's task list as the dashboard holds it: task_update frames folded
 * into a record keyed by task_id, plus the derived groupings the Tasks view
 * and the lane cards show. Pure, so it is unit-tested under node --test.
 */
import type { Task, TaskUpdate } from '../types.ts';
import { isSafeKey, withEntry } from '../safeKey.ts';

/** Tasks kept on the dashboard (the relay keeps at most 250 per room). */
export const MAX_TRACKED_TASKS = 300;

/**
 * Fold one task_update. A snapshot older than the one held is ignored: feed
 * events queued before this dashboard joined can arrive after its catch-up.
 */
export function applyTaskEvent(
  prev: Record<string, Task>,
  event: TaskUpdate,
): Record<string, Task> {
  const task = event.task;
  if (!task || typeof task.task_id !== 'string' || !isSafeKey(task.task_id)) return prev;
  const held = prev[task.task_id];
  if (held && held.updated_at > task.updated_at) return prev;
  const next = withEntry(prev, task.task_id, task);
  const keys = Object.keys(next);
  if (keys.length <= MAX_TRACKED_TASKS) return next;
  // Drop the oldest finished tasks first, then the oldest of any kind.
  const closed = keys.filter((k) => next[k].status === 'done' || next[k].status === 'cancelled');
  const drop = new Set((closed.length ? closed : keys).slice(0, keys.length - MAX_TRACKED_TASKS));
  return Object.fromEntries(keys.filter((k) => !drop.has(k)).map((k) => [k, next[k]]));
}

/** Open, and nothing it depends on is still to do (a dependency the
 * dashboard never saw is treated as done, as the relay does once pruned). */
export function isReady(task: Task, tasks: Record<string, Task>): boolean {
  if (task.status !== 'open') return false;
  return task.depends_on.every((id) => {
    const dep = tasks[id];
    return !dep || dep.status === 'done' || dep.status === 'cancelled';
  });
}

export interface TaskGroups {
  ready: Task[];
  waiting: Task[];
  claimed: Task[];
  closed: Task[];
}

function byNumber(a: Task, b: Task): number {
  const n = (t: Task) => Number(t.task_id.replace(/^\D+/, '')) || 0;
  return n(a) - n(b);
}

/** A step of an agent's own plan (kept in step by its daemon), not a team task. */
export function isPlanStep(task: Task): boolean {
  return task.source === 'agent_plan';
}

function isClosed(task: Task): boolean {
  return task.status === 'done' || task.status === 'cancelled';
}

/**
 * The view's columns for the team's own tasks: ready to claim, waiting on
 * dependencies, claimed, finished. Agents' plan steps are left out (see
 * planGroups).
 */
export function groupTasks(tasks: Record<string, Task>): TaskGroups {
  const groups: TaskGroups = { ready: [], waiting: [], claimed: [], closed: [] };
  for (const task of Object.values(tasks)) {
    if (isPlanStep(task)) continue;
    if (task.status === 'claimed') groups.claimed.push(task);
    else if (task.status === 'open') (isReady(task, tasks) ? groups.ready : groups.waiting).push(task);
    else groups.closed.push(task);
  }
  groups.ready.sort(byNumber);
  groups.waiting.sort(byNumber);
  groups.claimed.sort(byNumber);
  groups.closed.sort((a, b) => b.updated_at - a.updated_at);
  return groups;
}

/** Team tasks a member holds, oldest claim first (its agents' plan steps are
 * shown as plan progress instead). */
export function claimedBy(tasks: Record<string, Task>, userId: string): Task[] {
  return Object.values(tasks)
    .filter((t) => t.status === 'claimed' && t.claimer === userId && !isPlanStep(t))
    .sort((a, b) => (a.claimed_at ?? 0) - (b.claimed_at ?? 0));
}

/** One agent's plan as the board shows it. */
export interface PlanGroup {
  /** The agent lane that wrote it ("adrian-claude"). */
  lane: string;
  /** The person its steps are claimed by ("adrian"). */
  person: string;
  /** "adrian's claude" (or the lane itself when it is not person-agent). */
  label: string;
  steps: Task[];
  done: number;
  /** Steps not cancelled (what "3/5 steps" counts). */
  total: number;
  current: Task | null;
  finished: boolean;
  updatedAt: number;
}

function laneOf(task: Task): string {
  return task.lane || task.claimer || task.created_by;
}

/** "adrian's claude" for lane adrian-claude of person adrian. */
export function planLabel(lane: string, person: string): string {
  if (person && lane.startsWith(`${person}-`) && lane.length > person.length + 1) {
    return `${person}'s ${lane.slice(person.length + 1)}`;
  }
  return person && person !== lane ? `${person}'s ${lane}` : lane;
}

/**
 * Agents' plan steps grouped by lane: plans with a step still to do first
 * (most recently changed first), then finished ones. Steps keep their order
 * of creation, which is the plan's order.
 */
export function planGroups(tasks: Record<string, Task>): PlanGroup[] {
  const byLane = new Map<string, Task[]>();
  for (const task of Object.values(tasks)) {
    if (!isPlanStep(task)) continue;
    const lane = laneOf(task);
    const list = byLane.get(lane);
    if (list) list.push(task);
    else byLane.set(lane, [task]);
  }
  const groups: PlanGroup[] = [];
  for (const [lane, steps] of byLane) {
    steps.sort(byNumber);
    const person = steps[0].created_by;
    const counted = steps.filter((t) => t.status !== 'cancelled');
    groups.push({
      lane,
      person,
      label: planLabel(lane, person),
      steps,
      done: counted.filter((t) => t.status === 'done').length,
      total: counted.length,
      current: steps.find((t) => t.active && t.status === 'claimed') ?? null,
      finished: steps.every(isClosed),
      updatedAt: Math.max(...steps.map((t) => t.updated_at)),
    });
  }
  return groups.sort((a, b) => Number(a.finished) - Number(b.finished) || b.updatedAt - a.updatedAt);
}

/** The step a lane's agent is on now, and how far its plan is. */
export function laneProgress(
  tasks: Record<string, Task>,
  laneId: string,
): { current: Task | null; done: number; total: number } | null {
  const group = planGroups(tasks).find((g) => g.lane === laneId && !g.finished);
  return group ? { current: group.current, done: group.done, total: group.total } : null;
}

/** Whether this dashboard created the task (its tag matches ours). */
export function isMine(task: Task, myTag: string | null): boolean {
  return !!myTag && !!task.dashboard_tag && task.dashboard_tag === myTag;
}

/** Comma- or newline-separated paths / ids from a form field, deduplicated. */
export function splitList(text: string): string[] {
  return [...new Set(text.split(/[,\n]/).map((p) => p.trim()).filter(Boolean))];
}

/** Task ids as the relay writes them ("t3", "3" -> "T3"); others dropped. */
export function taskIds(text: string): string[] {
  const out: string[] = [];
  for (const raw of splitList(text)) {
    const id = raw.toUpperCase().replace(/^T?(\d+)$/, 'T$1');
    if (/^T\d+$/.test(id) && !out.includes(id)) out.push(id);
  }
  return out;
}

/** Why the relay refused a dashboard's task request, in words. */
export function taskRefusal(reason: string | undefined): string {
  switch (reason) {
    case 'limit':
      return "The room's task list is full";
    case 'rate_limited':
      return 'Too fast, try again in a moment';
    case 'not_found':
      return 'A task it depends on does not exist';
    case 'not_owner':
      return 'Only the dashboard that added it can cancel it';
    case 'closed':
      return 'That task is already finished';
    case 'invalid':
      return 'The relay refused it (is your name or the title empty?)';
    case 'not_allowed':
      return 'Dashboards can only add and cancel tasks';
    default:
      return reason ? `Refused: ${reason}` : 'Refused';
  }
}

/** "18 min left", "2 h left", or "lease lapsing" once it is due. */
export function leaseLabel(task: Task, nowSeconds: number): string {
  if (!task.lease_until) return '';
  const left = task.lease_until - nowSeconds;
  if (left <= 0) return 'lease lapsing';
  const minutes = Math.floor(left / 60);
  if (minutes < 1) return 'under a minute left';
  if (minutes < 120) return `${minutes} min left`;
  return `${Math.floor(minutes / 60)} h left`;
}

/** The result note of a done task, if it left one. */
export function taskResult(task: Task): string {
  for (let i = task.notes.length - 1; i >= 0; i -= 1) {
    if (task.notes[i].kind === 'done' && task.notes[i].text) return task.notes[i].text;
  }
  return '';
}
