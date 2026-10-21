/**
 * The replay view's fold (src/replay.ts): history events up to time T become
 * lanes, decisions and conflicts. Run: npm run test:unit.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  CONFLICT_TTL_SECONDS,
  LANE_DECISIONS,
  advance,
  foldUntil,
  onHistoryChunk,
  publishHistoryChunk,
  sortEvents,
  spanLabel,
  timeBounds,
  type DecodedChunk,
} from '../../src/replay.ts';
import type {
  ActivityUpdate,
  ConflictAlert,
  EditDecision,
  FeedEvent,
  PlanUpdate,
} from '../../src/types.ts';

const T0 = 1_700_000_000;

function joined(user: string, at: number): FeedEvent {
  return { type: 'user_joined', user_id: user, agent: 'cursor', timestamp: at } as FeedEvent;
}

function left(user: string, at: number): FeedEvent {
  return { type: 'user_left', user_id: user, timestamp: at } as FeedEvent;
}

function edit(user: string, at: number, files: string[], prompt: string | null = null): ActivityUpdate {
  return {
    type: 'activity_update',
    user_id: user,
    timestamp: at,
    prompt,
    caption: `${user} edits`,
    changes: files.map((file_path) => ({ file_path, diff: `+${file_path}` })),
    agent: 'claude-code',
  };
}

function decided(user: string, at: number, file: string, decision: 'warn' | 'block'): EditDecision {
  return {
    type: 'edit_decision',
    user_id: user,
    file_path: file,
    decision,
    others: ['ada'],
    reason: '',
    timestamp: at,
    decision_id: `${user}-${at}`,
  };
}

function conflict(at: number, file: string, verdict: ConflictAlert['verdict'] = 'overlapping'): ConflictAlert {
  return { type: 'conflict_alert', users: ['ada', 'sam'], file_path: file, verdict, explanation: '', timestamp: at };
}

function plan(user: string, at: number, ttl: number): PlanUpdate {
  return {
    type: 'plan_update',
    user_id: user,
    summary: 'refactor',
    files: ['a.py'],
    symbols: [],
    timestamp: at,
    expires_at: at + ttl,
  };
}

const HISTORY: FeedEvent[] = sortEvents([
  joined('ada', T0),
  edit('ada', T0 + 10, ['a.py'], 'fix login'),
  joined('sam', T0 + 20),
  edit('sam', T0 + 30, ['a.py']),
  decided('sam', T0 + 31, 'a.py', 'block'),
  conflict(T0 + 32, 'a.py'),
  edit('ada', T0 + 40, [], 'next prompt'),
  decided('sam', T0 + 50, 'b.py', 'warn'),
  left('sam', T0 + 60),
]);

test('nothing has happened before the first event', () => {
  const state = foldUntil(HISTORY, T0 - 1);
  assert.equal(state.applied, 0);
  assert.deepEqual(state.lanes, {});
});

test('lanes, decisions and conflicts at a moment in the middle', () => {
  const state = foldUntil(HISTORY, T0 + 35);
  assert.deepEqual(Object.keys(state.lanes).sort(), ['ada', 'sam']);
  assert.equal(state.lanes.ada.prompt, 'fix login');
  assert.deepEqual(state.lanes.ada.changes.map((c) => c.file_path), ['a.py']);
  assert.equal(state.lanes.sam.decisions[0].decision, 'block');
  assert.equal(state.blocks, 1);
  assert.equal(state.warns, 0);
  assert.equal(state.conflicts.length, 1);
  assert.equal(state.applied, 6);
});

test('a prompt-only update keeps the last diffs, and leaving greys the lane', () => {
  const state = foldUntil(HISTORY, T0 + 60);
  assert.equal(state.lanes.ada.prompt, 'next prompt');
  assert.deepEqual(state.lanes.ada.changes.map((c) => c.file_path), ['a.py']);
  assert.equal(state.lanes.sam.online, false);
  assert.equal(state.conflicts.length, 0, "a departed member's conflicts end with them");
  assert.deepEqual(state.decisions.map((d) => d.decision), ['warn', 'block']);
});

test('conflicts expire after the TTL and an unrelated verdict clears one', () => {
  const events = sortEvents([conflict(T0, 'a.py'), conflict(T0 + 5, 'b.py'), conflict(T0 + 6, 'b.py', 'unrelated')]);
  assert.deepEqual(foldUntil(events, T0 + 5).conflicts.map((c) => c.file_path), ['b.py', 'a.py']);
  assert.deepEqual(foldUntil(events, T0 + 6).conflicts.map((c) => c.file_path), ['a.py']);
  assert.equal(foldUntil(events, T0 + CONFLICT_TTL_SECONDS + 1).conflicts.length, 0);
});

test('lane decisions keep the newest per file, capped', () => {
  const events = sortEvents([
    decided('sam', T0, 'a.py', 'warn'),
    decided('sam', T0 + 1, 'a.py', 'block'),
    decided('sam', T0 + 2, 'b.py', 'warn'),
    decided('sam', T0 + 3, 'c.py', 'warn'),
    decided('sam', T0 + 4, 'd.py', 'warn'),
  ]);
  const lane = foldUntil(events, T0 + 10).lanes.sam;
  assert.equal(lane.decisions.length, LANE_DECISIONS);
  assert.deepEqual(lane.decisions.map((d) => d.file_path), ['d.py', 'c.py', 'b.py']);
});

test('plans show only while unexpired at T', () => {
  const events = [plan('ada', T0, 60)];
  assert.ok(foldUntil(events, T0 + 30).lanes.ada.plan);
  assert.equal(foldUntil(events, T0 + 61).lanes.ada.plan, null);
});

test('sorting is stable and gives timestamp-less events the previous time', () => {
  const noTime = { type: 'room_policy_update', require_approval: true } as FeedEvent;
  const sorted = sortEvents([edit('ada', T0 + 5, ['x']), noTime, edit('sam', T0 + 1, ['y'])]);
  assert.deepEqual(
    sorted.map((e) => (e.type === 'activity_update' ? e.user_id : e.type)),
    ['sam', 'ada', 'room_policy_update'],
  );
  assert.deepEqual(timeBounds(sorted), { start: T0 + 1, end: T0 + 5 });
  assert.equal(timeBounds([]), null);
});

test('playback advances at the chosen speed and stops at the end', () => {
  assert.equal(advance(100, 1000, 1, 1000), 101);
  assert.equal(advance(100, 1000, 60, 1000), 160);
  assert.equal(advance(100, 60_000, 60, 1000), 1000);
  assert.equal(advance(100, -5, 10, 1000), 100);
  assert.equal(spanLabel(3725), '1h 02m');
  assert.equal(spanLabel(75), '1m 15s');
});

test('history chunks reach subscribers decoded and in order', async () => {
  const seen: DecodedChunk[] = [];
  const stop = onHistoryChunk((chunk) => seen.push(chunk));
  const slowFirst = async (event: FeedEvent) => {
    if (event.type === 'user_joined') await new Promise((resolve) => setTimeout(resolve, 20));
    return { ...event, decoded: true } as FeedEvent;
  };
  void publishHistoryChunk({ type: 'history_chunk', events: [joined('ada', T0)], done: false }, slowFirst);
  await publishHistoryChunk({ type: 'history_chunk', events: [left('ada', T0 + 1)], done: true }, slowFirst);
  stop();
  assert.deepEqual(seen.map((c) => c.done), [false, true]);
  assert.equal((seen[0].events[0] as { decoded?: boolean }).decoded, true);
  await publishHistoryChunk({ type: 'history_chunk', events: [], done: true }, slowFirst);
  assert.equal(seen.length, 2, 'unsubscribed listeners hear nothing');
});
