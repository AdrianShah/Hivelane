/**
 * What the v3 bee and status dot show for one lane. Extends laneStatus.ts
 * (offline / blocked / editing / idle) with the two states v3 adds:
 *
 *   waiting   an edit of theirs is held and they are in line for the file
 *             (queue_position 2 or more, or an owner put them behind
 *             someone): blue
 *   conflict  in a live overlap alert: amber
 *
 * Precedence, first match wins: offline > blocked > waiting > conflict >
 * editing > idle. A hold an owner released (hold_released, or a go-first
 * naming this lane) ends blocked/waiting at once, without waiting for the
 * 30 s block display to lapse.
 */
import type { AvatarState } from '../brand/mascot';
import { BLOCKED_FOR_SECONDS, type LaneStatus } from './laneStatus.ts';

export type LaneState = 'editing' | 'idle' | 'conflict' | 'blocked' | 'waiting' | 'offline';

export interface LaneStateInput {
  /** laneStatus() for the lane. */
  status: LaneStatus;
  /** Newest block/warn about this lane's own edit. */
  lastDecision: { decision: 'warn' | 'block'; timestamp: number; queue_position?: number } | null;
  /** When an owner last let this lane through (seconds), if ever. */
  releasedAt: number | null;
  /** When an owner last put this lane behind someone (seconds), if ever. */
  queuedAt: number | null;
  inConflict: boolean;
  nowSeconds: number;
}

/** How long "waiting" shows after the latest queue signal with nothing newer. */
export const WAITING_FOR_SECONDS = 60;

export function laneState({
  status,
  lastDecision,
  releasedAt,
  queuedAt,
  inConflict,
  nowSeconds,
}: LaneStateInput): LaneState {
  if (status === 'offline') return 'offline';
  const decisionAt = lastDecision?.timestamp ?? -Infinity;
  const released = releasedAt != null && releasedAt >= decisionAt;
  const queued =
    queuedAt != null && nowSeconds - queuedAt < WAITING_FOR_SECONDS && (releasedAt == null || queuedAt > releasedAt);
  if (!released && lastDecision?.decision === 'block' && nowSeconds - decisionAt < WAITING_FOR_SECONDS) {
    if ((lastDecision.queue_position ?? 0) >= 2 || queued) return 'waiting';
    if (status === 'blocked' || nowSeconds - decisionAt < BLOCKED_FOR_SECONDS) return 'blocked';
  }
  if (queued) return 'waiting';
  if (inConflict) return 'conflict';
  // A released hold leaves laneStatus at "blocked" for a few seconds more:
  // they were mid-edit, so they read as editing.
  return status === 'editing' || status === 'blocked' ? 'editing' : 'idle';
}

/** The bee avatar for a lane state (they share names). */
export function avatarFor(state: LaneState): AvatarState {
  return state;
}

export const LANE_STATE_LABEL: Record<LaneState, string> = {
  editing: 'Editing',
  idle: 'Idle',
  conflict: 'Overlap',
  blocked: 'Held',
  waiting: 'Waiting',
  offline: 'Offline',
};
