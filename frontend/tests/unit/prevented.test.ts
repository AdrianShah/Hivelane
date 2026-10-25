/**
 * The prevented-collision heuristic (see src/dashboard/prevented.ts).
 * Run: npm run test:unit
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { groupEpisodes, summarizePrevention } from '../../src/dashboard/prevented.ts';
import {
  createSessionLog,
  reduceSessionLog,
  type SessionLogAction,
} from '../../src/dashboard/sessionLog.ts';
import { roomStats } from '../../src/dashboard/stats.ts';
import { activity, conflict, decision } from './fixtures.ts';

function log(actions: SessionLogAction[]) {
  return actions.reduce(reduceSessionLog, createSessionLog());
}

test('a block followed by an edit to another file within 2 min is prevented', () => {
  const l = log([decision('sam', 100, 'a.ts', 'block'), activity('sam', 160, ['b.ts'])]);
  const s = summarizePrevention(l, 1000);
  assert.equal(s.prevented, 1);
  assert.equal(s.outcomes['d-sam-100'], 'prevented');
});

test('editing the same file next is "edited anyway", even inside a multi-file update', () => {
  const l = log([
    decision('sam', 100, 'a.ts', 'warn'),
    activity('sam', 101, ['b.ts', 'a.ts']),
    decision('lin', 100, 'x.ts', 'warn'),
    activity('lin', 101, ['x.ts']),
  ]);
  const s = summarizePrevention(l, 1000);
  assert.equal(s.prevented, 0);
  assert.equal(s.editedAnyway, 2);
});

test('only the first edit after the decision decides the outcome', () => {
  const l = log([
    decision('sam', 100, 'a.ts', 'block'),
    activity('sam', 110, ['a.ts']),
    activity('sam', 120, ['b.ts']),
  ]);
  assert.equal(summarizePrevention(l, 1000).editedAnyway, 1);
});

test('an edit after the window does not count; an open window is pending', () => {
  const late = log([decision('sam', 100, 'a.ts', 'block'), activity('sam', 221, ['b.ts'])]);
  assert.equal(summarizePrevention(late, 1000).unknown, 1);
  const open = log([decision('sam', 100, 'a.ts', 'block')]);
  assert.equal(summarizePrevention(open, 150).pending, 1);
  assert.equal(summarizePrevention(open, 300).unknown, 1);
  // Edits before the decision are irrelevant.
  const before = log([activity('sam', 90, ['b.ts']), decision('sam', 100, 'a.ts', 'block')]);
  assert.equal(summarizePrevention(before, 1000).unknown, 1);
});

test('retries of one blocked edit are one episode, judged after the last retry', () => {
  const l = log([
    decision('sam', 100, 'a.ts', 'block'),
    decision('sam', 130, 'a.ts', 'block'),
    decision('sam', 200, 'a.ts', 'warn'),
    activity('sam', 260, ['b.ts']),
  ]);
  const episodes = groupEpisodes(l.timeline);
  assert.equal(episodes.length, 1);
  assert.deepEqual(episodes[0].keys, ['d-sam-100', 'd-sam-130', 'd-sam-200']);
  assert.equal(episodes[0].blocked, true);
  const s = summarizePrevention(l, 1000);
  assert.equal(s.prevented, 1);
  assert.equal(s.outcomes['d-sam-100'], 'prevented');
});

test('different people or files are separate episodes; conflicts are not decisions', () => {
  const l = log([
    decision('sam', 100, 'a.ts', 'block'),
    decision('sam', 101, 'b.ts', 'warn'),
    decision('lin', 102, 'a.ts', 'warn'),
    conflict(['sam', 'lin'], 103, 'a.ts'),
    activity('sam', 110, ['c.ts']),
    activity('lin', 110, ['d.ts']),
  ]);
  const s = summarizePrevention(l, 1000);
  assert.equal(s.episodes.length, 3);
  assert.equal(s.prevented, 3);
});

test('room stats carry counts and agent kinds only, no names, files or prompts', () => {
  const l = log([
    activity('sam', 100, ['secret/path.ts'], { prompt: 'secret prompt', agent: 'cursor' }),
    decision('sam', 110, 'secret/path.ts', 'block', { agent: 'cursor' }),
    activity('sam', 120, ['other.ts'], { agent: 'cursor' }),
    activity('ada', 130, ['x.ts'], { agent: 'claude-code' }),
    conflict(['ada', 'sam'], 140, 'x.ts'),
  ]);
  const prevention = summarizePrevention(l, 400);
  const stats = roomStats(l, prevention, { 'd-sam-110': { useful_count: 2, noise_count: 1 } }, 400);
  assert.equal(stats.prevented, 1);
  assert.equal(stats.blocks, 1);
  assert.equal(stats.conflicts, 1);
  assert.equal(stats.people, 2);
  assert.equal(stats.filesTouched, 3);
  assert.equal(stats.usefulVotes, 2);
  assert.deepEqual([...stats.agentKinds].sort(), ['Claude Code', 'Cursor']);
  assert.equal(stats.editHistogram.reduce((a, b) => a + b, 0), 3);
  const serialized = JSON.stringify(stats);
  for (const secret of ['secret', 'sam', 'ada', 'path.ts', 'x.ts']) {
    assert.ok(!serialized.includes(secret), `stats leak "${secret}"`);
  }
});
