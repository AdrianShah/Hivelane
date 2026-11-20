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

/** The view's columns: ready to claim, waiting on dependencies, claimed, finished. */
export function groupTasks(tasks: Record<string, Task>): TaskGroups {
  const groups: TaskGroups = { ready: [], waiting: [], claimed: [], closed: [] };
  for (const task of Object.values(tasks)) {
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

/** Tasks a member holds, oldest claim first. */
export function claimedBy(tasks: Record<string, Task>, userId: string): Task[] {
  return Object.values(tasks)
    .filter((t) => t.status === 'claimed' && t.claimer === userId)
    .sort((a, b) => (a.claimed_at ?? 0) - (b.claimed_at ?? 0));
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
