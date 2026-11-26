/**
 * This browser's dashboard task key and the name it adds tasks under.
 *
 * A dashboard that adds a task sends a random secret with it
 * (`dashboard_key`); the relay keeps only its hash and lets a later cancel
 * through only with the same secret. The task itself carries the public tag
 * (first 16 hex of the key's SHA-256), so this dashboard can tell which tasks
 * it may cancel. One key per browser profile, kept in localStorage: clearing
 * site data gives up the ability to cancel those tasks (a member can still
 * claim and cancel them).
 */

const KEY_STORAGE = 'hivelane:task-key';
const NAME_STORAGE = 'hivelane:task-name';

function randomKey(): string {
  const bytes = new Uint8Array(24);
  globalThis.crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
}

function read(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function write(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch {
    // Private mode or blocked storage: this tab still works, the key does not persist.
  }
}

let memoryKey: string | null = null;

/** This browser's task key, created on first use. */
export function dashboardTaskKey(): string {
  const stored = read(KEY_STORAGE);
  if (stored && /^[0-9a-f]{32,128}$/.test(stored)) return stored;
  memoryKey ??= randomKey();
  write(KEY_STORAGE, memoryKey);
  return memoryKey;
}

/** The relay's dashboard_tag for a key (server/tasks.py dashboard_tag);
 *  null where WebCrypto is unavailable (plain-http pages). */
export async function dashboardTag(key: string): Promise<string | null> {
  const subtle = globalThis.crypto?.subtle;
  if (!subtle) return null;
  const digest = await subtle.digest('SHA-256', new TextEncoder().encode(key));
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, '0'))
    .join('')
    .slice(0, 16);
}

/** The name this viewer adds tasks under, as last typed. */
export function savedTaskName(): string {
  return read(NAME_STORAGE) ?? '';
}

export function saveTaskName(name: string): void {
  write(NAME_STORAGE, name);
}

/** The relay's name rule (server/sanitize.py), for a hint before sending. */
export function cleanName(raw: string): string {
  return raw.trim().toLowerCase().replace(/[^a-z0-9_-]/g, '').slice(0, 20);
}
