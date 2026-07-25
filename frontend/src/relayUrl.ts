/**
 * Shared helpers for turning the relay's WebSocket base URL into the two
 * things pages need: an HTTP origin for POST /rooms, and a room-scoped WS
 * URL for the dashboard. Single source of truth so Home and App can't drift.
 */

const WS_BASE = import.meta.env.VITE_WS_URL ?? 'ws://localhost:8000/ws';

/** ws://localhost:8000/ws -> http://localhost:8000 (strip trailing /ws, swap scheme). */
export function relayHttpOrigin(): string {
  const withoutWsPath = WS_BASE.replace(/\/ws\/?$/, '');
  return withoutWsPath.replace(/^ws/, 'http');
}

/** ws://localhost:8000/ws + K7M2PQ -> ws://localhost:8000/ws/K7M2PQ */
export function roomWsUrl(roomId: string): string {
  return `${WS_BASE.replace(/\/$/, '')}/${roomId.trim().toUpperCase()}`;
}

export function normalizeRoomCode(raw: string): string {
  return raw.trim().toUpperCase();
}
