/**
 * Lane status, keyboard shortcut sequences, the typing guard and theme
 * resolution. Run: npm run test:unit
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  BLOCKED_FOR_SECONDS,
  IDLE_AFTER_SECONDS,
  laneStatus,
} from '../../src/dashboard/laneStatus.ts';
import { diffStat, initials, personColor, relativeTime } from '../../src/dashboard/people.ts';
import { createShortcutMatcher, isTypingTarget } from '../../src/dashboard/shortcuts.ts';
import { parseThemePref, resolveTheme } from '../../src/dashboard/theme.ts';

const NOW = 10_000;

test('lane status precedence: offline > blocked > editing > idle', () => {
  const recentBlock = { decision: 'block' as const, timestamp: NOW - 5 };
  assert.equal(
    laneStatus({ offline: true, lastActivity: NOW, lastDecision: recentBlock, nowSeconds: NOW }),
    'offline',
  );
  assert.equal(
    laneStatus({ offline: false, lastActivity: NOW, lastDecision: recentBlock, nowSeconds: NOW }),
    'blocked',
  );
  assert.equal(
    laneStatus({
      offline: false,
      lastActivity: NOW - 10,
      lastDecision: { decision: 'block', timestamp: NOW - BLOCKED_FOR_SECONDS },
      nowSeconds: NOW,
    }),
    'editing',
  );
  assert.equal(
    laneStatus({
      offline: false,
      lastActivity: NOW,
      lastDecision: { decision: 'warn', timestamp: NOW },
      nowSeconds: NOW,
    }),
    'editing',
  );
  assert.equal(
    laneStatus({
      offline: false,
      lastActivity: NOW - IDLE_AFTER_SECONDS,
      lastDecision: null,
      nowSeconds: NOW,
    }),
    'editing',
  );
  assert.equal(
    laneStatus({
      offline: false,
      lastActivity: NOW - IDLE_AFTER_SECONDS - 1,
      lastDecision: null,
      nowSeconds: NOW,
    }),
    'idle',
  );
  assert.equal(
    laneStatus({ offline: false, lastActivity: null, lastDecision: null, nowSeconds: NOW }),
    'idle',
  );
});

test('shortcut sequences: g-prefixed views, single keys, timeouts', () => {
  const match = createShortcutMatcher();
  assert.equal(match('g', 0), null);
  assert.equal(match('t', 100), 'go-timeline');
  assert.equal(match('g', 200), null);
  assert.equal(match('h', 300), 'go-heatmap');
  assert.equal(match('g', 400), null);
  assert.equal(match('l', 500), 'go-lanes');
  assert.equal(match('/', 600), 'focus-search');
  assert.equal(match('?', 700), 'toggle-help');
  assert.equal(match('Escape', 800), 'close');
  // Too slow: the "g" expired.
  assert.equal(match('g', 1000), null);
  assert.equal(match('t', 5000), null);
  // A stray key resets; "g g t" still works.
  assert.equal(match('g', 6000), null);
  assert.equal(match('x', 6100), null);
  assert.equal(match('t', 6200), null);
  assert.equal(match('g', 7000), null);
  assert.equal(match('g', 7100), null);
  assert.equal(match('t', 7200), 'go-timeline');
  // "g" then "/" focuses search at once.
  assert.equal(match('g', 8000), null);
  assert.equal(match('/', 8100), 'focus-search');
});

test('typing guard', () => {
  assert.equal(isTypingTarget({ tagName: 'INPUT', type: 'text' }), true);
  assert.equal(isTypingTarget({ tagName: 'input', type: 'search' }), true);
  assert.equal(isTypingTarget({ tagName: 'INPUT', type: 'checkbox' }), false);
  assert.equal(isTypingTarget({ tagName: 'TEXTAREA' }), true);
  assert.equal(isTypingTarget({ tagName: 'SELECT' }), true);
  assert.equal(isTypingTarget({ tagName: 'DIV', isContentEditable: true }), true);
  assert.equal(isTypingTarget({ tagName: 'BUTTON' }), false);
  assert.equal(isTypingTarget(null), false);
});

test('theme preference parsing and resolution', () => {
  assert.equal(parseThemePref('light'), 'light');
  assert.equal(parseThemePref('nonsense'), 'system');
  assert.equal(parseThemePref(null), 'system');
  assert.equal(resolveTheme('system', true), 'dark');
  assert.equal(resolveTheme('system', false), 'light');
  assert.equal(resolveTheme('light', true), 'light');
  assert.equal(resolveTheme('dark', false), 'dark');
});

test('people helpers', () => {
  assert.equal(personColor('ada'), personColor('ada'));
  assert.match(personColor('sam'), /^#[0-9a-f]{6}$/);
  assert.equal(initials('ada'), 'AD');
  assert.equal(initials('ada-lovelace'), 'AL');
  assert.equal(relativeTime(NOW - 30, NOW), '30s ago');
  assert.equal(relativeTime(NOW - 600, NOW), '10m ago');
  assert.equal(relativeTime(null, NOW), 'never');
  assert.deepEqual(diffStat('--- a\n+++ b\n@@ -1 +1 @@\n-old\n+new\n+more'), { added: 2, removed: 1 });
});
