/**
 * "Synergo prevented N collisions this session" — the heuristic.
 *
 * The relay never learns whether a warning changed anything; the dashboard
 * infers it from what the warned agent did next, using only frames it already
 * has (edit_decision and activity_update):
 *
 * 1. Episodes. Every warn/block is about one person's attempted edit of one
 *    file. Retries of the same edit (same person, same file, each within
 *    PREVENTION_WINDOW_SECONDS of the previous decision) are one episode, so
 *    an agent hammering a blocked file counts once, not once per attempt.
 *
 * 2. Outcome. Look at that person's first edit after the episode's last
 *    decision:
 *    - "prevented": it came within PREVENTION_WINDOW_SECONDS (2 min) and was
 *      to a different file — the agent steered away from the collision.
 *    - "edited-anyway": it came within the window and touched the same file
 *      (a warn lets the edit through, or the agent pushed on after a block).
 *      An update that touches several files, the contested one among them,
 *      counts as edited-anyway.
 *    - "pending": no edit yet, and the window is still open.
 *    - "unknown": no edit within the window (the agent stopped, or its edits
 *      fell out of the dashboard's memory). Conservatively not counted.
 *
 * Only "prevented" episodes are counted. The count is a lower bound in spirit:
 * a human who resolved a collision by talking, or an agent that waited and
 * then made a compatible edit, is not credited.
 */
import type { SessionLog, TimelineEntry, Touch } from './sessionLog.ts';

export const PREVENTION_WINDOW_SECONDS = 120;

export type PreventionOutcome = 'prevented' | 'edited-anyway' | 'pending' | 'unknown';

export interface PreventionEpisode {
  user_id: string;
  file_path: string;
  firstAt: number;
  lastAt: number;
  /** Timeline keys of the decisions folded into this episode. */
  keys: string[];
  /** True when any decision in the episode was a block. */
  blocked: boolean;
  outcome: PreventionOutcome;
}

export interface PreventionSummary {
  prevented: number;
  editedAnyway: number;
  pending: number;
  unknown: number;
  episodes: PreventionEpisode[];
  /** Outcome per timeline key (decisions only). */
  outcomes: Record<string, PreventionOutcome>;
}

function touchesByUser(touches: Touch[]): Map<string, Touch[]> {
  const out = new Map<string, Touch[]>();
  for (const touch of touches) {
    const list = out.get(touch.user_id);
    if (list) list.push(touch);
    else out.set(touch.user_id, [touch]);
  }
  return out;
}

/** Index of the first touch strictly after ts (touches ascending). */
function firstAfter(list: Touch[], ts: number): number {
  let lo = 0;
  let hi = list.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (list[mid].timestamp <= ts) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

export function groupEpisodes(
  timeline: TimelineEntry[],
  windowSeconds = PREVENTION_WINDOW_SECONDS,
): PreventionEpisode[] {
  const decisions = timeline
    .filter((entry) => entry.decision != null)
    .sort((a, b) => a.timestamp - b.timestamp);
  const open = new Map<string, PreventionEpisode>();
  const episodes: PreventionEpisode[] = [];
  for (const entry of decisions) {
    const user = entry.users[0];
    const id = `${user}\u0000${entry.file_path}`;
    const current = open.get(id);
    if (current && entry.timestamp - current.lastAt <= windowSeconds) {
      current.lastAt = entry.timestamp;
      current.keys.push(entry.key);
      current.blocked ||= entry.kind === 'block';
      continue;
    }
    const episode: PreventionEpisode = {
      user_id: user,
      file_path: entry.file_path,
      firstAt: entry.timestamp,
      lastAt: entry.timestamp,
      keys: [entry.key],
      blocked: entry.kind === 'block',
      outcome: 'unknown',
    };
    open.set(id, episode);
    episodes.push(episode);
  }
  return episodes;
}

export function episodeOutcome(
  episode: Pick<PreventionEpisode, 'file_path' | 'lastAt'>,
  userTouches: Touch[],
  nowSeconds: number,
  windowSeconds = PREVENTION_WINDOW_SECONDS,
): PreventionOutcome {
  const start = firstAfter(userTouches, episode.lastAt);
  const next = userTouches[start];
  if (next && next.timestamp - episode.lastAt <= windowSeconds) {
    // Every file in that same update (one activity_update shares a timestamp).
    for (let i = start; i < userTouches.length; i += 1) {
      const touch = userTouches[i];
      if (touch.timestamp !== next.timestamp) break;
      if (touch.file_path === episode.file_path) return 'edited-anyway';
    }
    return 'prevented';
  }
  return nowSeconds - episode.lastAt < windowSeconds ? 'pending' : 'unknown';
}

export function summarizePrevention(
  log: Pick<SessionLog, 'timeline' | 'touches'>,
  nowSeconds: number,
  windowSeconds = PREVENTION_WINDOW_SECONDS,
): PreventionSummary {
  const byUser = touchesByUser(log.touches);
  const episodes = groupEpisodes(log.timeline, windowSeconds);
  const summary: PreventionSummary = {
    prevented: 0,
    editedAnyway: 0,
    pending: 0,
    unknown: 0,
    episodes,
    outcomes: {},
  };
  for (const episode of episodes) {
    episode.outcome = episodeOutcome(
      episode,
      byUser.get(episode.user_id) ?? [],
      nowSeconds,
      windowSeconds,
    );
    if (episode.outcome === 'prevented') summary.prevented += 1;
    else if (episode.outcome === 'edited-anyway') summary.editedAnyway += 1;
    else if (episode.outcome === 'pending') summary.pending += 1;
    else summary.unknown += 1;
    for (const key of episode.keys) summary.outcomes[key] = episode.outcome;
  }
  return summary;
}

export const OUTCOME_LABEL: Record<PreventionOutcome, string> = {
  prevented: 'prevented',
  'edited-anyway': 'edited anyway',
  pending: 'watching',
  unknown: 'no follow-up',
};
