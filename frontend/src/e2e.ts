/**
 * End-to-end encrypted rooms, dashboard side (the daemon's twin is
 * synergo/crypto.py; protocol/e2e-test-vectors.json pins both).
 *
 * In a room whose .synergo.toml says `e2e = true` the relay only ever sees
 * keyed hashes (`h:` + 32 hex) where paths, symbols and branches would be,
 * empty diffs and prompts, and an opaque `e2e` blob per message. With the
 * room passphrase this module opens the blobs and puts the real values back:
 *
 * Version 2 (what current daemons write):
 *   ROOM        = room code with every whitespace and "-" removed, upper case
 *                 (the relay's normalize_room_code)
 *   salt        = "synergo-e2e-v2:" + ROOM
 *   stretched   = PBKDF2-HMAC-SHA256(NFC(passphrase), salt, 600000, 32 bytes)
 *   enc/mac key = HKDF-SHA256(stretched, salt, info "enc"/"mac")
 *   blob        = "2." + base64url(nonce(12) || AES-256-GCM(ciphertext || tag)),
 *                 AD = message type, plaintext padded with spaces to a size bucket
 *   hash        = "h:" + first 32 hex of HMAC-SHA256(mac key, name)
 *
 * Version 1 (read only, so stored blobs stay readable): ikm = the raw
 * passphrase (no PBKDF2, no NFC), salt "synergo-e2e-v1:" + code.trim().upper(),
 * no "2." prefix. A v2 key carries the v1 keys of the same passphrase, so
 * a v1 blob opens and names hashed by a v1 daemon are learned too.
 *
 * The passphrase arrives in the URL fragment (`#k=...`, which browsers never
 * send to a server), is stripped from the address bar at once and kept in
 * sessionStorage for this room only, or is pasted by the viewer.
 *
 * Erasable TypeScript with type-only imports, so `node --test` runs the
 * shared vectors against this exact file (npm run test:e2e-crypto).
 */

export const SALT_PREFIX_V1 = 'synergo-e2e-v1:';
export const SALT_PREFIX = 'synergo-e2e-v2:';
export const PBKDF2_ITERATIONS = 600_000;
export const BLOB_V2_PREFIX = '2.';
const PAD_MIN = 256;
const PAD_LINEAR_STEP = 16 * 1024;
export const ENVELOPE_TYPES = ['activity', 'intent', 'plan', 'edit_check'] as const;
export type EnvelopeType = (typeof ENVELOPE_TYPES)[number];

const NONCE_BYTES = 12;
const HASH_HEX = 32;
const HASH_TOKEN = /h:[0-9a-f]{32}/g;
const STORAGE_PREFIX = 'synergo:e2e:';

const encoder = new TextEncoder();
const decoder = new TextDecoder('utf-8', { fatal: true });

/** The browser refuses WebCrypto outside a secure context (plain-http LAN pages). */
export class E2EUnsupportedError extends Error {
  constructor() {
    super(
      'This browser only allows the decryption API on https:// or localhost pages; open the dashboard over https to read an encrypted room.',
    );
    this.name = 'E2EUnsupportedError';
  }
}

export class DecryptError extends Error {
  constructor(message = "can't decrypt with this room key") {
    super(message);
    this.name = 'DecryptError';
  }
}

function subtle(): SubtleCrypto {
  const api = globalThis.crypto?.subtle;
  if (!api) throw new E2EUnsupportedError();
  return api;
}

/** The canonical room code keys are derived from: identical to the relay's
 *  normalize_room_code and synergo/crypto.py normalize_room (every run of
 *  whitespace and "-" removed, then upper case). */
export function normalizeRoom(room: string): string {
  return (room ?? '').replace(/[\s-]+/g, '').toUpperCase();
}

/** v1's room rule (trim and upper case only), kept to open v1 blobs. */
export function legacyRoom(room: string): string {
  return (room ?? '').trim().toUpperCase();
}

