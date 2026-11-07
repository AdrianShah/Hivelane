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
  PlanUpdate,
} from '../types';

/** Decisions kept per lane: newest first, one per file. */
export const MAX_LANE_DECISIONS = 3;

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
}

export type LaneEvent = ActivityUpdate | IntentUpdate | PlanUpdate | EditDecision;

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
