/**
 * Room replay: the relay's opt-in history (server/history.py) folded back
 * into what the board looked like at any moment.
 *
 * A room whose .synergo.toml says `history_hours = 4` keeps up to that many
 * hours of feed events on the relay (in memory only). The replay view sends
 * `history_request`, collects the `history_chunk` frames (decoded through the
 * same E2EDecoder as live frames, so an encrypted room replays readable for
 * whoever holds the key), and folds the events up to the scrubber's time T
 * into lanes, decisions and conflicts.
 *
 * Pure apart from the small chunk bus at the bottom, so the fold is unit
 * tested with `node --test` (tests/unit/replay.test.ts); erasable TypeScript
 * with type-only imports.
 */
import type {
  AgentKind,
  ConflictAlert,
  EditDecision,
  FeedEvent,
  FileChange,
  HistoryChunk,
  PlanUpdate,
  RoomSettingsUpdate,
} from './types';

/** Playback speeds offered by the replay view. */
export const SPEEDS = [1, 10, 60] as const;
export type Speed = (typeof SPEEDS)[number];

/** Decisions shown per lane (matches App.tsx MAX_LANE_DECISIONS). */
export const LANE_DECISIONS = 3;
/** Recent decisions listed under the lanes. */
export const RECENT_DECISIONS = 50;
/** A conflict alert stays "active" this long (App.tsx CONFLICT_TTL_SECONDS). */
export const CONFLICT_TTL_SECONDS = 120;

export interface ReplayLane {
  userId: string;
  agent: AgentKind;
  /** False after user_left (and until the next user_joined). */
  online: boolean;
  caption: string | null;
  prompt: string | null;
  intent: string | null;
  changes: FileChange[];
  branch: string;
  /** Latest unexpired plan at T, or null. */
  plan: PlanUpdate | null;
  /** Newest first, one per file, at most LANE_DECISIONS. */
  decisions: EditDecision[];
  /** Time of this lane's latest event at or before T. */
  lastAt: number | null;
}

export interface ReplayState {
  at: number;
  /** Events at or before `at`. */
  applied: number;
  lanes: Record<string, ReplayLane>;
  /** Newest first. */
  decisions: EditDecision[];
  /** Actionable conflict alerts still inside CONFLICT_TTL_SECONDS at T. */
  conflicts: ConflictAlert[];
  settings: RoomSettingsUpdate | null;
  blocks: number;
  warns: number;
}

/** When an event happened (relay clock, seconds), or null if it says nothing. */
export function eventTime(event: FeedEvent): number | null {
  const ts = (event as { timestamp?: unknown }).timestamp;
  return typeof ts === 'number' && Number.isFinite(ts) ? ts : null;
}

/**
 * Events in time order. The relay sends them in broadcast order and stamps
 * the ones without a timestamp; an event that still has none takes the time
 * of the event before it. Stable, so same-time events keep relay order.
 */
export function sortEvents(events: readonly FeedEvent[]): FeedEvent[] {
  let last = Number.NEGATIVE_INFINITY;
  const keyed = events.map((event, index) => {
    const ts = eventTime(event);
    if (ts != null) last = ts;
    return { event, index, ts: ts ?? last };
  });
  keyed.sort((a, b) => a.ts - b.ts || a.index - b.index);
  return keyed.map((k) => k.event);
}

/** First and last event time; null for an empty history. */
export function timeBounds(events: readonly FeedEvent[]): { start: number; end: number } | null {
  let start = Number.POSITIVE_INFINITY;
  let end = Number.NEGATIVE_INFINITY;
  for (const event of events) {
    const ts = eventTime(event);
    if (ts == null) continue;
    if (ts < start) start = ts;
    if (ts > end) end = ts;
  }
  return Number.isFinite(start) ? { start, end } : null;
}

function blankLane(userId: string, agent: AgentKind = 'unknown'): ReplayLane {
  return {
    userId,
    agent,
    online: true,
    caption: null,
    prompt: null,
    intent: null,
    changes: [],
    branch: '',
    plan: null,
    decisions: [],
    lastAt: null,
  };
}

function isActionable(alert: ConflictAlert): boolean {
  return alert.verdict === 'overlapping' || alert.verdict === 'related';
}

/**
 * The board at time `at`: every event with a time at or before it, applied
 * in order. `events` must already be sorted (sortEvents). Lanes appear with
 * a member's first event in the kept window: someone who joined before the
 * oldest kept event shows up when they first act.
 */