/** Trailing spaces up to the next size bucket (as synergo/crypto.py
 *  pad_plaintext): powers of two from 256 bytes to 16 KiB, then multiples
 *  of 16 KiB. Measured in UTF-8 bytes; JSON parsers ignore the spaces. */
export function padPlaintext(text: string): string {
  const size = encoder.encode(text).length;
  let target: number;
  if (size <= PAD_MIN) target = PAD_MIN;
  else if (size <= PAD_LINEAR_STEP) target = 2 ** Math.ceil(Math.log2(size));
  else target = Math.ceil(size / PAD_LINEAR_STEP) * PAD_LINEAR_STEP;
  return text + ' '.repeat(target - size);
}

export function isHashed(value: unknown): value is string {
  return typeof value === 'string' && value.length === 2 + HASH_HEX && value.startsWith('h:');
}

// --- encoding -----------------------------------------------------------------

export function b64urlEncode(bytes: Uint8Array): string {
  let binary = '';
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

export function b64urlDecode(text: string): Uint8Array<ArrayBuffer> {
  const body = text.trim();
  // Strict, like synergo/crypto.py b64url_decode: atob() would accept '+', '/' and padding.
  if (!/^[A-Za-z0-9_-]*$/.test(body) || body.length % 4 === 1) throw new DecryptError('not a base64url blob');
  const base64 = body.replace(/-/g, '+').replace(/_/g, '/');
  const padded = base64 + '='.repeat((4 - (base64.length % 4)) % 4);
  let binary: string;
  try {
    binary = atob(padded);
  } catch {
    throw new DecryptError('not a base64url blob');
  }
  const out = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) out[i] = binary.charCodeAt(i);
  return out;
}

export function toHex(bytes: Uint8Array): string {
  return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
}

export function fromHex(hex: string): Uint8Array<ArrayBuffer> {
  const out = new Uint8Array(hex.length / 2);
  for (let i = 0; i < out.length; i += 1) out[i] = parseInt(hex.slice(i * 2, i * 2 + 2), 16);
  return out;
}

// --- keys, blobs, hashes ---------------------------------------------------------

export type KeyVersion = 1 | 2;

export interface RoomKey {
  room: string;
  version: KeyVersion;
  encKey: CryptoKey;
  macKey: CryptoKey;
  /** Raw key bytes, kept for the test vectors; never displayed. */
  encRaw: Uint8Array<ArrayBuffer>;
  macRaw: Uint8Array<ArrayBuffer>;
  /** PBKDF2 output (v2 only), kept for the test vectors. */
  stretchedRaw?: Uint8Array<ArrayBuffer>;
  /** The v1 keys of the same passphrase and room (on a v2 key): open
   *  pre-v2 blobs and learn names a v1 daemon hashed. */
  legacy?: { encKey: CryptoKey; macKey: CryptoKey };
}

