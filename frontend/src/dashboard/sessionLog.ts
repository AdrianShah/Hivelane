/**
 * The dashboard's memory of this session, built only from relay frames the
 * spectator socket already receives (activity, intents, plans, decisions,
 * conflicts). App.tsx's lane state keeps "what is true now"; this log keeps
 * "what happened", which the heat map, collision timeline, lane history and
 * prevented-collision counter are views over. Nothing here talks to the relay.
 *
 * Pure and immutable (a useReducer reducer): every frame returns either the
 * same object (nothing changed) or a new one sharing untouched parts. Frames
 * may arrive twice (catch-up after a reconnect, history replays) and out of
 * order, so inserts are de-duplicated and kept sorted by the relay's
 * timestamps (server time, authoritative — see server/main.py).
 */
import type {
  AgentKind,
  ConflictAlert,
  EditDecision,
  FeedEvent,
  PlanUpdate,
} from '../types';
import { normPath } from './paths.ts';

/** File touches kept for the heat map and lane history (oldest dropped first). */
export const MAX_TOUCHES = 3000;
/** Collision timeline entries kept (oldest dropped first). */
export const MAX_TIMELINE = 500;
/**
 * Repeats of one conflict (same file, same people) within this many seconds
 * of its last sighting fold into one entry. Matches the relay's
 * CONFLICT_WINDOW_SECONDS (server/room.py) and App.tsx's CONFLICT_TTL_SECONDS.
 */
export const CONFLICT_MERGE_SECONDS = 120;

/** One file in one activity_update: someone's edit, as the relay relayed it. */
export interface Touch {
  user_id: string;
  agent: AgentKind;
  /** Normalised (forward slashes). */
  file_path: string;
  /** Relay timestamp, seconds. */
  timestamp: number;
  symbols: string[];
  diff: string;
  /** The update's AI caption, if any. */
  caption: string | null;
  /** The person's declared intent when this edit arrived. */
  intent: string | null;
}

export type TimelineKind = 'block' | 'warn' | 'conflict';

export interface TimelineEntry {
  /** decision_id, or a synthetic key for conflicts and id-less decisions. */
  key: string;
  kind: TimelineKind;
  /** First sighting, seconds (relay clock). */
  timestamp: number;
  /** Last sighting; differs from timestamp only for repeated conflicts. */
  lastSeen: number;
  file_path: string;
  /** Decisions: the requester first, then the people it collided with. */
  users: string[];
  decision: EditDecision | null;
  conflict: ConflictAlert | null;
  /** Each involved person's intent when the entry first arrived. */
  intents: Record<string, string | null>;
  /** How many alerts folded into this entry (conflicts only; 1 otherwise). */
  repeats: number;
}

export interface IntentRecord {
  prompt: string;
  timestamp: number;
}

export interface SessionLog {
  /** Oldest first, sorted by timestamp. */
  touches: Touch[];
  /** Newest first, sorted by timestamp. */
  timeline: TimelineEntry[];
  /** Latest intent per person. */
  intents: Record<string, IntentRecord>;
  /** Everyone seen this session, with their latest known agent kind. */
  agents: Record<string, AgentKind>;
  /** Latest plan per person (callers check expires_at). */
  plans: Record<string, PlanUpdate>;
  /** Latest activity/intent timestamp per person, seconds. */
  lastActivity: Record<string, number>;
  /** Earliest event timestamp seen, seconds; null before any. */
  startedAt: number | null;
}

export type SessionLogAction = FeedEvent | { type: 'reset' };

export function createSessionLog(): SessionLog {
  return {
    touches: [],
    timeline: [],
    intents: {},
    agents: {},
    plans: {},
    lastActivity: {},
    startedAt: null,
  };
}

function isActionable(alert: ConflictAlert): boolean {
  return alert.verdict === 'overlapping' || alert.verdict === 'related';
}

function withAgent(log: SessionLog, userId: string, agent?: AgentKind): SessionLog {
  const prior = log.agents[userId];
  const known = agent && agent !== 'unknown' ? agent : undefined;
  if (prior !== undefined && (known === undefined || known === prior)) return log;
  return { ...log, agents: { ...log.agents, [userId]: known ?? prior ?? 'unknown' } };
}

function withActivityAt(log: SessionLog, userId: string, timestamp: number): SessionLog {
  const prior = log.lastActivity[userId];
  const startedAt =
    log.startedAt == null ? timestamp : Math.min(log.startedAt, timestamp);
  if (prior != null && prior >= timestamp && startedAt === log.startedAt) return log;
  return {
    ...log,
    startedAt,
    lastActivity:
      prior != null && prior >= timestamp
        ? log.lastActivity
        : { ...log.lastActivity, [userId]: timestamp },
  };
}

