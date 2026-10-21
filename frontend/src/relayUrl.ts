/**
 * Shared helpers for turning the relay's WebSocket base URL into the things
 * pages need — an HTTP origin for POST /rooms, a room-scoped WS URL, the
 * dashboard link for a room, and the CLI line that joins it — plus room-code
 * normalization. Single source of truth so Home, App and the invite panels
 * can't drift.
 */

/** What `synergo up` connects to when given no --relay. */
const DEFAULT_WS_BASE = 'ws://localhost:8000/ws';

const WS_BASE = import.meta.env.VITE_WS_URL ?? DEFAULT_WS_BASE;

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
  return `synergo up --room ${normalizeRoomCode(roomId)}${relay}`;
}

/** The one line a teammate pastes to install Synergo and join a room. */
export function setupCommand(roomId: string): string {
  return `pipx install synergo && ${upCommand(roomId)}`;
}
