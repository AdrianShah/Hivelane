/**
 * The dashboard's e2e crypto against the vectors synergo/tests/test_crypto.py
 * checks the daemon with: the same passphrase must give the same keys, blobs
 * and hashes in WebCrypto as in Python, or a team's dashboard reads nothing.
 *
 * Run: npm run test:e2e-crypto (node --test with WebCrypto from
 * globalThis.crypto.subtle; Node strips the types).
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import {
  E2EDecoder,
  LOCKED_TEXT,
  UNREADABLE_TEXT,
  decryptBlob,
  deriveRoomKey,
  encryptBlob,
  fromHex,
  hashName,
  openEnvelope,
  parseKeyFragment,
  stripKeyFragment,
  toHex,
  type E2EState,
} from '../src/e2e.ts';

interface Vector {
  passphrase: string;
  room: string;
  enc_key_hex: string;
  mac_key_hex: string;
  encryptions: { type: string; plaintext: string; nonce_hex: string; blob: string }[];
  hashes: { value: string; token: string }[];
}

const VECTORS = JSON.parse(
  readFileSync(new URL('../../protocol/e2e-test-vectors.json', import.meta.url), 'utf8'),
) as { vectors: Vector[]; wrong_key: { passphrase: string } };

test('keys, blobs and hashes match the shared vectors', async () => {
  for (const vector of VECTORS.vectors) {
    const key = await deriveRoomKey(vector.passphrase, vector.room);
    assert.equal(toHex(key.encRaw), vector.enc_key_hex);
    assert.equal(toHex(key.macRaw), vector.mac_key_hex);
    for (const c of vector.encryptions) {
      assert.equal(await encryptBlob(key, c.type, c.plaintext, fromHex(c.nonce_hex)), c.blob);
      assert.equal(await decryptBlob(key, c.type, c.blob), c.plaintext);
      const opened = await openEnvelope(key, c.blob);
      assert.equal(opened.type, c.type);
      assert.deepEqual(opened.envelope, JSON.parse(c.plaintext));
    }
    for (const h of vector.hashes) {
      assert.equal(await hashName(key, h.value), h.token);
    }
  }
});

test('room codes are case-insensitive; the type is authenticated', async () => {
  const a = await deriveRoomKey('pw', 'k7q2xm');
  const b = await deriveRoomKey('pw', ' K7Q2XM ');
  assert.equal(toHex(a.encRaw), toHex(b.encRaw));
  const blob = await encryptBlob(a, 'activity', '{"v":1}');
  await assert.rejects(decryptBlob(a, 'intent', blob), /decrypt/);
  assert.notEqual(await encryptBlob(a, 'activity', '{"v":1}'), blob); // random nonce
});

function activityFrame(vector: Vector, pathToken: string) {
  return {
    type: 'activity_update',
    user_id: 'sam',
    timestamp: 1,
    prompt: null,
    changes: [{ file_path: pathToken, diff: '', symbols: [] }],
    agent: 'cursor',
    branch: '',
    e2e: vector.encryptions[0].blob,
  };
}

test('the right key overlays real paths, diffs and prompts', async () => {
  const vector = VECTORS.vectors[0];
  const key = await deriveRoomKey(vector.passphrase, vector.room);
  const states: E2EState[] = [];
  const decoder = new E2EDecoder(vector.passphrase, vector.room, (s) => states.push(s));
  const frame = activityFrame(vector, await hashName(key, 'server/room.py'));
  const out = await decoder.decode(frame);
  assert.equal(out.changes[0].file_path, 'server/room.py');
  assert.equal(out.changes[0].diff, '-a\n+b');
  assert.deepEqual(out.changes[0].symbols, ['Room.join']);
  assert.equal(out.prompt, 'fix the login bug');
  assert.equal(out.branch, 'main');
  assert.deepEqual(states, ['ok']);
  // Names learned from one blob read relay text elsewhere.
  const alert = await decoder.decode({
    type: 'conflict_alert',
    users: ['sam', 'ada'],
    file_path: await hashName(key, 'server/room.py'),
    verdict: 'overlapping',
    explanation: `both edited ${await hashName(key, 'server/room.py')}`,
    timestamp: 1,
  });
  assert.equal(alert.file_path, 'server/room.py');
  assert.equal(alert.explanation, 'both edited server/room.py');
});

test("a wrong key shows can't decrypt, never garbage", async () => {
  const vector = VECTORS.vectors[0];
  const key = await deriveRoomKey(vector.passphrase, vector.room);
  const states: E2EState[] = [];
  const decoder = new E2EDecoder(VECTORS.wrong_key.passphrase, vector.room, (s) => states.push(s));
  const token = await hashName(key, 'server/room.py');
  const out = await decoder.decode(activityFrame(vector, token));
  assert.equal(out.prompt, UNREADABLE_TEXT);
  assert.equal(out.changes[0].file_path, `can't decrypt·${token.slice(2, 8)}`);
  assert.equal(out.changes[0].diff, '');
  assert.deepEqual(states, ['bad-key']);
});

test('no key: locked placeholders and a locked state', async () => {
  const vector = VECTORS.vectors[0];
  const key = await deriveRoomKey(vector.passphrase, vector.room);
  const states: E2EState[] = [];
  const decoder = new E2EDecoder(null, vector.room, (s) => states.push(s));
  const token = await hashName(key, 'server/room.py');
  const out = await decoder.decode(activityFrame(vector, token));
  assert.equal(out.prompt, LOCKED_TEXT);
  assert.equal(out.changes[0].file_path, `🔒 ${token.slice(2, 8)}`);
  assert.deepEqual(states, ['locked']);
  // Cleartext rooms pass through untouched.
  const plain = { type: 'intent_update', user_id: 'x', prompt: 'hello', timestamp: 1 };
  assert.equal(await decoder.decode(plain), plain);
});

test('the key travels in the fragment and is stripped from it', () => {
  assert.equal(parseKeyFragment('#k=abc-123_x'), 'abc-123_x');
  assert.equal(parseKeyFragment('#view=1&k=a%2Bb'), 'a+b');
  assert.equal(parseKeyFragment('#view=1'), null);
  assert.equal(parseKeyFragment(''), null);
  assert.equal(stripKeyFragment('#k=abc'), '');
  assert.equal(stripKeyFragment('#view=1&k=abc'), '#view=1');
});
