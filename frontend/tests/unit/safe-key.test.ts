/**
 * Ids used as object keys in dashboard state cannot reach Object.prototype.
 * Run: npm run test:unit
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { hasSafeKeys, isSafeKey, withEntry } from '../../src/safeKey.ts';
import { applyFeedbackUpdate } from '../../src/detection.ts';

test('prototype keys are unsafe, ordinary ids are not', () => {
  for (const key of ['__proto__', 'constructor', 'prototype']) assert.equal(isSafeKey(key), false);
  for (const key of ['maya', 'tomas-o', 'd-123', undefined]) assert.equal(isSafeKey(key), true);
});

test('a frame is safe only when every id is', () => {
  assert.equal(hasSafeKeys({ user_id: 'maya', decision_id: 'd1' }), true);
  assert.equal(hasSafeKeys({ user_id: '__proto__' }), false);
  assert.equal(hasSafeKeys({ user_id: 'maya', decision_id: 'constructor' }), false);
});

test('a feedback update for a prototype key leaves the map and Object.prototype alone', () => {
  const prev = {};
  const next = applyFeedbackUpdate(prev, {
    type: 'decision_feedback_update',
    decision_id: '__proto__',
    useful_count: 1,
    noise_count: 0,
  } as Parameters<typeof applyFeedbackUpdate>[1]);
  assert.equal(next, prev);
  assert.equal(({} as Record<string, unknown>).useful_count, undefined);
});

test('withEntry sets own properties, keeps key order, refuses unsafe keys', () => {
  const base = { a: 1, b: 2 };
  const next = withEntry(base, 'a', 9);
  assert.deepEqual(Object.keys(next), ['a', 'b']);
  assert.equal(next.a, 9);
  assert.equal(base.a, 1, 'input untouched');
  assert.equal(withEntry(base, '__proto__', 5), base);
  assert.equal(({} as Record<string, unknown>).polluted, undefined);
});
