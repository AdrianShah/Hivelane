/**
 * Room ownership on the dashboard. POST /rooms returns an owner token once;
 * the dashboard that created the room keeps it in localStorage (per room code)
 * and presents it in its spectator join, which makes that tab an owner: it can
 * require approval to join and approve or deny people waiting.
 *
 * Everyone else gets in with the share link's invite secret (`#invite=`, read
 * from the URL once, then kept for this tab in sessionStorage) or, after the
 * first admission, with the member token the relay hands back (localStorage,
 * keyed like the owner token), so a reload is not asked again. The room code
 * alone only asks an owner to let this tab in.
 *
 * localStorage may be unavailable (private windows, blocked storage); every
 * access is wrapped so the dashboard then simply works as a non-owner.
 */

import {
  isValidRoomCode,
  normalizeRoomCode,
  parseInviteFragment,
  relayHttpOrigin,
  relaySource,
  relayWsBase,
  stripInviteFragment,
} from './relayUrl';
import type { JoinMessage } from './types';
import { livenessFromResponse, ownedRoomCodes, type RoomLiveness } from './dashboard/roomLife';

const OWNER_TOKEN_PREFIX = 'hivelane:owner-token:';
const MEMBER_TOKEN_PREFIX = 'hivelane:member-token:';
const INVITE_PREFIX = 'hivelane:invite:';

/**
 * A token is kept per relay and room. It is only ever sent to the relay that
 * issued it, so a link with ?relay= naming another server cannot collect it.
 */
function storageKey(roomId: string): string {
  return `${OWNER_TOKEN_PREFIX}${relayWsBase()}|${normalizeRoomCode(roomId)}`;
}

