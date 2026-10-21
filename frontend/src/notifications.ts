/**
 * Notifications from the dashboard.
 *
 * 1. Browser notifications (the Notification API) for edit blocks — anyone's,
 *    or only those involving one name — and conflict alerts, per room, with
 *    the preference in localStorage. Permission is only ever requested from a
 *    click (browsers ignore or punish prompts that are not), and every
 *    storage access is wrapped: blocked storage just means defaults.
 * 2. The owner's webhook API on the relay (server/webhooks.py): list, add and
 *    delete Slack / Discord / generic webhooks with the owner token the
 *    dashboard kept when it created the room (roomAccess.ts).
 *
 * The decision logic (noticeFor) is pure and unit tested
 * (tests/unit/notifications.test.ts).
 */
import type { FeedEvent } from './types';

/** Room codes as relayUrl.ts normalizes them (kept local so node can import this file). */
function normalizeRoomCode(raw: string): string {
  return raw.trim().replace(/[\s-]+/g, '').toUpperCase();
}

// --- browser notifications ---------------------------------------------------------------

export type BlockScope = 'anyone' | 'me' | 'off';

export interface NotificationPrefs {
  /** Master switch for this room. */
  enabled: boolean;
  /** Which edit blocks notify: everyone's, ones involving `myName`, or none. */
  blocks: BlockScope;
  /** The display name `hivelane up` uses (as the relay shows it). */
  myName: string;
  /** Conflict alerts (overlapping or related edits). */
  conflicts: boolean;
}

export const DEFAULT_PREFS: NotificationPrefs = {
  enabled: false,
  blocks: 'anyone',
  myName: '',
  conflicts: true,
};

const PREFS_PREFIX = 'hivelane:notify:';

function prefsKey(roomId: string): string {
  return PREFS_PREFIX + normalizeRoomCode(roomId);
}

/** Names as the relay stores them (server/sanitize.py): lower case, [a-z0-9_-]. */
export function relayName(raw: string): string {
  return raw.trim().toLowerCase().replace(/[^a-z0-9_-]/g, '');
}

export function parsePrefs(raw: string | null): NotificationPrefs {
  if (!raw) return { ...DEFAULT_PREFS };
  try {
    const value = JSON.parse(raw) as Partial<NotificationPrefs>;
    return {
      enabled: value.enabled === true,
      blocks: value.blocks === 'me' || value.blocks === 'off' ? value.blocks : 'anyone',
      myName: typeof value.myName === 'string' ? relayName(value.myName).slice(0, 20) : '',
      conflicts: value.conflicts !== false,
    };
  } catch {
    return { ...DEFAULT_PREFS };
  }
}

const cache = new Map<string, NotificationPrefs>();

export function loadPrefs(roomId: string): NotificationPrefs {
  const key = prefsKey(roomId);
  const known = cache.get(key);
  if (known) return known;
  let raw: string | null = null;
  try {
    raw = globalThis.localStorage?.getItem(key) ?? null;
  } catch {
    raw = null;
  }
  const prefs = parsePrefs(raw);
  cache.set(key, prefs);
  return prefs;
}

export function savePrefs(roomId: string, prefs: NotificationPrefs): void {
  const key = prefsKey(roomId);
  cache.set(key, prefs);
  try {
    globalThis.localStorage?.setItem(key, JSON.stringify(prefs));
  } catch {
    // Not remembered across reloads; still used in this tab.
  }
}

export type PermissionState = NotificationPermission | 'unsupported';

export function permissionState(): PermissionState {
  if (typeof globalThis.Notification === 'undefined') return 'unsupported';
  return globalThis.Notification.permission;
}

/** Ask the browser. Call only from a click handler. */
export async function requestPermission(): Promise<PermissionState> {
  if (typeof globalThis.Notification === 'undefined') return 'unsupported';
  try {
    return await globalThis.Notification.requestPermission();
  } catch {
    return permissionState();
  }
}

export interface BrowserNotice {
  title: string;
  body: string;
  /** Same tag replaces rather than stacks (one per decision or file). */
  tag: string;
}

/** What to show for a decoded feed event under `prefs`, or null for nothing. */
export function noticeFor(
  event: FeedEvent,
  prefs: NotificationPrefs,
  roomId: string,
): BrowserNotice | null {
  if (!prefs.enabled) return null;
  const room = normalizeRoomCode(roomId);
  if (event.type === 'edit_decision' && event.decision === 'block') {
    if (prefs.blocks === 'off') return null;
    const me = relayName(prefs.myName);
    const involved = [event.user_id, ...(event.others ?? [])];
    if (prefs.blocks === 'me' && (!me || !involved.includes(me))) return null;
    const mine = me !== '' && event.user_id === me;
    const holders = (event.others ?? []).join(', ');
    return {
      title: mine ? 'Your agent was blocked' : `${event.user_id}'s agent was blocked`,
      body: `${event.file_path}${holders ? ` — held by ${holders}` : ''}${
        event.reason ? `\n${event.reason}` : ''
      }`,
      tag: `hivelane:${room}:${event.decision_id || `${event.user_id}:${event.file_path}`}`,
    };
  }
  if (event.type === 'conflict_alert') {
    if (!prefs.conflicts) return null;
    if (event.verdict !== 'overlapping' && event.verdict !== 'related') return null;
    return {
      title: `Conflict: ${event.file_path}`,
      body: `${event.users.join(' and ')} — ${event.verdict}${
        event.explanation ? `\n${event.explanation}` : ''
      }`,
      tag: `hivelane:${room}:conflict:${event.file_path}`,
    };
  }
  return null;
}

