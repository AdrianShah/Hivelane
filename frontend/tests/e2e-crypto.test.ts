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
  hashNames,
  normalizeRoom,
  openEnvelope,
  padPlaintext,
  parseKeyFragment,
  repoToken,
  sealEnvelope,
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

interface VectorV2 extends Vector {
  canonical_room: string;
  salt: string;
  pbkdf2_iterations: number;
  stretched_hex: string;
  encryptions: { type: string; plaintext: string; padded_length: number; nonce_hex: string; blob: string }[];
  repo_tokens: { repo_id: string; token: string }[];
}

const VECTORS = JSON.parse(
  readFileSync(new URL('../../protocol/e2e-test-vectors.json', import.meta.url), 'utf8'),
) as {
  vectors: Vector[];
  wrong_key: { passphrase: string };
  vectors_v2: VectorV2[];
  wrong_key_v2: { passphrase: string; room: string; blob_from_vector: number; encryption: number };
};

test('v2: keys, padded blobs, hashes and repo tokens match the shared vectors', async () => {
  assert.ok(VECTORS.vectors_v2.length > 0);
  for (const vector of VECTORS.vectors_v2) {
    assert.equal(normalizeRoom(vector.room), vector.canonical_room);
    const key = await deriveRoomKey(vector.passphrase, vector.room);
    assert.equal(key.version, 2);
    assert.equal(toHex(key.stretchedRaw!), vector.stretched_hex);
    assert.equal(toHex(key.encRaw), vector.enc_key_hex);
    assert.equal(toHex(key.macRaw), vector.mac_key_hex);
    for (const c of vector.encryptions) {
      const padded = padPlaintext(c.plaintext);
      assert.equal(new TextEncoder().encode(padded).length, c.padded_length);
      assert.equal(await encryptBlob(key, c.type, padded, fromHex(c.nonce_hex)), c.blob);
      assert.equal(await decryptBlob(key, c.type, c.blob), padded);
      const opened = await openEnvelope(key, c.blob);
      assert.equal(opened.type, c.type);
      assert.deepEqual(opened.envelope, JSON.parse(c.plaintext));
    }
    for (const h of vector.hashes) assert.equal(await hashName(key, h.value), h.token);
    for (const r of vector.repo_tokens) assert.equal(await repoToken(key, r.repo_id), r.token);
  }
});

test('v2: a wrong passphrase does not open a v2 blob', async () => {
  const w = VECTORS.wrong_key_v2;
  const blob = VECTORS.vectors_v2[w.blob_from_vector].encryptions[w.encryption];
  const key = await deriveRoomKey(w.passphrase, w.room);
  await assert.rejects(decryptBlob(key, blob.type, blob.blob), /decrypt/);
});

test('v2 keys still read v1 blobs and learn v1-hashed names', async () => {
  const vector = VECTORS.vectors[0];
  const key = await deriveRoomKey(vector.passphrase, vector.room);
  for (const c of vector.encryptions) {
    assert.equal(await decryptBlob(key, c.type, c.blob), c.plaintext);
  }
  const tokens = await hashNames(key, vector.hashes[0].value);
  assert.ok(tokens.includes(vector.hashes[0].token));
  // A v1-only key refuses a v2 blob instead of misreading it.
  const v1 = await deriveRoomKey(vector.passphrase, vector.room, 1);
  const v2blob = VECTORS.vectors_v2[0].encryptions[0];
  await assert.rejects(decryptBlob(v1, v2blob.type, v2blob.blob), /v2/);
});

test('sealEnvelope pads to a bucket and round-trips', async () => {
  const key = await deriveRoomKey('correct horse battery staple', 'K7Q2XM');
  const blob = await sealEnvelope(key, 'intent', { v: 1, prompt: 'x'.repeat(300) });
  assert.ok(blob.startsWith('2.'));
  const opened = await openEnvelope(key, blob);
  assert.equal((opened.envelope as { prompt: string }).prompt.length, 300);
  assert.equal(padPlaintext('a'.repeat(257)).length, 512);
  assert.equal(padPlaintext('a'.repeat(20000)).length, 32768);
});

test('v1 (legacy): keys, blobs and hashes match the shared vectors', async () => {
  for (const vector of VECTORS.vectors) {
    const key = await deriveRoomKey(vector.passphrase, vector.room, 1);
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

test('room codes normalise like the relay; the type is authenticated', async () => {
  assert.equal(normalizeRoom(' k7q2-xm9p '), 'K7Q2XM9P');
  assert.equal(normalizeRoom('k7 q2 xm'), 'K7Q2XM');
  const a = await deriveRoomKey('pw', 'k7q2xm');
  const b = await deriveRoomKey('pw', ' K7Q2-XM ');
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

test('the decoder reads a v2 activity frame end to end', async () => {
  const vector = VECTORS.vectors_v2[1];
  const key = await deriveRoomKey(vector.passphrase, vector.room);
  const decoder = new E2EDecoder(vector.passphrase, 'k7q2 xm9p');
  const out = await decoder.decode(activityFrame(vector, await hashName(key, 'server/room.py')));
  assert.equal(out.changes[0].file_path, 'server/room.py');
  assert.equal(out.prompt, 'fix the login bug');
  assert.equal(out.branch, 'main');
});