function withStart(log: SessionLog, timestamp: number): SessionLog {
  if (log.startedAt != null && log.startedAt <= timestamp) return log;
  return { ...log, startedAt: timestamp };
}

/** First index whose timestamp is greater than ts (ascending array). */
function upperBound(items: { timestamp: number }[], ts: number): number {
  let lo = 0;
  let hi = items.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (items[mid].timestamp <= ts) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

/** Insert into an ascending, capped array (the oldest fall off the front). */
function insertAscending<T extends { timestamp: number }>(
  items: T[],
  added: T[],
  ts: number,
  cap: number,
): T[] {
  const last = items[items.length - 1];
  let next: T[];
  if (!last || last.timestamp <= ts) {
    next = items.concat(added);
  } else {
    const at = upperBound(items, ts);
    next = [...items.slice(0, at), ...added, ...items.slice(at)];
  }
  return next.length > cap ? next.slice(next.length - cap) : next;
}

/** Insert into a descending (newest first), capped array. */
function insertDescending(
  items: TimelineEntry[],
  entry: TimelineEntry,
  cap: number,
): TimelineEntry[] {
  let at = 0;
  while (at < items.length && items[at].timestamp > entry.timestamp) at += 1;
  const next = [...items.slice(0, at), entry, ...items.slice(at)];
  return next.length > cap ? next.slice(0, cap) : next;
}

function intentSnapshot(log: SessionLog, users: string[]): Record<string, string | null> {
  const out: Record<string, string | null> = {};
  for (const user of users) out[user] = log.intents[user]?.prompt ?? null;
  return out;
}

function sameUsers(a: string[], b: string[]): boolean {
  if (a.length !== b.length) return false;
  const sa = [...a].sort();
  const sb = [...b].sort();
  return sa.every((value, i) => value === sb[i]);
}

function recordDecision(log: SessionLog, d: EditDecision): SessionLog {
  const file = normPath(d.file_path);
  const key = d.decision_id || `decision|${d.user_id}|${file}|${d.timestamp}`;
  let next = withStart(withAgent(log, d.user_id, d.agent), d.timestamp);
  if (next.timeline.some((entry) => entry.key === key)) return next;
  const users = [d.user_id, ...d.others.filter((o) => o !== d.user_id)];
  for (const other of users.slice(1)) next = withAgent(next, other);
  const entry: TimelineEntry = {
    key,
    kind: d.decision,
    timestamp: d.timestamp,
    lastSeen: d.timestamp,
    file_path: file,
    users,
    decision: d,
    conflict: null,
    intents: intentSnapshot(next, users),
    repeats: 1,
  };
  return { ...next, timeline: insertDescending(next.timeline, entry, MAX_TIMELINE) };
}

function recordConflict(log: SessionLog, c: ConflictAlert): SessionLog {
  if (!isActionable(c)) return log;
  const file = normPath(c.file_path);
  let next = withStart(log, c.timestamp);
  for (const user of c.users) next = withAgent(next, user);
  const index = next.timeline.findIndex(
    (entry) =>
      entry.kind === 'conflict' &&
      entry.file_path === file &&
      sameUsers(entry.users, c.users) &&
      c.timestamp >= entry.timestamp - CONFLICT_MERGE_SECONDS &&
      c.timestamp <= entry.lastSeen + CONFLICT_MERGE_SECONDS,
  );
  if (index >= 0) {
    const prior = next.timeline[index];
    // A replay of an alert already folded in changes nothing.
    if (c.timestamp >= prior.timestamp && c.timestamp <= prior.lastSeen) return next;
    const newer = c.timestamp > prior.lastSeen;
    const merged: TimelineEntry = {
      ...prior,
      timestamp: Math.min(prior.timestamp, c.timestamp),
      lastSeen: Math.max(prior.lastSeen, c.timestamp),
      conflict: newer ? c : prior.conflict,
      repeats: prior.repeats + 1,
    };
    const without = next.timeline.filter((_, i) => i !== index);
    return { ...next, timeline: insertDescending(without, merged, MAX_TIMELINE) };
  }
  const entry: TimelineEntry = {
    key: `conflict|${file}|${[...c.users].sort().join(',')}|${c.timestamp}`,
    kind: 'conflict',
    timestamp: c.timestamp,
    lastSeen: c.timestamp,
    file_path: file,
    users: [...c.users],
    decision: null,
    conflict: c,
    intents: intentSnapshot(next, c.users),
    repeats: 1,
  };
  return { ...next, timeline: insertDescending(next.timeline, entry, MAX_TIMELINE) };
}

export function reduceSessionLog(log: SessionLog, action: SessionLogAction): SessionLog {
  switch (action.type) {
    case 'reset':
      return createSessionLog();

    case 'user_joined':
      return withAgent(log, action.user_id, action.agent);

    case 'activity_update': {
      const user = action.user_id;
      let next = withActivityAt(withAgent(log, user, action.agent), user, action.timestamp);
      if (action.changes.length === 0) return next;
      // The relay stamps each update with server time, so the same person at
      // the same instant is a replay (catch-up after reconnect, history).
      if (next.touches.some((t) => t.user_id === user && t.timestamp === action.timestamp)) {
        return next;
      }
      const agent = next.agents[user] ?? 'unknown';
      const intent = next.intents[user]?.prompt ?? null;
      const added: Touch[] = action.changes.map((change) => ({
        user_id: user,
        agent,
        file_path: normPath(change.file_path),
        timestamp: action.timestamp,
        symbols: change.symbols ?? [],
        diff: change.diff,
        caption: action.caption ?? null,
        intent,
      }));
      next = {
        ...next,
        touches: insertAscending(next.touches, added, action.timestamp, MAX_TOUCHES),
      };
      return next;
    }

    case 'intent_update': {
      const user = action.user_id;
      let next = withActivityAt(withAgent(log, user, action.agent), user, action.timestamp);
      const prior = next.intents[user];
      const isNewer = !prior || prior.timestamp < action.timestamp;
      const isRestated =
        prior != null &&
        prior.timestamp === action.timestamp &&
        prior.prompt !== action.prompt;
      if (isNewer || isRestated) {
        next = {
          ...next,
          intents: {
            ...next.intents,
            [user]: { prompt: action.prompt, timestamp: action.timestamp },
          },
        };
      }
      return next;
    }

    case 'plan_update': {
      const user = action.user_id;
      let next = withStart(withAgent(log, user, action.agent), action.timestamp);
      const prior = next.plans[user];
      if (prior !== action && (!prior || prior.timestamp <= action.timestamp)) {
        next = { ...next, plans: { ...next.plans, [user]: action } };
      }
      return next;
    }

    case 'edit_decision':
      return recordDecision(log, action);

    case 'conflict_alert':
      return recordConflict(log, action);

    default:
      return log;
  }
}

// --- selectors --------------------------------------------------------------

/** Everyone seen this session, sorted. */
export function sessionPeople(log: SessionLog): string[] {
  return Object.keys(log.agents).sort();
}

/** A person's most recent edits, newest first. */
export function recentTouches(log: SessionLog, userId: string, limit = 5): Touch[] {
  const out: Touch[] = [];
  for (let i = log.touches.length - 1; i >= 0 && out.length < limit; i -= 1) {
    if (log.touches[i].user_id === userId) out.push(log.touches[i]);
  }
  return out;
}

/** Files a person edited at or after `sinceSeconds` (normalised paths). */
export function filesEditedSince(
  log: SessionLog,
  userId: string,
  sinceSeconds: number,
): Set<string> {
  const out = new Set<string>();
  for (let i = log.touches.length - 1; i >= 0; i -= 1) {
    const touch = log.touches[i];
    if (touch.timestamp < sinceSeconds) break;
    if (touch.user_id === userId) out.add(touch.file_path);
  }
  return out;
}

/**
 * The diff to show for one person in a timeline entry: their latest edit of
 * the file up to `slackSeconds` after the entry. The slack picks up the edit a
 * warned requester went on to make (decisions are pre-edit checks, so the
 * requester's own diff usually lands just after).
 */
export function diffFor(
  log: SessionLog,
  userId: string,
  filePath: string,
  atSeconds: number,
  slackSeconds = CONFLICT_MERGE_SECONDS,
): Touch | null {
  const file = normPath(filePath);
  const limit = atSeconds + slackSeconds;
  for (let i = log.touches.length - 1; i >= 0; i -= 1) {
    const touch = log.touches[i];
    if (touch.timestamp > limit) continue;
    if (touch.user_id === userId && touch.file_path === file) return touch;
  }
  return null;
}

/** The newest warn/block the relay sent about a person's own edit, if any. */
export function lastDecisionFor(log: SessionLog, userId: string): EditDecision | null {
  for (const entry of log.timeline) {
    if (entry.decision && entry.decision.user_id === userId) return entry.decision;
  }
  return null;
}
