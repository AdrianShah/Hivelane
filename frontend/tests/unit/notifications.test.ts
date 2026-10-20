/**
 * Browser-notification decisions and stored preferences (src/notifications.ts).
 * Run: npm run test:unit.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  DEFAULT_PREFS,
  guessKind,
  noticeFor,
  parsePrefs,
  relayName,
  webhookUrlProblem,
  type NotificationPrefs,
} from '../../src/notifications.ts';
import type { ConflictAlert, EditDecision } from '../../src/types.ts';

const ON: NotificationPrefs = { ...DEFAULT_PREFS, enabled: true };

function block(user: string, others: string[], decision: 'block' | 'warn' = 'block'): EditDecision {
  return {
    type: 'edit_decision',
    user_id: user,
    file_path: 'server/room.py',
    decision,
    others,
    reason: 'sam is editing Room.join',
    timestamp: 1,
    decision_id: 'd1',
  };
}

function alert(verdict: ConflictAlert['verdict']): ConflictAlert {
  return {
    type: 'conflict_alert',
    users: ['ada', 'sam'],
    file_path: 'a.py',
    verdict,
    explanation: 'both renamed it',
    timestamp: 1,
  };
}

test('nothing until the room is switched on', () => {
  assert.equal(noticeFor(block('ada', ['sam']), DEFAULT_PREFS, 'k7q2-xmab'), null);
});

test("anyone's blocks notify; warnings never do", () => {
  const notice = noticeFor(block('ada', ['sam']), ON, 'k7q2-xmab');
  assert.ok(notice);
  assert.equal(notice.title, "ada's agent was blocked");
  assert.match(notice.body, /server\/room\.py — held by sam/);
  assert.equal(notice.tag, 'synergo:K7Q2XMAB:d1');
  assert.equal(noticeFor(block('ada', ['sam'], 'warn'), ON, 'K7Q2XMAB'), null);
});

test('only me: blocks I asked for or caused, matched like relay names', () => {
  const me: NotificationPrefs = { ...ON, blocks: 'me', myName: ' Sam ' };
  assert.equal(noticeFor(block('ada', ['priya']), me, 'R'), null);
  assert.ok(noticeFor(block('ada', ['sam']), me, 'R'), 'my edit held someone up');
  const mine = noticeFor(block('sam', ['ada']), me, 'R');
  assert.equal(mine?.title, 'Your agent was blocked');
  assert.equal(noticeFor(block('sam', []), { ...me, myName: '' }, 'R'), null);
  assert.equal(noticeFor(block('sam', []), { ...ON, blocks: 'off' }, 'R'), null);
});

test('conflict alerts: actionable verdicts only, and they can be turned off', () => {
  assert.equal(noticeFor(alert('overlapping'), ON, 'R')?.title, 'Conflict: a.py');
  assert.ok(noticeFor(alert('related'), ON, 'R'));
  assert.equal(noticeFor(alert('unrelated'), ON, 'R'), null);
  assert.equal(noticeFor(alert('overlapping'), { ...ON, conflicts: false }, 'R'), null);
});

test('stored preferences are parsed forgivingly', () => {
  assert.deepEqual(parsePrefs(null), DEFAULT_PREFS);
  assert.deepEqual(parsePrefs('not json'), DEFAULT_PREFS);
  assert.deepEqual(parsePrefs('{"enabled":true,"blocks":"me","myName":"Ada!","conflicts":false}'), {
    enabled: true,
    blocks: 'me',
    myName: 'ada',
    conflicts: false,
  });
  assert.equal(parsePrefs('{"blocks":"everyone"}').blocks, 'anyone');
  assert.equal(relayName(' Priya K '), 'priyak');
});

test('webhook URLs: https only (http to localhost for a local receiver), kind guessed', () => {
  assert.equal(webhookUrlProblem('https://hooks.slack.com/services/T/B/x'), null);
  assert.equal(webhookUrlProblem('http://localhost:9000/hook'), null);
  assert.match(webhookUrlProblem('http://example.com/hook') ?? '', /https/);
  assert.match(webhookUrlProblem('nope') ?? '', /not a URL/);
  assert.equal(guessKind('https://hooks.slack.com/services/T/B/x'), 'slack');
  assert.equal(guessKind('https://discord.com/api/webhooks/1/x'), 'discord');
  assert.equal(guessKind('https://example.com/x'), 'generic');
});

test('close codes: 1013 and 4408 reconnect; 4403 and 4404 stop', async () => {
  const { closeAction } = await import('../../src/closeCodes.ts');
  assert.equal(closeAction(1013), 'reconnect');
  assert.equal(closeAction(4408), 'reconnect');
  assert.equal(closeAction(1006), 'reconnect');
  assert.equal(closeAction(4429), 'reconnect');
  assert.equal(closeAction(4403), 'refused');
  assert.equal(closeAction(4404), 'room_missing');
  assert.equal(closeAction(4004), 'room_missing');
});