function scopedKey(prefix: string, roomId: string): string {
  return `${prefix}${relayWsBase()}|${normalizeRoomCode(roomId)}`;
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

/** The member token the relay issued to this browser for a room, if any. */
export function memberToken(roomId: string): string | null {
  try {
    return window.localStorage.getItem(scopedKey(MEMBER_TOKEN_PREFIX, roomId));
  } catch {
    return null;
  }
}

export function rememberMemberToken(roomId: string, token: string): void {
  try {
    window.localStorage.setItem(scopedKey(MEMBER_TOKEN_PREFIX, roomId), token);
  } catch {
    // Not remembered: the next visit asks again (or uses the link).
  }
}

export function forgetMemberToken(roomId: string): void {
  try {
    window.localStorage.removeItem(scopedKey(MEMBER_TOKEN_PREFIX, roomId));
  } catch {
    // Nothing stored.
  }
}

/** The invite secret this tab holds for a room (from a share link), if any. */
export function inviteSecret(roomId: string): string | null {
  try {
    return window.sessionStorage.getItem(scopedKey(INVITE_PREFIX, roomId));
  } catch {
    return null;
  }
}

export function rememberInviteSecret(roomId: string, secret: string): void {
  try {
    window.sessionStorage.setItem(scopedKey(INVITE_PREFIX, roomId), secret);
  } catch {
    // Used for this page only.
  }
}

export function forgetInviteSecret(roomId: string): void {
  try {
    window.sessionStorage.removeItem(scopedKey(INVITE_PREFIX, roomId));
  } catch {
    // Nothing stored.
  }
}

/**
 * The invite secret for this page: from `#invite=` (then stripped from the
 * address bar and history, so a screenshot or a copied URL does not carry it,
 * and kept for this tab) or one this tab took earlier.
 */
export function takeInviteSecret(roomId: string): string | null {
  if (typeof window !== 'undefined') {
    const fromUrl = parseInviteFragment(window.location.hash);
    if (fromUrl) {
      try {
        const url = window.location.pathname + window.location.search + stripInviteFragment(window.location.hash);
        window.history.replaceState(window.history.state, '', url);
      } catch {
        /* the secret stays in the address bar */
      }
      rememberInviteSecret(roomId, fromUrl);
      return fromUrl;
    }
  }
  return inviteSecret(roomId);
}

/**
 * Create a room on the relay and keep its owner token (and invite secret,
 * which the relay also sends owners in room_access). Returns the room code;
 * throws with the HTTP status when the relay refuses.
 */
export async function createRoom(): Promise<string> {
  const response = await fetch(`${relayHttpOrigin()}/rooms`, { method: 'POST' });
  if (!response.ok) throw new Error(`Server returned ${response.status}`);
  const data = (await response.json()) as { room_id: string; owner_token?: string; invite_secret?: string };
  if (data.owner_token) rememberOwnerToken(data.room_id, data.owner_token);
  if (data.invite_secret) rememberInviteSecret(data.room_id, data.invite_secret);
  return data.room_id;
}

/**
 * The dashboard's join: a spectator, carrying the owner token when it has
 * one, else the member token and the invite secret it holds (either lets it in
 * without an owner's approval; the relay treats a stale one as none).
 */
export function spectatorJoin(roomId: string): JoinMessage {
  const token = ownerToken(roomId);
  if (token) return { type: 'join', name: null, role: 'spectator', owner_token: token };
  const member = memberToken(roomId);
  const invite = inviteSecret(roomId);
  return {
    type: 'join',
    name: null,
    role: 'spectator',
    ...(member ? { member_token: member } : {}),
    ...(invite ? { invite_secret: invite } : {}),
  };
}

/** room_rejected reason when an owner revoked this browser's grant (server/main.py REASON_MEMBER_REVOKED). */
export const MEMBER_REVOKED_REASON = 'an owner removed you from this room';

/**
 * Whether a room_rejected means "not let in" (approval-required room) rather
 * than "room gone". A refusal after join_pending always is; the one refusal
 * that comes without join_pending is a full waiting queue (server/main.py
 * REASON_APPROVAL_QUEUE_FULL).
 */
export function isApprovalRefusal(reason: string, wasWaiting: boolean): boolean {
  return wasWaiting || reason.includes('waiting for approval') || reason === MEMBER_REVOKED_REASON;
}

/**
 * How many owned rooms one page load asks about. GET /rooms/{code} shares the
 * relay's repository-lookup budget (30 a minute per address by default) with
 * every `hivelane up` behind the same address, so the list must not spend it.
 */
const ROOM_CHECK_LIMIT = 8;
/** Answers are reused for this long (moving between pages re-renders the list). */
const ROOM_CHECK_TTL_MS = 60_000;
const checked = new Map<string, { at: number; liveness: RoomLiveness }>();

/** Asks the relay whether one owned room is still alive (needs its owner token). */
export async function checkOwnedRoom(roomId: string): Promise<RoomLiveness | 'rate-limited'> {
  const code = normalizeRoomCode(roomId);
  // Only a well-formed code goes into the request path (codes come from this
  // browser's own storage, but never trust a stored key into a URL).
  if (!isValidRoomCode(code)) return { state: 'unknown' };
  const token = ownerToken(code);
  if (!token) return { state: 'unknown' };
  const cacheKey = `${relayWsBase()}|${code}`;
  const hit = checked.get(cacheKey);
  if (hit && Date.now() - hit.at < ROOM_CHECK_TTL_MS) return hit.liveness;
  let response: Response;
  try {
    response = await fetch(`${relayHttpOrigin()}/rooms/${encodeURIComponent(code)}`, {
      headers: { 'X-Hivelane-Owner-Token': token },
    });
  } catch {
    return { state: 'unknown' };
  }
  if (response.status === 429) return 'rate-limited';
  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    body = null;
  }
  const liveness = livenessFromResponse(response.status, body);
  if (liveness.state !== 'unknown') checked.set(cacheKey, { at: Date.now(), liveness });
  return liveness;
}

/**
 * Checks the owned rooms one at a time (in list order, at most
 * ROOM_CHECK_LIMIT, stopping at the first 429) and reports each answer as it
 * arrives. A room the relay says is gone has its owner token dropped: that
 * is the only thing that removes one, never a failed or ambiguous request.
 */
export async function refreshOwnedRooms(
  codes: string[],
  onResult: (code: string, liveness: RoomLiveness) => void,
): Promise<void> {
  for (const code of codes.slice(0, ROOM_CHECK_LIMIT)) {
    const liveness = await checkOwnedRoom(code);
    if (liveness === 'rate-limited') break;
    if (liveness.state === 'gone') forgetOwnerToken(code);
    onResult(code, liveness);
  }
}

/** Room codes this browser owns on the relay in use (the header's room switcher). */
export function ownedRooms(): string[] {
  try {
    return ownedRoomCodes(window.localStorage, OWNER_TOKEN_PREFIX, relayWsBase());
  } catch {
    return [];
  }
}
