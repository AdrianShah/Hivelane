/**
 * The session log reducer: de-duplication, ordering, caps and selectors.
 * Run: npm run test:unit
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  MAX_TIMELINE,
  createSessionLog,
  diffFor,
  filesEditedSince,
  lastDecisionFor,
  recentTouches,
  reduceSessionLog,
  sessionPeople,
  type SessionLog,
  type SessionLogAction,
} from '../../src/dashboard/sessionLog.ts';
import { activity, conflict, decision, intent, plan } from './fixtures.ts';

function apply(actions: SessionLogAction[], start: SessionLog = createSessionLog()): SessionLog {
  return actions.reduce(reduceSessionLog, start);
}

test('activity becomes sorted, normalised touches with the intent of the moment', () => {
  const log = apply([
    intent('sam', 5, 'fix login'),
    activity('sam', 10, ['src\\auth.ts', './src/app.ts'], { caption: 'Hardened auth' }),
    activity('ada', 8, ['README.md']),
  ]);
  assert.deepEqual(
    log.touches.map((t) => [t.user_id, t.file_path, t.timestamp]),
    [
      ['ada', 'README.md', 8],
      ['sam', 'src/auth.ts', 10],
      ['sam', 'src/app.ts', 10],
    ],
  );
  assert.equal(log.touches[1].intent, 'fix login');
  assert.equal(log.touches[1].caption, 'Hardened auth');
  assert.equal(log.startedAt, 5);
  assert.deepEqual(sessionPeople(log), ['ada', 'sam']);
});

test('replayed frames change nothing', () => {
  const once = apply([activity('sam', 10, ['a.ts']), decision('sam', 11, 'a.ts', 'warn')]);
  const twice = apply([activity('sam', 10, ['a.ts']), decision('sam', 11, 'a.ts', 'warn')], once);
  assert.equal(twice.touches.length, 1);
  assert.equal(twice.timeline.length, 1);
  // Unrelated frames return the very same object (cheap re-renders).
  assert.equal(
    reduceSessionLog(once, {
      type: 'decision_feedback_update',
      decision_id: 'x',
      useful_count: 1,
      noise_count: 0,
    }),
    once,
  );
});

test('prompt-only activity counts as activity but adds no touches', () => {
  const log = apply([activity('sam', 30, [])]);
  assert.equal(log.touches.length, 0);
  assert.equal(log.lastActivity.sam, 30);
});

test('timeline is newest first; decisions list the requester first', () => {
  const log = apply([
    decision('sam', 20, 'a.ts', 'block', { others: ['ada', 'sam'] }),
    decision('lin', 10, 'b.ts', 'warn'),
    conflict(['ada', 'sam'], 30, 'c.ts'),
  ]);
  assert.deepEqual(
    log.timeline.map((e) => [e.kind, e.timestamp]),
    [
      ['conflict', 30],
      ['block', 20],
      ['warn', 10],
    ],
  );
  assert.deepEqual(log.timeline[1].users, ['sam', 'ada']);
});

test('repeated conflicts on one file fold into one entry; unrelated verdicts are ignored', () => {
  const log = apply([
    conflict(['ada', 'sam'], 100, 'a.ts'),
    conflict(['sam', 'ada'], 150, 'a.ts'),
    conflict(['ada', 'sam'], 150, 'a.ts'), // replay
    conflict(['ada', 'sam'], 160, 'a.ts', 'unrelated'),
    conflict(['ada', 'sam'], 500, 'a.ts'), // long after: a new conflict
  ]);
  assert.equal(log.timeline.length, 2);
  const folded = log.timeline[1];
  assert.equal(folded.repeats, 2);
  assert.equal(folded.timestamp, 100);
  assert.equal(folded.lastSeen, 150);
});

test('intent snapshots are taken when the entry arrives', () => {
  const log = apply([
    intent('sam', 1, 'add OAuth'),
    intent('ada', 2, 'rename login'),
    decision('sam', 3, 'auth.ts', 'warn'),
    intent('sam', 4, 'something else'),
  ]);
  assert.deepEqual(log.timeline[0].intents, { sam: 'add OAuth', ada: 'rename login' });
  assert.equal(log.intents.sam.prompt, 'something else');
});

test('older intents and plans never overwrite newer ones', () => {
  const log = apply([intent('sam', 10, 'new'), intent('sam', 5, 'old'), plan('sam', 10, ['a']), plan('sam', 5, ['b'])]);
  assert.equal(log.intents.sam.prompt, 'new');
  assert.deepEqual(log.plans.sam.files, ['a']);
});

test('the timeline is capped', () => {
  const actions: SessionLogAction[] = [];
  for (let i = 0; i < MAX_TIMELINE + 25; i += 1) actions.push(decision('sam', i, `f${i}.ts`, 'warn'));
  const log = apply(actions);
  assert.equal(log.timeline.length, MAX_TIMELINE);
  assert.equal(log.timeline[0].timestamp, MAX_TIMELINE + 24);
});

test('selectors: recent edits, files since, diff lookup, last decision', () => {
  const log = apply([
    activity('sam', 1, ['a.ts']),
    activity('sam', 2, ['b.ts']),
    activity('ada', 3, ['a.ts']),
    activity('sam', 4, ['c.ts']),
    decision('sam', 5, 'a.ts', 'warn'),
    decision('sam', 6, 'b.ts', 'block'),
    activity('sam', 50, ['a.ts']),
  ]);
  assert.deepEqual(
    recentTouches(log, 'sam', 3).map((t) => t.file_path),
    ['a.ts', 'c.ts', 'b.ts'],
  );
  assert.deepEqual([...filesEditedSince(log, 'sam', 2)].sort(), ['a.ts', 'b.ts', 'c.ts']);
  assert.equal(diffFor(log, 'sam', 'a.ts', 5)?.timestamp, 50);
  assert.equal(diffFor(log, 'sam', 'a.ts', 5, 0)?.timestamp, 1);
  assert.equal(diffFor(log, 'lin', 'a.ts', 5), null);
  assert.equal(lastDecisionFor(log, 'sam')?.decision, 'block');
  assert.equal(lastDecisionFor(log, 'ada'), null);
});

test('reset clears everything', () => {
  const log = apply([activity('sam', 1, ['a.ts']), { type: 'reset' }]);
  assert.deepEqual(log, createSessionLog());
});
