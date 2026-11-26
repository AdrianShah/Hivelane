/**
 * How one relay feed event changes a lane, shared by the live dashboard
 * (App.tsx) and the replay fold (replay.ts) so the two cannot drift: replay
 * should show what people saw live.
 *
 * Pure and erasable TypeScript with type-only imports, so it runs under
 * `node --test` (tests/unit/lane-reducer.test.ts).
 */
import type {
  ActivityUpdate,
  AgentKind,
  ConflictAlert,
  EditDecision,
  FileChange,
  IntentUpdate,
  PendingFile,
  PendingUpdate,
  PlanUpdate,
} from '../types';

/** Decisions kept per lane: newest first, one per file. */
export const MAX_LANE_DECISIONS = 3;
/** Repos whose unpushed work a lane keeps (the relay keeps 8 per member). */
export const MAX_LANE_PENDING_REPOS = 8;

/** The lane fields both views derive from feed events. */
export interface LaneFields {
  agent: AgentKind;
  caption: string | null;
  prompt: string | null;
  intent: string | null;
  changes: FileChange[];
  branch: string;
  plan: PlanUpdate | null;
  decisions: EditDecision[];
  /** Latest unpushed-work report per repo (pending_update); absent = none heard. */
  pending?: PendingUpdate[];
}

export type LaneEvent = ActivityUpdate | IntentUpdate | PlanUpdate | EditDecision | PendingUpdate;

/**
 * Reports after `next`: its repo's earlier report is replaced, or dropped
 * when `next` lists no files (everything was pushed).
 */
export function replacePending(prev: readonly PendingUpdate[] | undefined, next: PendingUpdate): PendingUpdate[] {
  const repo = next.repo_id ?? '';
  const without = (prev ?? []).filter((p) => (p.repo_id ?? '') !== repo);
  if (next.files.length === 0) return without;
  return [...without, next].slice(-MAX_LANE_PENDING_REPOS);
}

/** One lane's unpushed files across its repos, oldest work first, one per path. */
export function pendingFiles(lane: Pick<LaneFields, 'pending'>): PendingFile[] {
  const byPath = new Map<string, PendingFile>();
  for (const report of lane.pending ?? []) {
    for (const file of report.files) {
      const prior = byPath.get(file.path);
      if (!prior || (file.since ?? 0) < (prior.since ?? 0)) byPath.set(file.path, file);
    }
  }
  return [...byPath.values()].sort((a, b) => (a.since ?? 0) - (b.since ?? 0) || a.path.localeCompare(b.path));
}

/** "Unpushed: 3 files" ("200+ files" when a report was cut at its cap). */
export function unpushedText(count: number, truncated: boolean): string {
  return `Unpushed: ${count}${truncated ? '+' : ''} ${count === 1 && !truncated ? 'file' : 'files'}`;
}

/** True when any of the lane's reports was cut at the daemon's or relay's cap. */
export function pendingTruncated(lane: Pick<LaneFields, 'pending'>): boolean {
  return (lane.pending ?? []).some((p) => p.truncated);
}

/** A relay default that means "not known" ("" branch, "unknown" agent) reads as absent. */
export function known<T extends string>(value: T | null | undefined, unknown: string): T | undefined {
  return value && value !== unknown ? value : undefined;
}

/**
 * `prior` with `event` applied; a new object, `prior` is not changed. Fields
 * outside LaneFields (timestamps, online state) are carried over untouched.
 */
export function applyLaneEvent<L extends LaneFields>(prior: L, event: LaneEvent): L {
  const agent = known(event.agent, 'unknown') ?? prior.agent;
  switch (event.type) {
    case 'activity_update':
      return {
        ...prior,
        agent,
        caption: event.caption ?? prior.caption ?? null,
        // Prompt-only updates send changes: [], keep the last diffs so the
        // lane doesn't blank out when a new prompt arrives.
        changes: event.changes.length > 0 ? event.changes : prior.changes,
        prompt: event.prompt,
        // The relay sends branch "" when it does not know it.
        branch: known(event.branch, '') ?? prior.branch,
      };
    case 'intent_update':
      return { ...prior, agent, intent: event.prompt };
    case 'plan_update':
      return { ...prior, agent, plan: event };
    case 'pending_update':
      return { ...prior, agent, pending: replacePending(prior.pending, event) };
    case 'edit_decision':
      return {
        ...prior,
        agent,
        decisions: [
          event,
          ...prior.decisions.filter((d) => d.file_path !== event.file_path),
        ].slice(0, MAX_LANE_DECISIONS),
      };
  }
}

export function isActionableConflict(alert: ConflictAlert): boolean {
  return alert.verdict === 'overlapping' || alert.verdict === 'related';
}

/** One alert per file *and* pair: the relay judges each pair separately. */
export function conflictKey(alert: ConflictAlert): string {
  return `${alert.file_path}\u0000${[...alert.users].sort().join('\u0000')}`;
}

/**
 * Conflicts after `next`: this pair's earlier alert is replaced, or cleared
 * when `next` is not actionable. Returns `prev` when nothing changed. Expiry
 * is the caller's call (live and replay use different clocks).
 */
export function replaceConflict(prev: ConflictAlert[], next: ConflictAlert): ConflictAlert[] {
  const key = conflictKey(next);
  const without = prev.filter((c) => conflictKey(c) !== key);
  if (!isActionableConflict(next)) return without.length === prev.length ? prev : without;
  return [...without, next];
}
