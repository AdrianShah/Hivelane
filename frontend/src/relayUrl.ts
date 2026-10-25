/**
 * Shared helpers for turning the relay's WebSocket base URL into the things
 * pages need — an HTTP origin for POST /rooms, a room-scoped WS URL, the
 * dashboard link for a room, and the CLI line that joins it — plus room-code
 * normalization. Single source of truth so Home, App and the invite panels
 * can't drift.
 */

/** What `hivelane up` connects to when given no --relay. */
const DEFAULT_WS_BASE = 'ws://localhost:8000/ws';

/** Vite's dev server and `vite preview`: no relay on these origins. */
const VITE_PORTS = new Set(['5173', '4173']);

/** Where a relay chosen with ?relay= is kept for the rest of this tab's visit. */
const RELAY_STORAGE_KEY = 'hivelane.relay';

/** The URL query parameter that names the relay: /r/K7M2PQ2X?relay=wss://relay.example/ws */
export const RELAY_PARAM = 'relay';

/**
 * A relay URL as typed or pasted, as the WebSocket base this module works
 * with: https:// and http:// become wss:// and ws://, a bare origin gets the
 * relay's /ws path, a trailing room code or slash is dropped. Anything that is
 * not an http(s)/ws(s) URL gives null.
 */
export function normalizeRelayUrl(raw: string): string | null {
  let url: URL;
  try {
    url = new URL(raw.trim());
  } catch {
    return null;
  }
  const schemes: Record<string, string> = { 'ws:': 'ws:', 'wss:': 'wss:', 'http:': 'ws:', 'https:': 'wss:' };
  const scheme = schemes[url.protocol];
  if (!scheme || !url.host) return null;
  let path = url.pathname.replace(/\/+$/, '');
  // A pasted room socket (…/ws/K7M2PQ2X) names the same relay.
  path = path.replace(/\/ws\/[A-Za-z0-9-]+$/, '/ws');
  if (!path.endsWith('/ws')) path = `${path}/ws`;
  return `${scheme}//${url.host}${path}`;
}

function readStoredRelay(): string | null {
  try {
    return window.sessionStorage.getItem(RELAY_STORAGE_KEY);
  } catch {
    return null; // storage blocked (private mode, sandboxed frame)
  }
}

/**
 * Remembers a relay for this tab, so it survives navigation between the home
 * page and room pages even once the ?relay= parameter is gone from the URL.
 * Returns the normalized URL, or null when `raw` is not a relay URL.
 */
export function rememberRelay(raw: string): string | null {
  const relay = normalizeRelayUrl(raw);
  if (!relay) return null;
  try {
    window.sessionStorage.setItem(RELAY_STORAGE_KEY, relay);
  } catch {
    // Not persisted; this page load still uses it.
  }
  // Takes effect at once (the Home page's relay field), not on the next load.
  resolved = { base: relay, source: 'chosen' };
  return relay;
}

/** Drops a relay picked in this tab: back to the build's or the page's own. */
export function forgetRelay(): void {
  try {
    window.sessionStorage.removeItem(RELAY_STORAGE_KEY);
  } catch {
    // nothing stored
  }
  resolved = resolveWsBase({ ignoreChosen: true });
}

/** A relay the visitor picked: ?relay= on this URL, else one picked earlier in this tab. */
function chosenRelay(): string | null {
  if (typeof window === 'undefined') return null;
  let fromQuery: string | null = null;
  try {
    fromQuery = new URLSearchParams(window.location.search).get(RELAY_PARAM);
  } catch {
    fromQuery = null;
  }
  if (fromQuery) {
    const relay = rememberRelay(fromQuery);
    if (relay) return relay;
  }
  const stored = readStoredRelay();
  return stored ? normalizeRelayUrl(stored) : null;
}

/**
 * True when this build is served from a static host with no relay behind it
 * (GitHub Pages): the page's own origin is then never a relay, so with no
 * relay chosen the dashboard falls back to the CLI default on localhost.
 */
const STATIC_HOST = import.meta.env.VITE_STATIC_HOST === '1';

/** True on a static host build (GitHub Pages): the Home page then asks for a relay URL. */
export function isStaticHost(): boolean {
  return STATIC_HOST;
}

/**
 * Which relay this dashboard talks to:
 * 1. ?relay=wss://… on the URL (a room link from `hivelane up` or a teammate),
 *    remembered for the tab in sessionStorage, or one remembered earlier;
 * 2. VITE_WS_URL, baked in at build time (a dashboard hosted apart from its
 *    relay, e.g. on Vercel);
 * 3. otherwise ws://localhost:8000/ws under `npm run dev` / `vite preview`,
 *    next to a local `uvicorn server.main:app`, and on a static host;
 * 4. otherwise the origin the page came from: a build served by the relay
 *    itself (the Docker image, `hivelane relay`) finds it with no
 *    configuration, as ws:// on http and wss:// on https.
 */
/** Where the relay in use came from (shown on the Home page). */
export type RelaySource = 'chosen' | 'build' | 'default' | 'origin';

function resolveWsBase(options: { ignoreChosen?: boolean } = {}): { base: string; source: RelaySource } {
  const chosen = options.ignoreChosen ? null : chosenRelay();
  if (chosen) return { base: chosen, source: 'chosen' };
  const configured = import.meta.env.VITE_WS_URL?.trim();
  // A VITE_WS_URL without /ws (or as https://) is normalized like a typed one.
  if (configured) return { base: normalizeRelayUrl(configured) ?? configured, source: 'build' };
  if (import.meta.env.DEV || STATIC_HOST || typeof window === 'undefined') {
    return { base: DEFAULT_WS_BASE, source: 'default' };
  }
  const { protocol, host, port } = window.location;
  if (VITE_PORTS.has(port) || !host) return { base: DEFAULT_WS_BASE, source: 'default' };
  const scheme = protocol === 'https:' ? 'wss:' : 'ws:';
  return { base: `${scheme}//${host}/ws`, source: 'origin' };
}

