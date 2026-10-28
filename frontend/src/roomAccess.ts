/**
 * Room ownership on the dashboard. POST /rooms returns an owner token once;
 * the dashboard that created the room keeps it in localStorage (per room code)
 * and presents it in its spectator join, which makes that tab an owner: it can
 * require approval to join and approve or deny people waiting.
 *
 * localStorage may be unavailable (private windows, blocked storage); every
 * access is wrapped so the dashboard then simply works as a non-owner.
 */

import { normalizeRoomCode, relayHttpOrigin, relaySource, relayWsBase } from './relayUrl';
import type { JoinMessage } from './types';

const OWNER_TOKEN_PREFIX = 'hivelane:owner-token:';

/**
 * A token is kept per relay and room. It is only ever sent to the relay that
 * issued it, so a link with ?relay= naming another server cannot collect it.
 */
function storageKey(roomId: string): string {
  return `${OWNER_TOKEN_PREFIX}${relayWsBase()}|${normalizeRoomCode(roomId)}`;
}

/** Key used before tokens were bound to a relay (room code only). */
function legacyKey(roomId: string): string {
  return `${OWNER_TOKEN_PREFIX}${normalizeRoomCode(roomId)}`;
}

export function rememberOwnerToken(roomId: string, token: string): void {
  try {
    window.localStorage.setItem(storageKey(roomId), token);
  } catch {
    // No storage: this tab still created the room, it just cannot own it.
  }
}

export function ownerToken(roomId: string): string | null {
  try {
    const token = window.localStorage.getItem(storageKey(roomId));
    if (token != null) return token;
    // A token saved before relay binding moves to the relay this dashboard
    // uses on its own, never to one a ?relay= link picked.
    const legacy = window.localStorage.getItem(legacyKey(roomId));
    if (legacy == null || relaySource() === 'chosen') return null;
    window.localStorage.setItem(storageKey(roomId), legacy);
    window.localStorage.removeItem(legacyKey(roomId));
    return legacy;
  } catch {
    return null;
  }
}

export function forgetOwnerToken(roomId: string): void {
  try {
    window.localStorage.removeItem(storageKey(roomId));
    window.localStorage.removeItem(legacyKey(roomId));
  } catch {
    // Nothing stored, or nothing to clear.
  }
}

/**
 * Create a room on the relay and keep its owner token. Returns the room code;
 * throws with the HTTP status when the relay refuses.
 */
export async function createRoom(): Promise<string> {
  const response = await fetch(`${relayHttpOrigin()}/rooms`, { method: 'POST' });
  if (!response.ok) throw new Error(`Server returned ${response.status}`);
  const data = (await response.json()) as { room_id: string; owner_token?: string };
  if (data.owner_token) rememberOwnerToken(data.room_id, data.owner_token);
  return data.room_id;
}

/** The dashboard's join: a spectator, carrying the owner token when it has one. */
export function spectatorJoin(roomId: string): JoinMessage {
  const token = ownerToken(roomId);
  return {
    type: 'join',
    name: null,
    role: 'spectator',
    ...(token ? { owner_token: token } : {}),
  };
}

/**
 * Whether a room_rejected means "not let in" (approval-required room) rather
 * than "room gone". A refusal after join_pending always is; the one refusal
 * that comes without join_pending is a full waiting queue (server/main.py
 * REASON_APPROVAL_QUEUE_FULL).
 */
export function isApprovalRefusal(reason: string, wasWaiting: boolean): boolean {
  return wasWaiting || reason.includes('waiting for approval');
}
