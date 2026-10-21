/**
 * Shared helpers for turning the relay's WebSocket base URL into the things
 * pages need — an HTTP origin for POST /rooms, a room-scoped WS URL, the
 * dashboard link for a room, and the CLI line that joins it — plus room-code
 * normalization. Single source of truth so Home, App and the invite panels
 * can't drift.
 */

/** What `synergo up` connects to when given no --relay. */
const DEFAULT_WS_BASE = 'ws://localhost:8000/ws';

/** Vite's dev server and `vite preview`: no relay on these origins. */
const VITE_PORTS = new Set(['5173', '4173']);

/**
 * Which relay this dashboard talks to:
 * 1. VITE_WS_URL, baked in at build time (a dashboard hosted apart from its
 *    relay, e.g. on Vercel);
 * 2. otherwise ws://localhost:8000/ws under `npm run dev` / `vite preview`,
 *    next to a local `uvicorn server.main:app`;
 * 3. otherwise the origin the page came from: a build served by the relay
 *    itself (the Docker image, `synergo relay`) finds it with no
 *    configuration, as ws:// on http and wss:// on https.
 */
function resolveWsBase(): string {
  const configured = import.meta.env.VITE_WS_URL?.trim();
  if (configured) return configured;
  if (import.meta.env.DEV || typeof window === 'undefined') return DEFAULT_WS_BASE;
  const { protocol, host, port } = window.location;
  if (VITE_PORTS.has(port) || !host) return DEFAULT_WS_BASE;
  const scheme = protocol === 'https:' ? 'wss:' : 'ws:';
  return `${scheme}//${host}/ws`;
}

const WS_BASE = resolveWsBase();

/** ws://localhost:8000/ws -> http://localhost:8000 (strip trailing /ws, swap scheme). */
export function relayHttpOrigin(): string {
  const withoutWsPath = WS_BASE.replace(/\/ws\/?$/, '');
  return withoutWsPath.replace(/^ws/, 'http');
}

/** Base WS URL without a room code — what `synergo up --relay` takes. */
export function relayWsBase(): string {
  return WS_BASE.replace(/\/$/, '');
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

/** Shareable dashboard link for a room on this site. */
export function roomLink(roomId: string): string {
  return `${window.location.origin}${roomPath(roomId)}`;
}

/**
 * Connects the current repo's agent to a room. --relay is only spelled out
 * when this dashboard talks to a relay other than the CLI default.
 */
export function upCommand(roomId: string): string {
  const relay = isDefaultRelay() ? '' : ` --relay ${relayWsBase()}`;
  const insecure = needsInsecure(relayWsBase()) ? ' --insecure' : '';
  return `synergo up --room ${normalizeRoomCode(roomId)}${relay}${insecure}`;
}

/**
 * The CLI refuses a plaintext ws:// relay on another machine unless told
 * --insecure (e.g. `synergo relay` on a LAN), so a copied line must carry it.
 * Mirrors transport_problem in synergo/config.py.
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

/** The one line a teammate pastes to install Synergo and join a room. */
export function setupCommand(roomId: string): string {
  return `pipx install synergo && ${upCommand(roomId)}`;
}
