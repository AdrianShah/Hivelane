/**
 * Dashboard v3's pure pieces: the headline sentence, sparkline paths, the
 * session chart series, lane activity buckets, the overlap bar, lane state
 * precedence, room expiry labels, owned-room scan and the command matcher.
 */
import assert from 'node:assert/strict';
import test from 'node:test';

import { baseName, headline } from '../../src/dashboard/headline.ts';
import {
  barHeights,
  laneBuckets,
  overlapBar,
  sessionSeries,
  sparkPath,
} from '../../src/dashboard/charts.ts';
import { laneState, WAITING_FOR_SECONDS } from '../../src/dashboard/laneState.ts';
import { expiresSoon, expiryLabel, ownedRoomCodes, rttLabel } from '../../src/dashboard/roomLife.ts';
import { isCommandMenuKey, matchCommands, type Command } from '../../src/dashboard/commands.ts';

test('headline: holds, then overlaps, then all clear, then empty', () => {
  assert.deepEqual(headline({ online: 0, conflicts: [], held: [] }), {
    text: 'Listening for your first agent.',
    tone: 'empty',
  });
  assert.equal(headline({ online: 1, conflicts: [], held: [] }).text, 'One agent is working, and nothing overlaps.');
  assert.deepEqual(headline({ online: 3, conflicts: [], held: [] }), {
    text: '3 agents are working, and nothing overlaps.',
    tone: 'clear',
  });
  const overlap = { users: ['maya', 'tomas'], file_path: 'src/auth/session.ts' };
  assert.deepEqual(headline({ online: 3, conflicts: [overlap], held: [] }), {
    text: 'maya and tomas are both in session.ts.',
    tone: 'warn',
  });
  assert.equal(
    headline({ online: 3, conflicts: [overlap, { users: ['a', 'b', 'c'], file_path: 'x.py' }], held: [] }).text,
    '2 overlaps need a look, starting with session.ts.',
  );
  const held = [{ user_id: 'maya', file_path: 'src/auth/session.ts' }];
  assert.deepEqual(headline({ online: 3, conflicts: [overlap], held }), {
    text: "Hivelane is holding maya's edit to session.ts.",
    tone: 'block',
  });
  assert.equal(
    headline({ online: 3, conflicts: [], held: [...held, { user_id: 'x', file_path: 'y' }] }).text,
    'Hivelane is holding 2 edits, starting with maya on session.ts.',
  );
  assert.equal(baseName('a\\b\\c.ts'), 'c.ts');
  assert.equal(baseName('plain'), 'plain');
});

test('sparkline paths stay inside the box and handle flat or short series', () => {
  assert.deepEqual(sparkPath([5], 100, 20), { line: '', area: '', end: null });
  const flat = sparkPath([3, 3, 3], 100, 20);
  assert.equal(flat.line, 'M0 10 L50 10 L100 10');
  const rising = sparkPath([0, 5, 10], 100, 20, 2);
  assert.equal(rising.line, 'M0 18 L50 10 L100 2');
  assert.equal(rising.area, 'M0 18 L50 10 L100 2 L100 20 L0 20 Z');
  assert.deepEqual(rising.end, { x: 100, y: 2 });
  // Non-finite values read as zero rather than breaking the path.
  assert.ok(!sparkPath([Number.NaN, 1, 2], 10, 10).line.includes('NaN'));
});

test('session series: range window, bucket counts and event dots', () => {
  const now = 10_000;
  const series = sessionSeries(
    [now - 900, now - 450, now - 449, now - 1, now + 3, now - 5000],
    [
      { key: 'b', kind: 'block', timestamp: now - 450 },
      { key: 'old', kind: 'warn', timestamp: now - 5000 },
    ],
    '15m',
    now,
    now - 6000,
    3,
  );
  assert.equal(series.start, now - 900);
  assert.deepEqual(series.counts, [1, 2, 2]);
  assert.deepEqual(
    series.dots.map((d) => [d.key, d.bucket, Math.round(d.x * 100)]),
    [['b', 1, 50]],
  );
  const all = sessionSeries([now - 5000], [], 'all', now, now - 6000, 6);
  assert.equal(all.start, now - 6000);
  assert.equal(all.counts.reduce((a, b) => a + b, 0), 1);
});

test('lane buckets: 24 slices of the last half hour, oldest first', () => {
  const now = 1800;
  // 1850 is a little ahead of the coarse clock: it counts as now; 2000 is too far.
  const buckets = laneBuckets([0, 1, 75, 1799, 1800, 1850, -5, 2000], now);
  assert.equal(buckets.length, 24);
  assert.equal(buckets[0], 2);
  assert.equal(buckets[1], 1);
  assert.equal(buckets[23], 3);
  assert.equal(buckets.reduce((a, b) => a + b, 0), 6);
  assert.deepEqual(barHeights([0, 2, 4]), [0, 0.5, 1]);
  assert.deepEqual(barHeights([0, 0]), [0, 0]);
});