// Resolved on first use, then kept; rememberRelay()/forgetRelay() replace it.
let resolved: { base: string; source: RelaySource } | null = null;

function current(): { base: string; source: RelaySource } {
  if (!resolved) resolved = resolveWsBase();
  return resolved;
}

/** How the relay in use was picked: ?relay= or the Home field, the build, the default, or this page's origin. */
export function relaySource(): RelaySource {
  return current().source;
}

/** ws://localhost:8000/ws -> http://localhost:8000 (strip trailing /ws, swap scheme). */
export function relayHttpOrigin(): string {
  const withoutWsPath = current().base.replace(/\/ws\/?$/, '');
  return withoutWsPath.replace(/^ws/, 'http');
}

/** Base WS URL without a room code — what `hivelane up --relay` takes. */
export function relayWsBase(): string {
  return current().base.replace(/\/$/, '');
}

/** True when the CLI's own default relay is the one this dashboard uses. */
export function isDefaultRelay(): boolean {
  return relayWsBase() === DEFAULT_WS_BASE;
}

/** ws://localhost:8000/ws + K7M2PQ2X -> ws://localhost:8000/ws/K7M2PQ2X */
export function roomWsUrl(roomId: string): string {
  return `${relayWsBase()}/${normalizeRoomCode(roomId)}`;
}

/**
 * Canonical room code from whatever was typed or pasted: case-folded, with
 * spaces and dashes dropped ("abcd-2345" -> "ABCD2345"). A pasted room link
 * works too — its last path segment is the code.
 */
export function normalizeRoomCode(raw: string): string {
  const trimmed = raw.trim().replace(/[?#].*$/, '').replace(/\/+$/, '');
  const lastSegment = trimmed.slice(trimmed.lastIndexOf('/') + 1);
  return lastSegment.replace(/[\s-]+/g, '').toUpperCase();
}

/**
 * New codes are 8 characters; rooms created before the switch have 6-character
 * codes that stay valid until they expire. The relay is the authority on
 * whether a code exists — this only catches typos before a round trip.
 */
export function isValidRoomCode(code: string): boolean {
  return /^(?:[A-Z0-9]{6}|[A-Z0-9]{8})$/.test(code);
}

/** Dashboard route for a room: /r/K7M2PQ2X */
export function roomPath(roomId: string): string {
  return `/r/${normalizeRoomCode(roomId)}`;
}

/**
 * The path this build is served under, without a trailing slash: '' at the
 * root, '/Hivelane' on GitHub Pages (vite.config.ts `base`). The router's
 * basename; roomPath() stays relative to it.
 */
export function basePath(): string {
  return import.meta.env.BASE_URL.replace(/\/+$/, '');
}

/** True when the relay is the server this page was loaded from. */
function relayIsThisOrigin(): boolean {
  if (typeof window === 'undefined') return false;
  try {
    return new URL(relayHttpOrigin()).host === window.location.host;
  } catch {
    return false;
  }
}

/**
 * Shareable dashboard link for a room on this site. Unless this page's own
 * server is the relay, the link carries ?relay= so whoever opens it reaches
 * the same relay: /Hivelane/r/K7M2PQ2X?relay=wss%3A%2F%2Frelay.example%2Fws
 */
export function roomLink(roomId: string): string {
  const link = `${window.location.origin}${basePath()}${roomPath(roomId)}`;
  if (relayIsThisOrigin()) return link;
  return `${link}?${RELAY_PARAM}=${encodeURIComponent(relayWsBase())}`;
}

/**
 * Connects the current repo's agent to a room. --relay is only spelled out
 * when this dashboard talks to a relay other than the CLI default.
 */
export function upCommand(roomId: string): string {
  const relay = isDefaultRelay() ? '' : ` --relay ${relayWsBase()}`;
  const insecure = needsInsecure(relayWsBase()) ? ' --insecure' : '';
  return `hivelane up --room ${normalizeRoomCode(roomId)}${relay}${insecure}`;
}

/**
 * The CLI refuses a plaintext ws:// relay on another machine unless told
 * --insecure (e.g. `hivelane relay` on a LAN), so a copied line must carry it.
 * Mirrors transport_problem in hivelane/config.py.
 */
function needsInsecure(wsBase: string): boolean {
  if (!/^ws:\/\//i.test(wsBase)) return false;
  let host: string;
  try {
    host = new URL(wsBase.replace(/^ws:/i, 'http:')).hostname.toLowerCase();
  } catch {
    return true;
  }
  host = host.replace(/^\[|\]$/g, '');
  const loopback =
    host === 'localhost' ||
    host === '::1' ||
    host === '0.0.0.0' ||
    host.endsWith('.localhost') ||
    host.startsWith('127.');
  return !loopback;
}

/** Installs the CLI (from GitHub until it is on PyPI). */
export const INSTALL_COMMAND = 'pipx install git+https://github.com/AdrianShah/Hivelane';

/** The one line a teammate pastes to install Hivelane and join a room. */
export function setupCommand(roomId: string): string {
  return `${INSTALL_COMMAND} && ${upCommand(roomId)}`;
}