async function hkdfPair(
  ikm: Uint8Array<ArrayBuffer>,
  salt: Uint8Array<ArrayBuffer>,
): Promise<{ encRaw: Uint8Array<ArrayBuffer>; macRaw: Uint8Array<ArrayBuffer>; encKey: CryptoKey; macKey: CryptoKey }> {
  const api = subtle();
  const base = await api.importKey('raw', ikm, 'HKDF', false, ['deriveBits']);
  const derive = async (info: string) =>
    new Uint8Array(
      await api.deriveBits({ name: 'HKDF', hash: 'SHA-256', salt, info: encoder.encode(info) }, base, 256),
    );
  const encRaw = await derive('enc');
  const macRaw = await derive('mac');
  return {
    encRaw,
    macRaw,
    encKey: await api.importKey('raw', encRaw, { name: 'AES-GCM' }, false, ['encrypt', 'decrypt']),
    macKey: await api.importKey('raw', macRaw, { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']),
  };
}

/** The PBKDF2 step of v2 (exported for the test vectors). */
export async function stretchV2(passphrase: string, room: string): Promise<Uint8Array<ArrayBuffer>> {
  const api = subtle();
  const pw = await api.importKey('raw', encoder.encode(passphrase.normalize('NFC')), 'PBKDF2', false, [
    'deriveBits',
  ]);
  const salt = encoder.encode(SALT_PREFIX + normalizeRoom(room));
  return new Uint8Array(
    await api.deriveBits({ name: 'PBKDF2', hash: 'SHA-256', salt, iterations: PBKDF2_ITERATIONS }, pw, 256),
  );
}

/** The room key: v2 (with the v1 keys alongside) unless asked otherwise.
 *  PBKDF2 at 600k iterations takes a few hundred ms; derive it once per room. */
export async function deriveRoomKey(passphrase: string, room: string, version: KeyVersion = 2): Promise<RoomKey> {
  if (!passphrase) throw new DecryptError('an empty passphrase is not a room key');
  const v1 = await hkdfPair(encoder.encode(passphrase), encoder.encode(SALT_PREFIX_V1 + legacyRoom(room)));
  if (version === 1) {
    return { room: normalizeRoom(room), version: 1, encKey: v1.encKey, macKey: v1.macKey, encRaw: v1.encRaw, macRaw: v1.macRaw };
  }
  const stretchedRaw = await stretchV2(passphrase, room);
  const v2 = await hkdfPair(stretchedRaw, encoder.encode(SALT_PREFIX + normalizeRoom(room)));
  return {
    room: normalizeRoom(room),
    version: 2,
    encKey: v2.encKey,
    macKey: v2.macKey,
    encRaw: v2.encRaw,
    macRaw: v2.macRaw,
    stretchedRaw,
    legacy: { encKey: v1.encKey, macKey: v1.macKey },
  };
}

async function hmacToken(macKey: CryptoKey, value: string): Promise<string> {
  const mac = new Uint8Array(await subtle().sign('HMAC', macKey, encoder.encode(value)));
  return 'h:' + toHex(mac).slice(0, HASH_HEX);
}

/** "h:" + 32 hex of HMAC-SHA256(mac key, value); "" stays "". */
export async function hashName(key: RoomKey, value: string): Promise<string> {
  if (!value) return value;
  return hmacToken(key.macKey, value);
}

/** Every token `value` may appear as on the relay: the v2 hash and, on a v2
 *  key, the v1 hash a not-yet-upgraded daemon would send. */
export async function hashNames(key: RoomKey, value: string): Promise<string[]> {
  if (!value) return [];
  const tokens = [await hmacToken(key.macKey, value)];
  if (key.legacy) tokens.push(await hmacToken(key.legacy.macKey, value));
  return tokens;
}

/** What a daemon sends as `repo_id` in an e2e room; "" stays "". */
export async function repoToken(key: RoomKey, repoId: string): Promise<string> {
  return repoId ? hashName(key, 'repo:' + repoId) : repoId;
}

/** [`2.`] base64url(nonce || ciphertext || tag), `type` as associated data.
 *  No padding here (sealEnvelope pads); `nonce` is for test vectors only. */
export async function encryptBlob(
  key: RoomKey,
  type: string,
  plaintext: string,
  nonce?: Uint8Array<ArrayBuffer>,
): Promise<string> {
  const iv = nonce ?? globalThis.crypto.getRandomValues(new Uint8Array(NONCE_BYTES));
  const sealed = new Uint8Array(
    await subtle().encrypt(
      { name: 'AES-GCM', iv, additionalData: encoder.encode(type) },
      key.encKey,
      encoder.encode(plaintext),
    ),
  );
  const out = new Uint8Array(iv.length + sealed.length);
  out.set(iv, 0);
  out.set(sealed, iv.length);
  return (key.version >= 2 ? BLOB_V2_PREFIX : '') + b64urlEncode(out);
}

/** encryptBlob() of an envelope's compact JSON, padded (v2) to a size bucket. */
export async function sealEnvelope(key: RoomKey, type: string, envelope: Envelope): Promise<string> {
  const text = JSON.stringify(envelope);
  return encryptBlob(key, type, key.version >= 2 ? padPlaintext(text) : text);
}

export async function decryptBlob(key: RoomKey, type: string, blob: string): Promise<string> {
  if (typeof blob !== 'string') throw new DecryptError('not a blob');
  let body = blob.trim();
  let encKey = key.encKey;
  if (body.startsWith(BLOB_V2_PREFIX)) {
    if (key.version < 2) throw new DecryptError('a v2 blob needs a v2 key');
    body = body.slice(BLOB_V2_PREFIX.length);
  } else if (key.version >= 2) {
    // A v1 blob (stored before the upgrade) under a v2 key.
    if (!key.legacy) throw new DecryptError('a v1 blob and no v1 key to open it');
    encKey = key.legacy.encKey;
  }
  const raw = b64urlDecode(body);
  if (raw.length < NONCE_BYTES + 16) throw new DecryptError('blob too short');
  let plain: ArrayBuffer;
  try {
    plain = await subtle().decrypt(
      { name: 'AES-GCM', iv: raw.slice(0, NONCE_BYTES), additionalData: encoder.encode(type) },
      encKey,
      raw.slice(NONCE_BYTES),
    );
  } catch (err) {
    if (err instanceof E2EUnsupportedError) throw err;
    throw new DecryptError();
  }
  try {
    return decoder.decode(plain);
  } catch {
    throw new DecryptError('envelope is not UTF-8');
  }
}

export type Envelope = Record<string, unknown>;

/** The envelope inside a blob made for any of `types` (the relay copies a
 *  teammate's activity or plan blob onto derived records, so the reader does
 *  not always know which type it was made for; GCM authenticates it). */
export async function openEnvelope(
  key: RoomKey,
  blob: string,
  types: readonly string[] = ENVELOPE_TYPES,
): Promise<{ type: string; envelope: Envelope }> {
  let last: unknown = null;
  for (const type of types) {
    try {
      const text = await decryptBlob(key, type, blob);
      const envelope = JSON.parse(text) as unknown;
      if (!envelope || typeof envelope !== 'object' || Array.isArray(envelope)) {
        throw new DecryptError('envelope is not an object');
      }
      return { type, envelope: envelope as Envelope };
    } catch (err) {
      if (err instanceof E2EUnsupportedError) throw err;
      last = err;
    }
  }
  throw last instanceof DecryptError ? last : new DecryptError();
}

// --- the passphrase in the browser ---------------------------------------------------

/** `k` from a URL fragment like `#k=abc` or `#view=x&k=abc`, else null. */
export function parseKeyFragment(hash: string): string | null {
  const body = hash.startsWith('#') ? hash.slice(1) : hash;
  if (!body) return null;
  for (const part of body.split('&')) {
    const [name, ...rest] = part.split('=');
    if (name === 'k' && rest.length) {
      try {
        const value = decodeURIComponent(rest.join('='));
        return value.trim() || null;
      } catch {
        return null;
      }
    }
  }
  return null;
}

/** The fragment with `k` removed ("" when nothing else was there). */
export function stripKeyFragment(hash: string): string {
  const body = hash.startsWith('#') ? hash.slice(1) : hash;
  const kept = body.split('&').filter((part) => part && !part.startsWith('k='));
  return kept.length ? `#${kept.join('&')}` : '';
}

/**
 * The room key for this page: from `#k=` (then stripped from the address bar
 * and history, and kept in sessionStorage for this room) or a key already
 * kept this session. Storage failures (private mode, blocked storage) only
 * mean the key is not remembered.
 */
export function takeRoomKey(room: string): string | null {
  const id = STORAGE_PREFIX + normalizeRoom(room);
  if (typeof window !== 'undefined') {
    const fromUrl = parseKeyFragment(window.location.hash);
    if (fromUrl) {
      try {
        const url = window.location.pathname + window.location.search + stripKeyFragment(window.location.hash);
        window.history.replaceState(window.history.state, '', url);
      } catch {
        /* the key stays in the address bar; nothing else to do */
      }
      rememberRoomKey(room, fromUrl);
      return fromUrl;
    }
  }
  try {
    return globalThis.sessionStorage?.getItem(id) || null;
  } catch {
    return null;
  }
}

export function rememberRoomKey(room: string, passphrase: string): void {
  try {
    globalThis.sessionStorage?.setItem(STORAGE_PREFIX + normalizeRoom(room), passphrase);
  } catch {
    /* not remembered across reloads; still used for this page */
  }
}

export function forgetRoomKey(room: string): void {
  try {
    globalThis.sessionStorage?.removeItem(STORAGE_PREFIX + normalizeRoom(room));
  } catch {
    /* nothing stored */
  }
}

// --- overlaying relay frames ------------------------------------------------------------

/**
 * off: nothing encrypted seen; locked: encrypted, no key; ok: blobs open;
 * bad-key: a key was given and no blob opens with it; unsupported: no
 * WebCrypto on this page.
 */
export type E2EState = 'off' | 'locked' | 'ok' | 'bad-key' | 'unsupported';

export const LOCKED_TEXT = '🔒 encrypted — paste the room key to view';
export const UNREADABLE_TEXT = "can't decrypt (wrong room key?)";

type Frame = Record<string, unknown> & { type?: string };

function asStrings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((v): v is string => typeof v === 'string') : [];
}

/**
 * Turns relay frames from an e2e room back into readable ones. Without a
 * key (or with the wrong one) the hashes become short, distinct placeholders
 * ("🔒 3f9a1c"), never raw ciphertext or garbage.
 */
export class E2EDecoder {
  private readonly key: Promise<RoomKey> | null;
  private readonly names = new Map<string, string>();
  private readonly report: (state: E2EState) => void;
  private state: E2EState = 'off';

  constructor(passphrase: string | null, room: string, onState: (state: E2EState) => void = () => {}) {
    this.report = onState;
    this.key = passphrase ? deriveRoomKey(passphrase, room) : null;
    // Unhandled until first use otherwise; failures surface through decode().
    this.key?.catch(() => {});
  }

  private setState(next: E2EState): void {
    if (next === this.state) return;
    // ok sticks once any blob opened: one teammate on a stale key must not
    // flip the whole dashboard to "wrong key".
    if (this.state === 'ok' && next === 'bad-key') return;
    this.state = next;
    this.report(next);
  }

  /** The real name for an h-token, or a short placeholder. */
  name(value: string): string {
    if (!isHashed(value)) return value;
    const known = this.names.get(value);
    if (known !== undefined) return known;
    const tag = value.slice(2, 8);
    return this.state === 'bad-key' ? `can't decrypt·${tag}` : `🔒 ${tag}`;
  }

  /** Every h-token in free text (relay reasons, conflict explanations) replaced. */
  text(value: string): string {
    return value.includes('h:') ? value.replace(HASH_TOKEN, (token) => this.name(token)) : value;
  }

  private async learn(key: RoomKey, envelope: Envelope): Promise<void> {
    const found: string[] = [];
    for (const change of Array.isArray(envelope.changes) ? envelope.changes : []) {
      if (change && typeof change === 'object') {
        const c = change as Record<string, unknown>;
        if (typeof c.file_path === 'string') found.push(c.file_path);
        found.push(...asStrings(c.symbols));
      }
    }
    if (typeof envelope.file_path === 'string') found.push(envelope.file_path);
    if (typeof envelope.branch === 'string') found.push(envelope.branch);
    found.push(...asStrings(envelope.files), ...asStrings(envelope.symbols));
    for (const value of found) {
      if (!value || value === '<imports>') continue;
      for (const token of await hashNames(key, value)) this.names.set(token, value);
    }
  }

  private async open(blob: unknown): Promise<Envelope | null> {
    if (typeof blob !== 'string' || !blob) return null;
    if (!this.key) {
      this.setState('locked');
      return null;
    }
    let key: RoomKey;
    try {
      key = await this.key;
    } catch (err) {
      this.setState(err instanceof E2EUnsupportedError ? 'unsupported' : 'bad-key');
      return null;
    }
    try {
      const { envelope } = await openEnvelope(key, blob);
      await this.learn(key, envelope);
      this.setState('ok');
      return envelope;
    } catch (err) {
      this.setState(err instanceof E2EUnsupportedError ? 'unsupported' : 'bad-key');
      return null;
    }
  }

  /** The frame with its blob opened and every hash shown as a real name (or a placeholder). */
  async decode<T>(item: T): Promise<T> {
    const frame = item as unknown as Frame;
    if (!frame || typeof frame !== 'object') return item;
    const blob = frame.e2e;
    const encrypted = typeof blob === 'string' && blob.length > 0;
    if (!encrypted && !JSON.stringify(frame).includes('"h:')) return item;
    const envelope = await this.open(blob);
    const locked = encrypted && envelope === null;
    const hidden = this.key ? UNREADABLE_TEXT : LOCKED_TEXT;
    const out: Frame = { ...frame };

    switch (frame.type) {
      case 'activity_update': {
        const real = new Map<string, Record<string, unknown>>();
        for (const change of Array.isArray(envelope?.changes) ? envelope.changes : []) {
          if (change && typeof change === 'object' && typeof (change as Record<string, unknown>).file_path === 'string') {
            const c = change as Record<string, unknown>;
            real.set(c.file_path as string, c);
          }
        }
        out.changes = (Array.isArray(frame.changes) ? frame.changes : []).map((change) => {
          const c = change as Record<string, unknown>;
          const path = this.name(String(c.file_path ?? ''));
          const source = real.get(path);
          return {
            ...c,
            file_path: path,
            diff: typeof source?.diff === 'string' ? source.diff : c.diff,
            symbols: asStrings(source?.symbols ?? c.symbols).map((s) => this.name(s)),
          };
        });
        if (envelope) out.prompt = typeof envelope.prompt === 'string' ? envelope.prompt : null;
        else if (locked) out.prompt = hidden;
        out.branch = envelope && typeof envelope.branch === 'string' ? envelope.branch : this.name(String(frame.branch ?? ''));
        break;
      }
      case 'intent_update':
        if (envelope) out.prompt = typeof envelope.prompt === 'string' ? envelope.prompt : '';
        else if (locked) out.prompt = hidden;
        break;
      case 'plan_update':
        if (envelope) {
          out.summary = typeof envelope.summary === 'string' ? envelope.summary : '';
          out.files = asStrings(envelope.files);
          out.symbols = asStrings(envelope.symbols);
          out.branch = typeof envelope.branch === 'string' ? envelope.branch : '';
        } else {
          if (locked) out.summary = hidden;
          out.files = asStrings(frame.files).map((f) => this.name(f));
          out.symbols = asStrings(frame.symbols).map((s) => this.name(s));
          out.branch = this.name(String(frame.branch ?? ''));
        }
        break;
      case 'edit_decision':
        out.file_path =
          envelope && typeof envelope.file_path === 'string'
            ? envelope.file_path
            : this.name(String(frame.file_path ?? ''));
        if (typeof frame.reason === 'string') out.reason = this.text(frame.reason);
        break;
      case 'conflict_alert':
        out.file_path = this.name(String(frame.file_path ?? ''));
        if (typeof frame.explanation === 'string') out.explanation = this.text(frame.explanation);
        break;
      default:
        break;
    }
    return out as unknown as T;
  }
}
