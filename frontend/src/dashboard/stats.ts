/**
 * Room statistics for the shareable "prevented collisions" card. Only counts
 * and agent kinds leave this module: no file names, prompts, intents, diffs,
 * people's names or the room code, so the card is safe to post publicly.
 */
import type { FeedbackMap } from '../detection.ts';
import type { PreventionSummary } from './prevented.ts';
import { agentLabel } from './people.ts';
import type { SessionLog } from './sessionLog.ts';

export interface RoomStats {
  prevented: number;
  blocks: number;
  warns: number;
  conflicts: number;
  people: number;
  /** Display labels of the agent kinds seen, most common first. */
  agentKinds: string[];
  edits: number;
  filesTouched: number;
  sessionSeconds: number;
  usefulVotes: number;
  noiseVotes: number;
  /** Edits per time bucket across the session, oldest first. */
  editHistogram: number[];
  /** Warns + blocks + conflicts per bucket, aligned with editHistogram. */
  collisionHistogram: number[];
}

export function histogram(
  timestamps: number[],
  start: number,
  end: number,
  buckets: number,
): number[] {
  const out = new Array<number>(buckets).fill(0);
  const span = Math.max(1, end - start);
  for (const ts of timestamps) {
    if (ts < start || ts > end) continue;
    const index = Math.min(buckets - 1, Math.floor(((ts - start) / span) * buckets));
    out[index] += 1;
  }
  return out;
}

export function roomStats(
  log: SessionLog,
  prevention: Pick<PreventionSummary, 'prevented'>,
  feedback: FeedbackMap,
  nowSeconds: number,
  buckets = 24,
): RoomStats {
  let blocks = 0;
  let warns = 0;
  let conflicts = 0;
  for (const entry of log.timeline) {
    if (entry.kind === 'block') blocks += 1;
    else if (entry.kind === 'warn') warns += 1;
    else conflicts += 1;
  }
  const kindCounts = new Map<string, number>();
  for (const agent of Object.values(log.agents)) {
    if (agent === 'unknown') continue;
    kindCounts.set(agent, (kindCounts.get(agent) ?? 0) + 1);
  }
  const agentKinds = [...kindCounts.entries()]
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .map(([kind]) => agentLabel(kind));
  let usefulVotes = 0;
  let noiseVotes = 0;
  const keys = new Set(log.timeline.map((e) => e.key));
  for (const [id, tally] of Object.entries(feedback)) {
    if (!keys.has(id)) continue;
    usefulVotes += tally.useful_count;
    noiseVotes += tally.noise_count;
  }
  const start = log.startedAt ?? nowSeconds;
  const end = Math.max(nowSeconds, start + 1);
  return {
    prevented: prevention.prevented,
    blocks,
    warns,
    conflicts,
    people: Object.keys(log.agents).length,
    agentKinds,
    edits: log.touches.length,
    filesTouched: new Set(log.touches.map((t) => t.file_path)).size,
    sessionSeconds: Math.max(0, Math.round(end - start)),
    usefulVotes,
    noiseVotes,
    editHistogram: histogram(
      log.touches.map((t) => t.timestamp),
      start,
      end,
      buckets,
    ),
    collisionHistogram: histogram(
      log.timeline.map((e) => e.timestamp),
      start,
      end,
      buckets,
    ),
  };
}

export function formatDuration(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? `${hours} h ${rest} min` : `${hours} h`;
}
