/**
 * The shared lane reducer (src/dashboard/laneReducer.ts) and replay parity:
 * the replay fold must build the same lanes the live dashboard shows.
 * Run: npm run test:unit.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  MAX_LANE_DECISIONS,
  applyLaneEvent,
  replaceConflict,
  type LaneFields,
} from '../../src/dashboard/laneReducer.ts';
import { foldUntil } from '../../src/replay.ts';
import type { ConflictAlert, FeedEvent } from '../../src/types.ts';

const T0 = 1_700_000_000;

const blank: LaneFields = {
  agent: 'unknown',
  caption: null,
  prompt: null,
  intent: null,
  changes: [],
  branch: '',
  plan: null,
  decisions: [],
};

const change = { file_path: 'a.ts', diff: '+x' } as never;

function activity(extra: Record<string, unknown>, at: number): FeedEvent {
  return {
    type: 'activity_update',
    user_id: 'u1',
    prompt: 'p',
    changes: [],
    caption: null,
    timestamp: at,
    ...extra,
  } as FeedEvent;
}

test('relay "unknown" placeholders do not wipe what the lane knows', () => {
  let lane = applyLaneEvent(blank, activity({ agent: 'cursor', branch: 'main', changes: [change] }, T0) as never);
  lane = applyLaneEvent(lane, activity({ agent: 'unknown', branch: '' }, T0 + 1) as never);
  assert.equal(lane.agent, 'cursor');
  assert.equal(lane.branch, 'main');
  assert.deepEqual(lane.changes, [change], 'prompt-only update keeps the last diffs');
});

test('decisions: newest first, one per file, capped', () => {
  let lane = blank;
  for (const [i, file] of ['a', 'b', 'a', 'c', 'd'].entries()) {
    lane = applyLaneEvent(lane, {
      type: 'edit_decision',
      user_id: 'u1',
      file_path: file,
      decision: 'warn',
      timestamp: T0 + i,
    } as never);
  }
  assert.equal(lane.decisions.length, MAX_LANE_DECISIONS);
  assert.deepEqual(lane.decisions.map((d) => d.file_path), ['d', 'c', 'a']);
});

test('conflicts are replaced per file and pair, cleared by a non-actionable verdict', () => {
  const alert = (users: string[], verdict: string): ConflictAlert =>
    ({ type: 'conflict_alert', file_path: 'a.ts', users, verdict, timestamp: T0 }) as never;
  let list = replaceConflict([], alert(['u1', 'u2'], 'overlapping'));
  list = replaceConflict(list, alert(['u1', 'u3'], 'related'));
  assert.equal(list.length, 2, 'two pairs on one file are two alerts');
  list = replaceConflict(list, alert(['u2', 'u1'], 'independent'));
  assert.equal(list.length, 1);
  assert.deepEqual(list[0].users, ['u1', 'u3']);
});

test('replay keeps the branch when the relay later sends an empty one (parity with live)', () => {
  const events: FeedEvent[] = [
    activity({ branch: 'feature/x', agent: 'cursor' }, T0),
    activity({ branch: '' }, T0 + 1),
  ];
  const state = foldUntil(events, T0 + 1);
  assert.equal(state.lanes.u1.branch, 'feature/x');
  assert.equal(state.lanes.u1.agent, 'cursor');
});
