/**
 * Tasks a dashboard adds: its task key's public tag matches the relay's
 * (server/tasks.py dashboard_tag), and in an e2e room the task is sealed the
 * way a daemon seals one (title in a "task" blob, files hashed).
 * Run: npm run test:unit.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { cleanName, dashboardTag } from '../../src/taskKey.ts';
import { E2EDecoder, deriveRoomKey, hashName, openEnvelope } from '../../src/e2e.ts';

test('the tag is the first 16 hex of the key SHA-256, like the relay', async () => {
  // python: hashlib.sha256(b"k" * 32).hexdigest()[:16]
  assert.equal(await dashboardTag('k'.repeat(32)), '5e318f8cf9cbe249');
});

test('names are cleaned like the relay does', () => {
  assert.equal(cleanName(' Adrian Shah! '), 'adrianshah');
  assert.equal(cleanName('x'.repeat(30)).length, 20);
});

test('an e2e task is sealed like a daemon would', async () => {
  const decoder = new E2EDecoder('correct horse battery', 'ROOM');
  assert.equal(decoder.hasKey, true);
  const sealed = await decoder.sealTask({ title: 'Fix login', body: 'soon', files: ['web/login.tsx'] });
  const key = await deriveRoomKey('correct horse battery', 'ROOM');
  assert.deepEqual(sealed.files, [await hashName(key, 'web/login.tsx')]);
  const opened = await openEnvelope(key, sealed.e2e, ['task']);
  assert.equal(opened.type, 'task');
  assert.equal(opened.envelope.title, 'Fix login');
  assert.deepEqual(opened.envelope.files, ['web/login.tsx']);
  // Its own files read back as names, not hashes.
  assert.equal(decoder.name(sealed.files[0]), 'web/login.tsx');
  assert.equal(new E2EDecoder(null, 'ROOM').hasKey, false);
});