export function foldUntil(events: readonly FeedEvent[], at: number): ReplayState {
  const lanes = new Map<string, ReplayLane>();
  const decisions: EditDecision[] = [];
  let conflicts: ConflictAlert[] = [];
  let settings: RoomSettingsUpdate | null = null;
  let applied = 0;
  let blocks = 0;
  let warns = 0;
  let last = Number.NEGATIVE_INFINITY;

  const lane = (userId: string, agent?: AgentKind): ReplayLane => {
    let found = lanes.get(userId);
    if (!found) {
      found = blankLane(userId, agent);
      lanes.set(userId, found);
    } else if (agent && agent !== 'unknown') {
      found.agent = agent;
    }
    return found;
  };

  for (const event of events) {
    const ts = eventTime(event) ?? last;
    if (ts > at) break;
    last = ts;
    applied += 1;
    switch (event.type) {
      case 'user_joined': {
        const l = lane(event.user_id, event.agent);
        l.online = true;
        l.lastAt = ts;
        break;
      }
      case 'user_left': {
        const l = lane(event.user_id);
        l.online = false;
        l.lastAt = ts;
        conflicts = conflicts.filter((c) => !c.users.includes(event.user_id));
        break;
      }
      case 'activity_update': {
        const l = lane(event.user_id, event.agent);
        if (event.changes.length > 0) l.changes = event.changes;
        l.prompt = event.prompt;
        l.caption = event.caption ?? l.caption;
        l.branch = event.branch ?? l.branch;
        l.lastAt = ts;
        break;
      }
      case 'intent_update': {
        const l = lane(event.user_id, event.agent);
        l.intent = event.prompt;
        l.lastAt = ts;
        break;
      }
      case 'plan_update': {
        const l = lane(event.user_id, event.agent);
        l.plan = event;
        l.lastAt = ts;
        break;
      }
      case 'edit_decision': {
        const l = lane(event.user_id, event.agent);
        l.decisions = [event, ...l.decisions.filter((d) => d.file_path !== event.file_path)].slice(
          0,
          LANE_DECISIONS,
        );
        l.lastAt = ts;
        decisions.unshift(event);
        if (decisions.length > RECENT_DECISIONS) decisions.pop();
        if (event.decision === 'block') blocks += 1;
        else warns += 1;
        break;
      }
      case 'conflict_alert': {
        conflicts = conflicts.filter((c) => c.file_path !== event.file_path);
        if (isActionable(event)) conflicts.push(event);
        break;
      }
      case 'room_settings':
        settings = event;
        break;
      default:
        break;
    }
  }

  const out: Record<string, ReplayLane> = {};
  for (const [id, l] of lanes) {
    out[id] = { ...l, plan: l.plan && l.plan.expires_at > at ? l.plan : null };
  }
  return {
    at,
    applied,
    lanes: out,
    decisions,
    conflicts: conflicts
      .filter((c) => at - c.timestamp <= CONFLICT_TTL_SECONDS)
      .sort((a, b) => b.timestamp - a.timestamp),
    settings,
    blocks,
    warns,
  };
}

/** The scrubber position after `elapsedMs` of playback at `speed`, capped at `end`. */
export function advance(at: number, elapsedMs: number, speed: number, end: number): number {
  return Math.min(end, at + (Math.max(0, elapsedMs) / 1000) * speed);
}

/** "14:03:07" in the viewer's local time. */
export function clockLabel(seconds: number): string {
  const d = new Date(seconds * 1000);
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

/** "1h 05m", "4m 10s", "12s". */
export function spanLabel(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  if (s >= 3600) return `${Math.floor(s / 3600)}h ${String(Math.floor((s % 3600) / 60)).padStart(2, '0')}m`;
  if (s >= 60) return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, '0')}s`;
  return `${s}s`;
}

// --- history chunks, from App's socket to the replay view -------------------------------

export interface DecodedChunk {
  events: FeedEvent[];
  done: boolean;
}

type ChunkListener = (chunk: DecodedChunk) => void;
const listeners = new Set<ChunkListener>();
// Decryption is async; one chain keeps chunks delivered in arrival order.
let chain: Promise<void> = Promise.resolve();

/** App.tsx: hand every history_chunk here with its E2EDecoder's decode. */
export function publishHistoryChunk(
  chunk: HistoryChunk,
  decode: (event: FeedEvent) => Promise<FeedEvent>,
): Promise<void> {
  chain = chain
    .then(async () => {
      const events: FeedEvent[] = [];
      for (const event of chunk.events ?? []) {
        try {
          events.push(await decode(event));
        } catch {
          events.push(event);
        }
      }
      for (const listener of listeners) listener({ events, done: chunk.done });
    })
    .catch((err: unknown) => console.warn('replay: could not read a history chunk', err));
  return chain;
}

/** The replay view's subscription; returns the unsubscribe. */
export function onHistoryChunk(listener: ChunkListener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}
