/**
 * End-to-end encrypted rooms, dashboard side (the daemon's twin is
 * synergo/crypto.py; protocol/e2e-test-vectors.json pins both).
 *
 * In a room whose .synergo.toml says `e2e = true` the relay only ever sees
 * keyed hashes (`h:` + 32 hex) where paths, symbols and branches would be,
 * empty diffs and prompts, and an opaque `e2e` blob per message. With the
 * room passphrase this module opens the blobs and puts the real values back:
 *
 *   enc/mac key = HKDF-SHA256(passphrase, salt "synergo-e2e-v1:" + ROOM, info "enc"/"mac")
 *   blob        = base64url(nonce(12) || AES-256-GCM(ciphertext || tag)), AD = message type
 *   hash        = "h:" + first 32 hex of HMAC-SHA256(mac key, name)
 *
 * The passphrase arrives in the URL fragment (`#k=...`, which browsers never
 * send to a server), is stripped from the address bar at once and kept in
 * sessionStorage for this room only, or is pasted by the viewer.
 *
 * Erasable TypeScript with type-only imports, so `node --test` runs the
 * shared vectors against this exact file (npm run test:e2e-crypto).
 */

export const SALT_PREFIX = 'synergo-e2e-v1:';
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

export function normalizeRoom(room: string): string {
  return room.trim().toUpperCase();
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
  const base64 = text.trim().replace(/-/g, '+').replace(/_/g, '/');
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

export interface RoomKey {
  room: string;
  encKey: CryptoKey;
  macKey: CryptoKey;
  /** Raw key bytes, kept for the test vectors; never displayed. */
  encRaw: Uint8Array<ArrayBuffer>;
  macRaw: Uint8Array<ArrayBuffer>;
}

export async function deriveRoomKey(passphrase: string, room: string): Promise<RoomKey> {
  if (!passphrase) throw new DecryptError('an empty passphrase is not a room key');
  const api = subtle();
  const ikm = await api.importKey('raw', encoder.encode(passphrase), 'HKDF', false, ['deriveBits']);
  const salt = encoder.encode(SALT_PREFIX + normalizeRoom(room));
  const derive = async (info: string) =>
    new Uint8Array(
      await api.deriveBits({ name: 'HKDF', hash: 'SHA-256', salt, info: encoder.encode(info) }, ikm, 256),
    );
  const encRaw = await derive('enc');
  const macRaw = await derive('mac');
  return {
    room: normalizeRoom(room),
    encKey: await api.importKey('raw', encRaw, { name: 'AES-GCM' }, false, ['encrypt', 'decrypt']),
    macKey: await api.importKey('raw', macRaw, { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']),
    encRaw,
    macRaw,
  };
}

/** "h:" + 32 hex of HMAC-SHA256(mac key, value); "" stays "". */
export async function hashName(key: RoomKey, value: string): Promise<string> {
  if (!value) return value;
  const mac = new Uint8Array(await subtle().sign('HMAC', key.macKey, encoder.encode(value)));
  return 'h:' + toHex(mac).slice(0, HASH_HEX);
}

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
  return b64urlEncode(out);
}

export async function decryptBlob(key: RoomKey, type: string, blob: string): Promise<string> {
  const raw = b64urlDecode(blob);
  if (raw.length < NONCE_BYTES + 16) throw new DecryptError('blob too short');
  let plain: ArrayBuffer;
  try {
    plain = await subtle().decrypt(
      { name: 'AES-GCM', iv: raw.slice(0, NONCE_BYTES), additionalData: encoder.encode(type) },
      key.encKey,
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
  private opened = false;
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
      if (value && value !== '<imports>') this.names.set(await hashName(key, value), value);
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
      this.opened = true;
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

  get anyOpened(): boolean {
    return this.opened;
  }
}
