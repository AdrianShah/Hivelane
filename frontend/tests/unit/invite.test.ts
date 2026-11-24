import assert from 'node:assert/strict';
import test from 'node:test';

import { agentJoinPrompt, invitePath, joinCommand } from '../../src/relayUrl.ts';

test('invitePath normalizes room codes', () => {
  assert.equal(invitePath('k7q2-xm9p'), '/j/K7Q2XM9P');
  assert.equal(invitePath('  ab12 34cd '), '/j/AB1234CD');
});

test('joinCommand quotes invite URL and appends --insecure only for remote ws://', () => {
  const url = 'https://adrianshah.github.io/Hivelane/j/K7Q2XM?relay=wss%3A%2F%2Frelay.example%2Fws#key=abc';
  assert.equal(
    joinCommand(url, 'wss://relay.example/ws'),
    `hivelane join "${url}"`,
  );
  assert.equal(
    joinCommand('http://127.0.0.1:8000/j/K7Q2XM', 'ws://127.0.0.1:8000/ws'),
    'hivelane join "http://127.0.0.1:8000/j/K7Q2XM"',
  );
  assert.equal(
    joinCommand('https://dash.example/j/K7Q2XM?relay=ws%3A%2F%2Frelay.lan%3A8000%2Fws', 'ws://relay.lan:8000/ws'),
    'hivelane join "https://dash.example/j/K7Q2XM?relay=ws%3A%2F%2Frelay.lan%3A8000%2Fws" --insecure',
  );
});

test('agentJoinPrompt includes the full invite command and install fallback', () => {
  const url = 'https://adrianshah.github.io/Hivelane/j/K7Q2XM#key=secret';
  const prompt = agentJoinPrompt(url, 'ws://localhost:8000/ws');
  assert.ok(prompt.includes(`hivelane join "${url}"`));
  assert.ok(prompt.includes('pipx install git+https://github.com/AdrianShah/Hivelane'));
});
