/**
 * URL filter parsing/serialisation and the per-view matchers.
 * Run: npm run test:unit
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  EMPTY_FILTERS,
  activeFilterCount,
  fileMatches,
  matchesLane,
  matchesTimelineEntry,
  matchesTouch,
  parseFilters,
  sameFilterText,
  textMatches,
  toggleValue,
  writeFilters,
  type DashboardFilters,
} from '../../src/dashboard/filters.ts';
import {
  createSessionLog,
  reduceSessionLog,
  type SessionLogAction,
} from '../../src/dashboard/sessionLog.ts';
import { activity, conflict, decision, intent } from './fixtures.ts';

function log(actions: SessionLogAction[]) {
  return actions.reduce(reduceSessionLog, createSessionLog());
}

function f(partial: Partial<DashboardFilters>): DashboardFilters {
  return { ...EMPTY_FILTERS, ...partial };
}

test('parses repeated and comma-separated values, dropping junk', () => {
  const parsed = parseFilters(
    new URLSearchParams(
      'person=ada&person=sam,lin&person=ada&agent=Cursor&type=block,nope&scope=symbol&scope=bogus&file=%20src/auth%20&q=%20login%20bug%20&view=timeline&person=%3Cscript%3E',
    ),
  );
  assert.deepEqual(parsed, {
    people: ['ada', 'sam', 'lin'],
    file: 'src/auth',
    agents: ['cursor'],
    types: ['block'],
    scopes: ['symbol'],
    q: 'login bug',
  });
  assert.deepEqual(parseFilters(new URLSearchParams('')), EMPTY_FILTERS);
  assert.equal(parseFilters(new URLSearchParams(`q=${'x'.repeat(500)}`)).q.length, 200);
});

test('writing keeps unrelated keys and round-trips', () => {
  const filters = f({ people: ['ada', 'sam'], types: ['warn'], q: 'oauth', file: 'auth' });
  const written = writeFilters(new URLSearchParams('view=timeline&person=old'), filters);
  assert.equal(written.get('view'), 'timeline');
  assert.deepEqual(written.getAll('person'), ['ada', 'sam']);
  assert.deepEqual(parseFilters(written), filters);
  assert.equal(writeFilters(new URLSearchParams('view=heatmap'), EMPTY_FILTERS).toString(), 'view=heatmap');
  assert.equal(activeFilterCount(filters), 5);
  assert.deepEqual(toggleValue(['a', 'b'], 'a'), ['b']);
  assert.deepEqual(toggleValue(['a'], 'b'), ['a', 'b']);
});

test('text and file matching', () => {
  assert.equal(textMatches('', []), true);
  assert.equal(textMatches('Login bug', ['fix the LOGIN flow', 'a bug']), true);
  assert.equal(textMatches('login crash', ['fix the login flow']), false);
  assert.equal(fileMatches('SRC\\Auth', 'src/auth/login.ts'), true);
  assert.equal(fileMatches('', 'anything'), true);
  assert.equal(fileMatches('server', 'src/app.ts'), false);
});

test('timeline matching: people, agents, file, type, scope, text', () => {
  const l = log([
    intent('sam', 1, 'add OAuth login'),
    decision('sam', 2, 'src/auth.ts', 'block', { scope: 'symbol', agent: 'cursor', others: ['ada'] }),
    conflict(['ada', 'lin'], 3, 'docs/readme.md'),
  ]);
  const [conf, block] = l.timeline;
  const agents = { ...l.agents, ada: 'claude-code' as const };
  assert.equal(matchesTimelineEntry(block, f({ people: ['ada'] }), agents), true);
  assert.equal(matchesTimelineEntry(block, f({ people: ['lin'] }), agents), false);
  assert.equal(matchesTimelineEntry(block, f({ agents: ['cursor'] }), agents), true);
  assert.equal(matchesTimelineEntry(conf, f({ agents: ['cursor'] }), agents), false);
  assert.equal(matchesTimelineEntry(block, f({ file: 'auth' }), agents), true);
  assert.equal(matchesTimelineEntry(block, f({ types: ['warn', 'conflict'] }), agents), false);
  assert.equal(matchesTimelineEntry(conf, f({ types: ['conflict'] }), agents), true);
  assert.equal(matchesTimelineEntry(block, f({ scopes: ['symbol'] }), agents), true);
  assert.equal(matchesTimelineEntry(conf, f({ scopes: ['symbol'] }), agents), false);
  assert.equal(matchesTimelineEntry(block, f({ q: 'oauth' }), agents), true);
  assert.equal(matchesTimelineEntry(conf, f({ q: 'oauth' }), agents), false);
});

test('touch and lane matching', () => {
  const l = log([
    intent('sam', 1, 'add OAuth'),
    activity('sam', 2, ['src/auth.ts'], { caption: 'Wired the callback', agent: 'cursor' }),
    decision('sam', 3, 'src/auth.ts', 'warn', { scope: 'lines' }),
  ]);
  const touch = l.touches[0];
  assert.equal(matchesTouch(touch, f({ q: 'callback' })), true);
  assert.equal(matchesTouch(touch, f({ q: 'oauth' })), true);
  assert.equal(matchesTouch(touch, f({ people: ['ada'] })), false);
  assert.equal(matchesTouch(touch, f({ agents: ['cursor'] })), true);

  const lane = {
    userId: 'sam',
    agent: 'cursor',
    files: ['src/auth.ts'],
    texts: ['add OAuth', 'Wired the callback'],
    entries: l.timeline,
  };
  assert.equal(matchesLane(lane, EMPTY_FILTERS), true);
  assert.equal(matchesLane(lane, f({ file: 'server' })), false);
  assert.equal(matchesLane(lane, f({ types: ['warn'], scopes: ['lines'] })), true);
  assert.equal(matchesLane(lane, f({ types: ['block'] })), false);
  assert.equal(matchesLane(lane, f({ q: 'callback oauth' })), true);
});

test('a trailing space in a draft is not a change from the URL (finding 06-9)', () => {
  // writeFilters trims; the draft "login " must survive its own round trip.
  const url = writeFilters(new URLSearchParams(), { ...EMPTY_FILTERS, q: 'login ' });
  const back = parseFilters(url).q;
  assert.equal(back, 'login');
  assert.ok(sameFilterText('login ', back));
  assert.ok(!sameFilterText('login', 'logout'));
});
