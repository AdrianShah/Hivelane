import assert from 'node:assert/strict';
import test from 'node:test';

import { livenessFromResponse, livenessLabel } from '../../src/dashboard/roomLife.ts';
import { agentPromptFor, projectFileFor, shareLinkFor } from '../../src/links.ts';

const PAGES = { origin: 'https://adrianshah.github.io', base: '/Hivelane', relayWs: 'wss://relay.example/ws', relayIsOrigin: false };

test('shareLinkFor builds the invite page link, with ?relay= unless the relay serves the page', () => {
  assert.equal(
    shareLinkFor('k7q2-xmab', PAGES),
    'https://adrianshah.github.io/Hivelane/j/K7Q2XMAB?relay=wss%3A%2F%2Frelay.example%2Fws',
  );
  assert.equal(
    shareLinkFor('K7Q2XMAB', { origin: 'https://relay.example', base: '', relayWs: 'wss://relay.example/ws', relayIsOrigin: true }),
    'https://relay.example/j/K7Q2XMAB',
  );
});

test('agentPromptFor matches what hivelane up prints (hivelane/links.py agent_join_prompt)', () => {
  const link = shareLinkFor('K7Q2XMAB', PAGES);
  assert.equal(
    agentPromptFor('K7Q2XMAB', link, PAGES.relayWs),
    'Join Hivelane room K7Q2XMAB in this repo: install hivelane if needed ' +
      '(`pipx install git+https://github.com/AdrianShah/Hivelane`), then run ' +
      `\`hivelane up --room "${link}" -y\`.`,
  );
  assert.ok(agentPromptFor('K7Q2XMAB', 'http://x/j/K7Q2XMAB', 'ws://relay.lan:8000/ws').endsWith(' -y --insecure`.'));
});

test('projectFileFor spells out only what differs from the CLI defaults', () => {
  assert.equal(projectFileFor('k7q2xmab', 'ws://localhost:8000/ws'), 'room = "K7Q2XMAB"\n');
  assert.equal(projectFileFor('K7Q2XMAB', 'wss://relay.example/ws'), 'room = "K7Q2XMAB"\nrelay = "wss://relay.example/ws"\n');
  assert.equal(
    projectFileFor('K7Q2XMAB', 'ws://relay.lan:8000/ws'),
    'room = "K7Q2XMAB"\nrelay = "ws://relay.lan:8000/ws"\ninsecure = true\n',
  );
});

test('livenessFromResponse only calls a room gone when the relay says so', () => {
  assert.deepEqual(livenessFromResponse(200, { alive: true, expires_at: 1000 }), { state: 'alive', expiresAt: 1000 });
  assert.deepEqual(livenessFromResponse(404, { detail: 'Room not found', alive: false }), { state: 'gone' });
  // A relay without the route answers a bare 404: not proof the room is gone.
  assert.deepEqual(livenessFromResponse(404, { detail: 'Not Found' }), { state: 'unknown' });
  assert.deepEqual(livenessFromResponse(503, null), { state: 'unknown' });
  assert.deepEqual(livenessFromResponse(200, 'nope'), { state: 'unknown' });
});

test('livenessLabel shows live rooms with their expiry', () => {
  const now = 1_000_000;
  assert.equal(livenessLabel({ state: 'alive', expiresAt: now + 6 * 86400 + 60 }, now), 'Live · expires in 6 days');
  assert.equal(livenessLabel({ state: 'alive', expiresAt: 0 }, now), 'Live');
  assert.equal(livenessLabel({ state: 'unknown' }, now), null);
  assert.equal(livenessLabel(undefined, now), null);
});