/** App.tsx: called with every decoded live feed event. */
export function notifyFeedEvent(roomId: string, event: FeedEvent): void {
  if (event.type !== 'edit_decision' && event.type !== 'conflict_alert') return;
  if (permissionState() !== 'granted') return;
  // Someone looking at the dashboard already sees the banner and the lane.
  if (pageIsWatched()) return;
  const notice = noticeFor(event, loadPrefs(roomId), roomId);
  if (!notice) return;
  showNotice(notice);
}

/** The tab is visible and focused: a system notification would only duplicate the page. */
export function pageIsWatched(): boolean {
  const doc = globalThis.document;
  if (!doc) return false;
  try {
    return doc.visibilityState === 'visible' && doc.hasFocus();
  } catch {
    return false;
  }
}

export function showNotice(notice: BrowserNotice): void {
  try {
    const shown = new globalThis.Notification(notice.title, {
      body: notice.body,
      tag: notice.tag,
    });
    shown.onclick = () => {
      globalThis.focus?.();
      shown.close();
    };
  } catch (err) {
    // Some browsers (Android Chrome) only allow notifications from a service worker.
    console.warn('notifications: could not show one', err);
  }
}

// --- webhooks (owner only) ----------------------------------------------------------------

export type WebhookKind = 'slack' | 'discord' | 'generic';
export type WebhookEvent = 'block' | 'warn' | 'conflict_alert' | 'join_request';

export const WEBHOOK_KINDS: WebhookKind[] = ['slack', 'discord', 'generic'];
export const WEBHOOK_EVENTS: WebhookEvent[] = ['block', 'warn', 'conflict_alert', 'join_request'];

export interface WebhookInfo {
  id: string;
  kind: WebhookKind;
  events: WebhookEvent[];
  /** Masked by the relay: scheme, host and the last four characters. */
  url: string;
  created_at: number;
  status: 'ok' | 'failing' | 'paused';
  consecutive_failures: number;
  last_error: string;
}

export class WebhookApiError extends Error {
  readonly status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = 'WebhookApiError';
    this.status = status;
  }
}

function webhooksUrl(origin: string, roomId: string, id?: string): string {
  const base = `${origin}/rooms/${encodeURIComponent(normalizeRoomCode(roomId))}/webhooks`;
  return id ? `${base}/${encodeURIComponent(id)}` : base;
}

async function call(url: string, token: string, init: RequestInit = {}): Promise<Response> {
  const response = await fetch(url, {
    ...init,
    headers: { ...(init.headers ?? {}), 'X-Hivelane-Owner-Token': token },
  });
  if (!response.ok) {
    let detail = `The relay answered ${response.status}`;
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === 'string') detail = body.detail;
      else if (Array.isArray(body.detail) && body.detail.length) {
        const first = body.detail[0] as { msg?: unknown; loc?: unknown };
        if (typeof first.msg === 'string') detail = first.msg;
      }
    } catch {
      // Not JSON: keep the status line.
    }
    throw new WebhookApiError(response.status, detail);
  }
  return response;
}

/** `origin` is the relay's HTTP origin (relayUrl.ts relayHttpOrigin()). */
export async function listWebhooks(
  origin: string,
  roomId: string,
  token: string,
): Promise<WebhookInfo[]> {
  const response = await call(webhooksUrl(origin, roomId), token);
  const body = (await response.json()) as { webhooks?: WebhookInfo[] };
  return Array.isArray(body.webhooks) ? body.webhooks : [];
}

export async function addWebhook(
  origin: string,
  roomId: string,
  token: string,
  hook: { url: string; kind: WebhookKind; events: WebhookEvent[] },
): Promise<WebhookInfo> {
  const response = await call(webhooksUrl(origin, roomId), token, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(hook),
  });
  return ((await response.json()) as { webhook: WebhookInfo }).webhook;
}

export async function deleteWebhook(
  origin: string,
  roomId: string,
  token: string,
  id: string,
): Promise<void> {
  await call(webhooksUrl(origin, roomId, id), token, { method: 'DELETE' });
}

/** Client-side shape check before a round trip; the relay decides for real. */
export function webhookUrlProblem(url: string): string | null {
  const text = url.trim();
  if (!text) return 'Paste the webhook URL';
  let parsed: URL;
  try {
    parsed = new URL(text);
  } catch {
    return 'That is not a URL';
  }
  const local = parsed.hostname === 'localhost' || parsed.hostname === '127.0.0.1';
  if (parsed.protocol !== 'https:' && !(parsed.protocol === 'http:' && local)) {
    return 'Webhook URLs must start with https://';
  }
  return null;
}

/** The kind a pasted URL most likely is (the relay does not care). */
export function guessKind(url: string): WebhookKind {
  try {
    const host = new URL(url.trim()).hostname;
    if (host === 'hooks.slack.com') return 'slack';
    if (host === 'discord.com' || host === 'discordapp.com' || host.endsWith('.discord.com')) {
      return 'discord';
    }
  } catch {
    // fall through
  }
  return 'generic';
}