test('overlap bar: segments and their intersection as fractions of the span', () => {
  const bar = overlapBar(
    [
      [10, 19],
      [20, 29],
    ],
    [[25, 49]],
  );
  assert.ok(bar);
  assert.equal(bar.lo, 10);
  assert.equal(bar.hi, 49);
  assert.deepEqual(bar.mine, [[0, 0.5]]);
  assert.deepEqual(bar.theirs, [[0.38, 1]]);
  assert.deepEqual(bar.both, [[0.38, 0.5]]);
  assert.equal(overlapBar([], [[1, 2]]), null);
  assert.deepEqual(overlapBar([[1, 5]], [[9, 12]])?.both, []);
});

test('lane state precedence and released holds', () => {
  const base = {
    status: 'editing' as const,
    lastDecision: null,
    releasedAt: null,
    queuedAt: null,
    inConflict: false,
    nowSeconds: 1000,
  };
  assert.equal(laneState(base), 'editing');
  assert.equal(laneState({ ...base, status: 'idle' }), 'idle');
  assert.equal(laneState({ ...base, inConflict: true }), 'conflict');
  const block = { decision: 'block' as const, timestamp: 990 };
  assert.equal(laneState({ ...base, status: 'blocked', lastDecision: block }), 'blocked');
  assert.equal(laneState({ ...base, lastDecision: { ...block, queue_position: 2 } }), 'waiting');
  // An owner released it: back to plain status, even inside the block display.
  assert.equal(laneState({ ...base, status: 'blocked', lastDecision: block, releasedAt: 995 }), 'editing');
  // Put behind someone by an owner.
  assert.equal(laneState({ ...base, queuedAt: 999 }), 'waiting');
  assert.equal(laneState({ ...base, queuedAt: 1000 - WAITING_FOR_SECONDS - 1 }), 'editing');
  // Offline wins over everything.
  assert.equal(laneState({ ...base, status: 'offline', lastDecision: block, inConflict: true }), 'offline');
});

test('room expiry and relay round-trip labels', () => {
  const now = 1_000_000;
  assert.equal(expiryLabel(0, now), null);
  assert.equal(expiryLabel(now - 1, now), 'Expired');
  assert.equal(expiryLabel(now + 30, now), 'Expires in 1 min');
  assert.equal(expiryLabel(now + 5 * 3600 + 5, now), 'Expires in 5 h');
  assert.equal(expiryLabel(now + 6 * 86400 + 100, now), 'Expires in 6 days');
  assert.equal(expiryLabel(now + 7 * 86400 - 60, now), 'Expires in 7 days');
  assert.equal(expiresSoon(now + 3600, now), true);
  assert.equal(expiresSoon(now + 3 * 86400, now), false);
  assert.equal(rttLabel(41.6), '42 ms');
  assert.equal(rttLabel(1520), '1.5 s');
  assert.equal(rttLabel(null), null);
});

test('owned rooms come from this relay only', () => {
  const keys = [
    'hivelane:owner-token:ws://localhost:8000/ws|K7M2PQ2X',
    'hivelane:owner-token:wss://other.example/ws|AAAABBBB',
    'hivelane:owner-token:ws://localhost:8000/ws|ABCD2345',
    'unrelated',
    'hivelane:owner-token:ws://localhost:8000/ws|bad code',
  ];
  const storage = { length: keys.length, key: (i: number) => keys[i] ?? null };
  assert.deepEqual(ownedRoomCodes(storage, 'hivelane:owner-token:', 'ws://localhost:8000/ws'), [
    'ABCD2345',
    'K7M2PQ2X',
  ]);
  assert.deepEqual(ownedRoomCodes(null, 'x', 'y'), []);
});

test('command menu matching and its shortcut', () => {
  const commands: Command[] = [
    { id: 'timeline', label: 'Timeline', group: 'Go to' },
    { id: 'invite', label: 'Invite teammates', group: 'Actions', keywords: ['share', 'link'] },
    { id: 'theme', label: 'Toggle theme', group: 'Actions', keywords: ['dark', 'light'] },
  ];
  assert.deepEqual(matchCommands(commands, '').map((c) => c.id), ['timeline', 'invite', 'theme']);
  assert.deepEqual(matchCommands(commands, 'inv').map((c) => c.id), ['invite']);
  assert.deepEqual(matchCommands(commands, 'share').map((c) => c.id), ['invite']);
  // Labels that start with the query rank first.
  assert.deepEqual(matchCommands(commands, 't').map((c) => c.id), ['timeline', 'theme', 'invite']);
  assert.deepEqual(matchCommands(commands, 'dark mode').map((c) => c.id), []);
  assert.equal(isCommandMenuKey({ key: 'k', metaKey: true, ctrlKey: false, altKey: false }), true);
  assert.equal(isCommandMenuKey({ key: 'K', metaKey: false, ctrlKey: true, altKey: false }), true);
  assert.equal(isCommandMenuKey({ key: 'k', metaKey: false, ctrlKey: false, altKey: false }), false);
});
