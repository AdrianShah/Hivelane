/**
 * What the lane view needs from the session log for one person: their
 * status, latest edits, plan progress and the facts the filters match on.
 */
import type { EditDecision, FileChange, PlanUpdate } from '../types';
import type { LaneFacts } from './filters.ts';
import { laneStatus, type LaneStatus } from './laneStatus.ts';
import {
  filesEditedSince,
  lastDecisionFor,
  recentTouches,
  type SessionLog,
  type Touch,
} from './sessionLog.ts';

/** The parts of App.tsx's lane entry this reads. */
export interface LaneLike {
  agent: string;
  intent: string | null;
  caption: string | null;
  prompt: string | null;
  changes: FileChange[];
  decisions: EditDecision[];
  plan: PlanUpdate | null;
  offlineSince: number | null;
  timestamp: number | null;
}

export interface LaneDerived {
  status: LaneStatus;
  recentEdits: Touch[];
  editedPlanFiles: Set<string>;
  lastActivity: number | null;
  facts: LaneFacts;
}

export const LANE_DIFFS = 5;

export function deriveLane(
  log: SessionLog,
  userId: string,
  lane: LaneLike,
  nowSeconds: number,
): LaneDerived {
  const recentEdits = recentTouches(log, userId, LANE_DIFFS);
  const loggedActivity = log.lastActivity[userId];
  const lastActivity =
    loggedActivity != null && lane.timestamp != null
      ? Math.max(loggedActivity, lane.timestamp)
      : (loggedActivity ?? lane.timestamp);
  const lastDecision = lastDecisionFor(log, userId) ?? lane.decisions[0] ?? null;
  return {
    status: laneStatus({
      offline: lane.offlineSince != null,
      lastActivity,
      lastDecision,
      nowSeconds,
    }),
    recentEdits,
    editedPlanFiles: lane.plan
      ? filesEditedSince(log, userId, lane.plan.timestamp)
      : new Set<string>(),
    lastActivity,
    facts: {
      userId,
      agent: lane.agent,
      files: [
        ...recentEdits.map((t) => t.file_path),
        ...lane.changes.map((c) => c.file_path),
        ...(lane.plan?.files ?? []),
        ...lane.decisions.map((d) => d.file_path),
      ],
      texts: [lane.intent, lane.caption, lane.prompt, lane.plan?.summary],
      entries: log.timeline.filter((e) => e.users.includes(userId)),
    },
  };
}
