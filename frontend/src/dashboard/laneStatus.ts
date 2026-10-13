/**
 * The lane status pill. Precedence, first match wins:
 *
 *   offline  the person's daemon left the room (user_left)
 *   blocked  the newest warn/block about their own edit was a block, less
 *            than BLOCKED_FOR_SECONDS ago
 *   editing  activity (edits, prompts or intents) within IDLE_AFTER_SECONDS
 *   idle     nothing for longer than that, or nothing yet
 *
 * Times are relay timestamps (seconds); "now" is the browser clock, as for
 * the rest of the dashboard's relative times.
 */
import type { EditDecision } from '../types';

export type LaneStatus = 'editing' | 'idle' | 'blocked' | 'offline';

export const IDLE_AFTER_SECONDS = 120;
export const BLOCKED_FOR_SECONDS = 30;

export interface LaneStatusInput {
  offline: boolean;
  /** Latest activity timestamp, seconds; null when none seen. */
  lastActivity: number | null;
  /** Newest decision about this person's own edit. */
  lastDecision: Pick<EditDecision, 'decision' | 'timestamp'> | null;
  nowSeconds: number;
}

export function laneStatus({
  offline,
  lastActivity,
  lastDecision,
  nowSeconds,
}: LaneStatusInput): LaneStatus {
  if (offline) return 'offline';
  if (
    lastDecision?.decision === 'block' &&
    nowSeconds - lastDecision.timestamp < BLOCKED_FOR_SECONDS
  ) {
    return 'blocked';
  }
  if (lastActivity != null && nowSeconds - lastActivity <= IDLE_AFTER_SECONDS) {
    return 'editing';
  }
  return 'idle';
}

export const STATUS_LABEL: Record<LaneStatus, string> = {
  editing: 'Editing',
  idle: 'Idle',
  blocked: 'Blocked',
  offline: 'Offline',
};

export const STATUS_HINT: Record<LaneStatus, string> = {
  editing: 'Active in the last 2 minutes',
  idle: 'No activity for over 2 minutes',
  blocked: 'Synergo blocked an edit in the last 30 seconds',
  offline: 'The agent left the room',
};
