import assert from 'node:assert/strict';
import test from 'node:test';

import { parseKeyFragment, stripKeyFragment } from '../../src/e2e.ts';
import { buildShareLink, parseInviteFragment, stripInviteFragment } from '../../src/relayUrl.ts';

const origin = 'https://adrianshah.github.io';
const base = '/Hivelane';
const secret = 'Zq8Tn4xK1pLw0aB7cD2eF5gH9jM3rS6u';

test('the share link carries the invite secret in the fragment', () => {
  assert.equal(
    buildShareLink({ origin, base, roomId: 'k7q2-xm9p', relayWs: 'wss://relay.example/ws', invite: secret }),
    `${origin}${base}/j/K7Q2XM9P?relay=wss%3A%2F%2Frelay.example%2Fws#invite=${secret}`,
  );
});

test('the share link combines the e2e key and the invite cleanly', () => {
  const link = buildShareLink({ origin, base: '', roomId: 'K7Q2XM9P', e2eKey: 'pass phrase&x', invite: secret });
  assert.equal(link, `${origin}/j/K7Q2XM9P#key=pass%20phrase%26x&invite=${secret}`);
  const hash = link.slice(link.indexOf('#'));
  assert.equal(parseKeyFragment(hash), 'pass phrase&x');
  assert.equal(parseInviteFragment(hash), secret);
});

test('no secrets, no fragment; blank ones are dropped', () => {
  assert.equal(buildShareLink({ origin, base, roomId: 'K7Q2XM9P' }), `${origin}${base}/j/K7Q2XM9P`);
  assert.equal(buildShareLink({ origin, base, roomId: 'K7Q2XM9P', invite: '  ', e2eKey: null }), `${origin}${base}/j/K7Q2XM9P`);
});

test('parseInviteFragment finds only invite=', () => {
  assert.equal(parseInviteFragment(`#invite=${secret}`), secret);
  assert.equal(parseInviteFragment(`#k=abc&invite=${secret}`), secret);
  assert.equal(parseInviteFragment('#k=abc'), null);
  assert.equal(parseInviteFragment('#abc'), null);
  assert.equal(parseInviteFragment('#invite='), null);
  assert.equal(parseInviteFragment(''), null);
});

test('stripping one secret keeps the other', () => {
  assert.equal(stripInviteFragment(`#k=abc&invite=${secret}`), '#k=abc');
  assert.equal(stripInviteFragment(`#invite=${secret}`), '');
  assert.equal(stripKeyFragment(`#k=abc&invite=${secret}`), `#invite=${secret}`);
  // An invite alone is not mistaken for an e2e key.
  assert.equal(parseKeyFragment(`#invite=${secret}`), null);
});
